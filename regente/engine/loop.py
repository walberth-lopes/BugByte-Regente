# -*- coding: utf-8 -*-
"""O processo que continua sozinho.

Ate aqui o motor so avancava quando alguem digitava `regente tick`. Todo o resto
estava pronto -- lease, orcamento, recuperacao, entrega -- e faltava a coisa
mais simples: alguem chamar de novo.

O laco e deliberadamente burro. Ele nao decide nada:

    le a intencao -> se RUNNING, um tick -> bate o ponto -> dorme -> repete

Toda decisao continua onde estava. O laco nao escolhe task, nao ignora
orcamento, nao pula lease e nao tem caminho proprio para nada -- se ele
soubesse decidir alguma coisa, existiriam duas versoes dessa decisao, e a que
diverge seria a que roda de madrugada.

**A intencao e relida a cada volta**, e nao lida uma vez no inicio. E isso que
faz `Pausar` na tela ter efeito em segundos sem matar processo nenhum, e o que
faz `Parar` ser obedecido por um processo que ja estava rodando.

**Erro de um ciclo nao derruba o laco.** Um provedor fora do ar e uma condicao
normal de operacao continua; morrer por causa dela transformaria uma falha de
rede de trinta segundos numa parada que so termina quando um humano perceber.
O que NAO e tolerado e falhar sempre: falhas consecutivas afastam as tentativas
e ficam visiveis no proprio sinal de vida.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from ..core.model import now
from ..core.operation import Intent

#: Quanto o intervalo cresce a cada falha consecutiva, e onde para. Um provedor
#: fora do ar por uma hora nao deve ser consultado 3600 vezes.
BACKOFF = (1, 2, 4, 8, 15)

#: Quanto tempo o laco dorme por vez, no maximo, antes de reler a intencao.
#: Dormir o intervalo inteiro de uma vez faria `Pausar` demorar o intervalo
#: inteiro para ter efeito -- com intervalo de dez minutos, a tela pareceria
#: quebrada.
SLICE_SECONDS = 2.0


@dataclass(slots=True)
class LoopReport:
    """O que este processo fez enquanto viveu. Para o `regente run` imprimir."""
    ticks: int = 0
    dispatched: int = 0
    failures: int = 0
    stopped_because: str = ""


@dataclass(slots=True)
class ContinuousLoop:
    """Roda ticks enquanto a intencao gravada mandar.

    Recebe funcoes, e nao o motor inteiro: quem monta o motor e a composicao, e
    o laco nao pode ganhar a capacidade de montar outro com autoridade diferente.
    """

    #: Devolve (intent, interval_seconds). Relido a cada volta.
    read_intent: Callable[[], tuple[Intent, int]]
    #: Um ciclo. Devolve quantas tasks despachou; pode levantar.
    tick: Callable[[], int]
    #: Publica sinal de vida.
    beat: Callable[[int, str], None]
    #: Apaga o sinal de vida ao sair.
    stand_down: Callable[[], None]

    clock: Callable[[], datetime] = now
    sleep: Callable[[float], None] = None            # type: ignore[assignment]
    #: Sinalizado por Ctrl+C ou por um pedido de parada. Uma `Event` e nao um
    #: booleano porque a espera precisa acordar NA HORA, e nao no fim do sono.
    stopping: threading.Event = field(default_factory=threading.Event)
    on_error: Callable[[BaseException], None] | None = None
    #: Por que paramos. Campo, e nao atributo criado em `run`: com `slots=True`
    #: um atributo nao declarado levanta `AttributeError` -- e levantaria no
    #: caminho de PARADA, que e o pior lugar para descobrir isso.
    _why: str = ""

    def __post_init__(self) -> None:
        if self.sleep is None:
            self.sleep = self.stopping.wait      # acorda cedo quando param

    # ------------------------------------------------------------------
    def request_stop(self, why: str = "sinal") -> None:
        """Pedido de parada vindo de fora do laco -- Ctrl+C, SIGTERM."""
        self._why = why
        self.stopping.set()

    # ------------------------------------------------------------------
    def run(self, max_cycles: int | None = None) -> LoopReport:
        """Roda ate mandarem parar. `max_cycles` existe para o teste terminar.

        O relatorio e devolvido em vez de impresso: o laco nao sabe se quem o
        chamou e um terminal, uma tela ou um teste.
        """
        rel = LoopReport()
        self._why = ""
        seguidas = 0
        # VOLTAS, e nao ciclos de trabalho. `rel.ticks` so cresce quando ha
        # despacho, entao usa-lo como limite deixava `--ciclos 3` girar para
        # sempre com o motor pausado: nenhuma volta produzia tick, e o limite
        # nunca era alcancado. Um limite que nao limita e pior que nenhum --
        # quem o passou acredita que o processo termina.
        voltas = 0

        try:
            while not self.stopping.is_set():
                if max_cycles is not None and voltas >= max_cycles:
                    rel.stopped_because = "limite de ciclos desta execucao"
                    break

                try:
                    intent, intervalo = self.read_intent()
                except Exception as e:                        # noqa: BLE001
                    # Nao dar para ler a intencao e diferente de nao dar para
                    # trabalhar: sem ela o laco nao sabe nem se deveria parar.
                    # Ele espera e tenta de novo, em vez de assumir qualquer
                    # coisa -- assumir RUNNING despacharia trabalho que ninguem
                    # pediu, e assumir STOPPED desligaria o motor por um erro
                    # de leitura.
                    self._report(e)
                    rel.failures += 1
                    seguidas += 1
                    self._wait(self._backoff(seguidas, 30))
                    continue

                voltas += 1
                if intent is Intent.STOPPED:
                    rel.stopped_because = "parada pedida"
                    break

                # Bate o ponto ANTES do trabalho. Um tick longo nao pode fazer o
                # processo parecer morto no meio dele -- que e exatamente quando
                # alguem olharia a tela para saber se ainda esta andando.
                self.beat(rel.ticks, f"intencao {intent.value}")

                if intent is Intent.RUNNING:
                    try:
                        rel.dispatched += int(self.tick() or 0)
                        rel.ticks += 1
                        seguidas = 0
                    except Exception as e:                    # noqa: BLE001
                        # Um ciclo que falha e uma condicao normal de operacao
                        # continua. Morrer aqui transformaria trinta segundos de
                        # rede ruim numa parada que so termina quando alguem ve.
                        self._report(e)
                        rel.failures += 1
                        seguidas += 1
                        rel.ticks += 1
                        self.beat(rel.ticks,
                                  f"{seguidas} falha(s) seguida(s): "
                                  f"{type(e).__name__}")
                # PAUSED cai aqui sem fazer nada: vivo, visivel, e sem
                # despachar. Continuar despachando enquanto a tela diz "pausado"
                # seria a pior mentira que este marco poderia contar.

                self._wait(self._backoff(seguidas, intervalo))
            else:
                rel.stopped_because = self._why or "sinal"
        finally:
            # Sempre. Um processo que sai sem apagar o sinal continua parecendo
            # vivo ate o prazo de graca passar, e a tela mostra `STOPPING` para
            # algo que ja acabou.
            self.stand_down()
        if not rel.stopped_because:
            rel.stopped_because = self._why or "sinal"
        return rel

    # ------------------------------------------------------------------
    def _backoff(self, seguidas: int, base: int) -> float:
        if seguidas <= 0:
            return float(base)
        fator = BACKOFF[min(seguidas, len(BACKOFF)) - 1]
        return float(base) * fator

    def _wait(self, seconds: float) -> None:
        """Dorme em fatias, para obedecer a uma parada sem esperar o sono todo."""
        restante = max(0.0, seconds)
        while restante > 0 and not self.stopping.is_set():
            fatia = min(SLICE_SECONDS, restante)
            self.sleep(fatia)
            restante -= fatia

    def _report(self, e: BaseException) -> None:
        if self.on_error is not None:
            self.on_error(e)

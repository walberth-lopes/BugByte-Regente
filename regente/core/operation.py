# -*- coding: utf-8 -*-
"""O estado do PROCESSAMENTO -- que nao e o estado de nenhuma task.

O motor sempre soube dizer onde cada task esta e o que cada run fez. Nunca soube
dizer se ele proprio esta trabalhando. Sem isso a Mission Control nao tem o que
mostrar nem o que controlar, e -- pior -- nao ha como separar duas coisas que
parecem a mesma de longe:

    o servidor HTTP da tela esta vivo
    o motor esta processando

Sao independentes. A tela pode estar de pe com nenhum processo trabalhando, e um
processo pode estar trabalhando com a tela fechada.

**Intencao e execucao sao separadas.** O que se grava aqui e o que alguem QUER
que aconteca. Quem faz acontecer e um processo que alguem iniciou, e ele publica
um sinal de vida. A leitura combina os dois -- e e por isso que "querer rodar" e
"estar rodando" nunca se confundem: `RUNNING` desejado sem sinal de vida recente
e `DEGRADED`, e dizer isso e o proposito do modulo.

Puro: sem I/O, sem relogio proprio, sem banco. Quem tem esses passa como
argumento.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class Intent(str, Enum):
    """O que uma pessoa pediu. Tres, e nao mais.

    `STOPPED` e o default de proposito: um workspace recem-criado nao comeca a
    despachar trabalho porque foi criado. Ligar e um ato.
    """
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class Phase(str, Enum):
    """O que esta ACONTECENDO, derivado de intencao + sinal de vida.

    Nunca gravado. Uma coluna criaria duas verdades sobre a mesma coisa, e a
    errada seria sempre a coluna -- foi a mesma decisao de `Status` de
    credencial no marco 15, pelo mesmo motivo.
    """
    #: Ninguem pediu para rodar.
    STOPPED = "STOPPED"
    #: Pediram, e ainda nao ha sinal de vida. Normal por alguns segundos.
    STARTING = "STARTING"
    #: Pediram e ha processo trabalhando.
    RUNNING = "RUNNING"
    #: Processo vivo, e deliberadamente sem despachar.
    PAUSED = "PAUSED"
    #: Pediram para parar e ainda ha processo vivo terminando o que comecou.
    STOPPING = "STOPPING"
    #: A intencao diz uma coisa e o mundo diz outra. O estado mais importante
    #: dos seis: e o unico que pede acao de alguem.
    DEGRADED = "DEGRADED"

    @property
    def working(self) -> bool:
        return self is Phase.RUNNING

    @property
    def needs_attention(self) -> bool:
        return self is Phase.DEGRADED


#: Quanto tempo sem sinal de vida antes de um processo ser considerado ausente.
#:
#: Generoso em relacao ao intervalo entre ticks: um tick que demora nao pode
#: parecer um processo morto, ou a tela pisca DEGRADED durante trabalho normal.
#: Quem chama passa o proprio valor quando o intervalo dele for outro.
DEFAULT_GRACE = timedelta(seconds=90)

#: Quanto tempo depois do pedido ainda e `STARTING` e nao `DEGRADED`.
#: Um processo precisa de tempo para subir; acusar antes disso e ruido.
DEFAULT_STARTUP = timedelta(seconds=45)


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """O sinal de vida de um processo que executa ticks.

    `pid` e `host` existem para uma pessoa conseguir achar o processo. Nao sao
    identidade e nao autorizam nada -- quem autoriza e a concessao gravada.
    """
    at: datetime
    pid: int = 0
    host: str = ""
    #: Quantos ticks este processo ja completou. Uma tela que mostra so "vivo"
    #: nao distingue trabalhando de travado num loop sem avancar.
    ticks: int = 0
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Operation:
    """Intencao gravada, e quem a gravou. A leitura combina com o heartbeat."""
    workspace_id: str
    intent: Intent = Intent.STOPPED
    changed_by: str = ""
    changed_at: datetime | None = None
    note: str = ""
    #: Segundos entre ticks. Do workspace, porque um board que muda uma vez por
    #: hora e um que muda a cada minuto nao querem a mesma coisa.
    interval_seconds: int = 60

    def phase(self, beat: Heartbeat | None, at: datetime,
              grace: timedelta = DEFAULT_GRACE,
              startup: timedelta = DEFAULT_STARTUP) -> Phase:
        """O que esta acontecendo AGORA. Derivado, nunca guardado.

        A tabela inteira cabe em seis linhas, e cada uma foi escolhida para que
        a ausencia de evidencia nunca vire evidencia de sucesso:
        """
        vivo = beat is not None and (at - beat.at) <= grace

        if self.intent is Intent.STOPPED:
            # Ninguem pediu para rodar. Um processo ainda vivo esta terminando
            # o que comecou -- isso e `STOPPING`, e nao um erro.
            return Phase.STOPPING if vivo else Phase.STOPPED

        if vivo:
            return Phase.RUNNING if self.intent is Intent.RUNNING else Phase.PAUSED

        # Pediram para rodar (ou pausar) e nao ha ninguem. Por um instante isso
        # e so um processo subindo; passado o prazo, e um problema de verdade e
        # tem de aparecer como tal.
        desde = self.changed_at or at
        if (at - desde) <= startup:
            return Phase.STARTING
        return Phase.DEGRADED


def explain(phase: Phase, beat: Heartbeat | None) -> str:
    """Uma frase que diz o que fazer. Estado sem acao vira enfeite na tela."""
    if phase is Phase.RUNNING:
        return f"processando; {beat.ticks if beat else 0} ciclo(s) neste processo"
    if phase is Phase.PAUSED:
        return "processo vivo, sem despachar; retome quando quiser"
    if phase is Phase.STARTING:
        return "pedido registrado; aguardando o processo dar sinal de vida"
    if phase is Phase.STOPPING:
        return "parada pedida; um processo ainda esta terminando o que comecou"
    if phase is Phase.STOPPED:
        return "ninguem pediu para rodar; use `regente run` para iniciar"
    return ("pedido para rodar, e nenhum processo deu sinal de vida. "
            "Inicie `regente run` nesta pasta, ou pare o processamento")

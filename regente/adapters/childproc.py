# -*- coding: utf-8 -*-
"""Como um subprocesso deste motor recebe -- ou nao recebe -- credencial.

O marco 16 provou **autoridade**: existe uma porta unica que decide se um
adapter pode ter material. Este modulo e a outra metade, e ela nao vem de graca:
o material autorizado precisa chegar ao processo filho, e chegar **so por ali**.

Uma fechadura numa porta que ninguem usa nao tranca nada. Enquanto o `gh` se
autenticava sozinho pelo chaveiro do sistema, o broker podia recusar o dia
inteiro sem mudar o que acontecia no mundo -- e foi exatamente isso que o
levantamento deste marco mediu.

    broker.material(use) -> ambiente do filho -> subprocesso

**Ambiente, nunca argv.** Nao e preferencia de estilo:

  - `argv` e legivel por QUALQUER usuario da maquina. `ps -ef`, o Gerenciador de
    Tarefas, `wmic process get commandline`. Nao ha permissao a pedir.
  - o ambiente e legivel pelo DONO do processo. `/proc/<pid>/environ` e 0400 do
    dono; no Windows exige abrir o processo com `PROCESS_VM_READ`.

Ambiente e melhor, e nao e invisivel -- quem ja e o usuario ve os dois. A
promessa honesta e "nao vaza para outro usuario da maquina, nem para log,
estado, evento, excecao ou remote", e nao "e inextraivel". Onde a ferramenta
aceita stdin, stdin e melhor ainda, e `probe.py` faz isso.

**Montado do vazio.** O ambiente do filho comeca em `{}` e recebe so o que foi
nomeado. Herdar o do pai e o que deixava um subprocesso encontrar credencial que
ninguem lhe deu -- inclusive a de outro provedor.

**Construir nao e autorizar.** Este objeto guarda NOMES; o material e pedido no
momento de disparar o processo. Guardar material aqui o manteria em memoria pelo
resto do processo com base numa construcao, e revogar deixaria de fechar a
porta.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..core import childenv
from ..core.redaction import scrub
from ..ports import AdapterError
from ..ports.support import CredentialBroker


@dataclass(frozen=True, slots=True)
class ChildLaunch:
    """O ambiente pronto de um filho, e o que nao pode sair dele em texto.

    Devolvido junto de proposito. Quem dispara o processo e quem lê o `stderr`
    dele, e o `stderr` de uma ferramenta pode ecoar o que recebeu. Separar as
    duas coisas em objetos diferentes seria pedir para alguem esquecer a
    segunda.
    """

    env: dict[str, str] = field(default_factory=dict)
    #: Valores que NAO podem aparecer em texto que vira registro permanente.
    #: Tupla, e nao um valor: um filho pode receber o mesmo material sob dois
    #: nomes, e uma so das ocorrencias seria limpa.
    secrets: tuple[str, ...] = ()

    def scrub(self, text: str | None) -> str:
        """`text` sem nenhum dos materiais, e sem credencial embutida em URL."""
        return scrub(text, self.secrets)

    # `env` carrega material. A representacao automatica de um dataclass o
    # imprimiria inteiro -- num traceback, num `print` de depuracao, num
    # `repr()` que alguem colocou num log "so para ver o que estava acontecendo".
    def __repr__(self) -> str:
        return (f"ChildLaunch({len(self.env)} vars, "
                f"{len(self.secrets)} secreta(s))")

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class ChildEnvironment:
    """A receita do ambiente de um subprocesso. Sem material dentro."""

    #: Nomes que o programa espera. Vem do contrato do adapter e da configuracao
    #: do workspace -- nunca de uma constante no motor, que assumiria fornecedor.
    #: Vazio significa "este subprocesso nao recebe credencial", que e uma
    #: configuracao real e nao uma peca faltando.
    names: tuple[str, ...] = ()
    #: A porta. `None` e legitimo quando `names` esta vazio, e so ai.
    broker: CredentialBroker | None = None
    #: O que o filho pode ver do ambiente do pai. Comeca do vazio.
    allow: tuple[str, ...] = childenv.BASE_ALLOWLIST
    #: Variaveis fixas e NAO secretas de que a ferramenta precisa -- isolamento
    #: de configuracao, desligar interatividade. Ficam aqui porque sao
    #: conhecimento do fornecedor, e o fornecedor mora no adapter.
    fixed: tuple[tuple[str, str], ...] = ()

    def expects_credential(self) -> bool:
        return bool(self.names)

    def allows(self, use) -> bool:
        """Pergunta que nao consome. Para o adapter anunciar o que sabe fazer.

        Responder `True` nao autoriza nada: `launch()` refaz a autorizacao
        inteira de qualquer forma.
        """
        if not self.names:
            return False
        return self.broker is not None and bool(self.broker.allows(use))

    def plain(self) -> ChildLaunch:
        """O ambiente sem credencial nenhuma.

        Para a invocacao que genuinamente nao precisa de uma -- perguntar a
        versao da ferramenta, por exemplo. Metodo separado, e nao um argumento
        opcional de `launch`, porque "sem credencial" tem de ser uma escolha que
        se le no ponto da chamada.
        """
        return ChildLaunch(env=childenv.compose(
            os.environ, self.allow, {n: v for n, v in self.fixed}))

    def launch(self, use) -> ChildLaunch:
        """O ambiente do filho, resolvido AGORA e nunca antes.

        Uma recusa nao e engolida: ela sobe. Disparar a ferramenta sem a
        credencial que ela esperava produziria um error do provedor -- e alguem
        iria trocar um token que estava bom.
        """
        extra = {name: value for name, value in self.fixed}
        if not self.names:
            return ChildLaunch(env=childenv.compose(os.environ, self.allow, extra))
        if self.broker is None:
            raise AdapterError(
                f"este adapter espera {len(self.names)} variavel(is) de "
                f"credencial e nao recebeu a porta governada; ele nao vai ler "
                f"o ambiente do processo no lugar")

        material = self.broker.material(use)
        for name in self.names:
            extra[name] = material
        return ChildLaunch(env=childenv.compose(os.environ, self.allow, extra),
                           secrets=(material,))

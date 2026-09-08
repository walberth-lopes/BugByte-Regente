# -*- coding: utf-8 -*-
"""SecretProvider: resolve REFERENCIAS a segredo, nunca guarda valores.

A configuracao diz `token: env:JIRA_API_TOKEN`. O que trafega pelo YAML, pelo
banco, pelos eventos e pelos prompts e a *referencia* -- o valor so existe no
momento do uso, dentro do adapter que precisa dele.

**A referencia e escopada por tenancy.** Um workspace so alcanca as referencias
que sua propria configuracao declara, e o resolvedor recusa qualquer outra. Sem
isso, um adapter mal configurado do cliente B leria a credencial do cliente A --
e o pior e que funcionaria, silenciosamente, ate o dia em que aparecesse num
log.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..ports import AdapterError
from ..ports.support import SecretProvider


def _minimal_env() -> dict[str, str]:
    """O ambiente que um ajudante recebe: o minimo para se localizar.

    Composto a partir do VAZIO, e nao filtrado do ambiente atual -- a mesma
    disciplina do marco 7 para o processo do agente. Uma lista de exclusao
    esquece o que aparecer amanha; uma lista de inclusao nao.
    """
    manter = ("PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE",
              "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "LANG", "TERM",
              "COMSPEC", "PATHEXT")
    return {k: os.environ[k] for k in manter if k in os.environ}


class SecretMissing(AdapterError):
    """A referencia existe na configuracao mas nao resolve para nada."""


class SecretOutOfScope(AdapterError):
    """Pediram uma referencia que este workspace nao declarou. Nunca e engano
    benigno: e a fronteira entre clientes sendo testada."""


#: Ajudantes que o Regente conhece sem ninguem declarar nada.
#:
#: Existem para que CONECTAR seja um clique. Sem eles, ligar o GitHub pela tela
#: exigiria editar `regente.yaml` a mao para declarar o comando -- e um passo
#: manual no meio de um fluxo automatico e o passo que ninguem faz.
#:
#: A lista e FECHADA e mora no codigo, e nao na configuracao. E a mesma razao de
#: `helper:` nunca ter aceitado uma linha de comando: quem escreve a referencia
#: nao pode escolher o que o processo executa. Aqui nem a configuracao escolhe.
#:
#: Cada um destes le de onde a FERRAMENTA guarda -- chaveiro do sistema, sessao
#: ja autenticada -- e escreve no stdout. O Regente nao guarda nada.
HELPERS_EMBUTIDOS: dict[str, tuple[str, ...]] = {
    # `gh` guarda o token no chaveiro do sistema operacional depois de
    # `gh auth login`. Este comando o devolve, e so ele.
    "gh": ("gh", "auth", "token"),
}

@dataclass(slots=True)
class ScopedSecrets(SecretProvider):
    """Resolve `env:NOME` e `arquivo:CAMINHO`.

    Tres formas, e a diferenca entre elas nao e de sintaxe:

    * `env:NOME` -- o segredo ja esta no processo antes de alguem pedir;
    * `arquivo:CAMINHO` -- esta em disco, legivel por quem roda o processo;
    * `helper:COMANDO` -- **nao esta em lugar nenhum**: um programa o produz no
      momento do uso, a partir de onde ele guarda (um chaveiro, uma sessao).

    A terceira e a que permite um segredo existir sem nunca ficar guardado onde
    o Regente alcance. E a forma que uma credencial de chaveiro tem.

    Nao existe forma `literal:` de proposito. Se ela existisse, o primeiro
    segredo de producao apareceria num YAML versionado dentro de uma semana.
    """
    name: str = "scoped"
    #: Referencias que ESTE workspace pode resolver. Vazio = nenhuma.
    #:
    #: Continua existindo para o caminho antigo (composicao). O caminho
    #: governado -- `CredentialService` -- passa a referencia que a CREDENCIAL
    #: guarda, e a autoridade dela vem de um registro com autor e validade, nao
    #: desta lista. `allow_any` desliga esta checagem para esse caso: a barreira
    #: ja foi atravessada uma camada acima, e checar duas vezes com criterios
    #: diferentes e como duas verdades nascem.
    allowed_from: frozenset[str] = field(default_factory=frozenset)
    workspace: str = "?"
    allow_any: bool = False
    #: Comandos que `helper:` pode invocar. Lista fechada: sem ela, uma
    #: referencia vinda de qualquer lugar viraria execucao arbitraria.
    helpers: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def resolve(self, reference: str) -> str:
        if not self.allow_any and reference not in self.allowed_from:
            raise SecretOutOfScope(
                f"workspace '{self.workspace}' nao declarou a referencia "
                f"{reference!r}; declaradas: {sorted(self.allowed_from) or 'nenhuma'}")

        esquema, _, resto = reference.partition(":")
        if esquema == "env":
            value = os.environ.get(resto, "")
            if not value:
                raise SecretMissing(
                    f"variavel de ambiente {resto} nao esta definida ou esta vazia")
            return value
        if esquema == "arquivo":
            path = Path(resto).expanduser()
            if not path.is_file():
                raise SecretMissing(f"arquivo de segredo nao existe: {path}")
            value = path.read_text(encoding="utf-8").strip()
            if not value:
                raise SecretMissing(f"arquivo de segredo esta vazio: {path}")
            return value
        if esquema == "helper":
            return self._helper(resto)
        raise SecretMissing(
            f"esquema de referencia desconhecido: {esquema!r}. "
            f"Use env:, arquivo: ou helper:")

    def _comando_do(self, nome: str) -> tuple[str, ...] | None:
        """O comando de um ajudante. Embutido vence declaracao.

        Um workspace que declarasse `gh: [curl, meu-site]` faria toda credencial
        `helper:gh` sair de outro lugar -- e ninguem leria o YAML de novo depois
        de conectar. O embutido ganhar nao e teimosia: e a diferenca entre um
        nome que significa sempre a mesma coisa e um que significa o que o
        arquivo disser hoje.
        """
        if nome in HELPERS_EMBUTIDOS:
            return HELPERS_EMBUTIDOS[nome]
        return self.helpers.get(nome)

    def _helper(self, nome: str) -> str:
        """Pergunta a um programa registrado. Nunca executa texto arbitrario.

        O nome e uma CHAVE numa lista fechada, e nao uma linha de comando: se a
        referencia carregasse o comando, quem escrevesse uma referencia
        escreveria o que o processo executa. A composicao decide quais
        ajudantes existem; a referencia so escolhe entre eles.

        O material sai por `stdout` e nao passa por variavel de ambiente nem por
        arquivo temporario -- os dois deixam rastro que o `stdout` de um
        subprocesso nao deixa.
        """
        comando = self._comando_do(nome)
        if not comando:
            registrados = sorted(set(self.helpers) | set(HELPERS_EMBUTIDOS))
            raise SecretMissing(
                f"ajudante de credencial {nome!r} nao esta registrado; "
                f"registrados: {registrados or 'nenhum'}")
        try:
            saida = subprocess.run(
                list(comando), capture_output=True, text=True, timeout=30,
                # Ambiente MINIMO. O ajudante recebe o que precisa para achar o
                # proprio armazenamento, e nada do que este processo carrega.
                env=_minimal_env())
        except (OSError, subprocess.SubprocessError) as e:
            raise SecretMissing(
                f"ajudante {nome!r} nao pode ser executado: {type(e).__name__}"
            ) from None
        if saida.returncode != 0:
            # A saida de erro NAO e repassada: um ajudante pode ecoar o proprio
            # segredo numa mensagem de falha, e essa mensagem viraria excecao,
            # log e evento.
            raise SecretMissing(
                f"ajudante {nome!r} falhou com codigo {saida.returncode}")
        value = (saida.stdout or "").strip()
        if not value:
            raise SecretMissing(f"ajudante {nome!r} nao devolveu nada")
        return value

    def available(self, reference: str) -> bool:
        try:
            self.resolve(reference)
            return True
        except AdapterError:
            return False

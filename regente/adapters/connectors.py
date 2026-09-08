# -*- coding: utf-8 -*-
"""Conectar um servico em um clique.

Este modulo existe por causa de uma frase: *chego, conecto o GitHub, conecto o
board, conecto o agente, e ponho o agente para trabalhar.* Tudo o que estiver
entre a pessoa e isso e defeito.

O que havia antes: escolher um adapter numa tela, digitar a organizacao, ir a
outra tela, registrar uma credencial digitando `helper:gh`, voltar, e so entao
buscar. Cinco passos em tres lugares para dizer "use o meu GitHub". Cada um
deles era uma decisao que o Regente podia ter tomado sozinho, e nao tomou.

ACENTO. Como o catalogo de provedores, este arquivo escreve portugues com
acento -- e pela mesma razao. O que esta aqui e TEXTO DE PRODUTO: titulos e
frases que aparecem na tela e no terminal para quem esta conectando. O resto do
pacote usa ASCII porque suas mensagens sao de diagnostico; estas sao a
interface, e interface em portugues sem acento le-se como descuido.

O QUE UM CONECTOR FAZ

    estado()        o que falta para isto funcionar, agora
    autorizar()     abre o navegador, quando faltar autorizacao
    contas()        de quais contas esta identidade pode escolher
    proposta()      a configuracao e a credencial que CONECTAR vai gravar

O que ele NAO faz: gravar. Quem grava e o motor, pelo caminho de sempre --
`SettingsService` para a configuracao, `CredentialService` para a credencial.
Um conector que escrevesse direto seria um segundo caminho ate a autoridade, e o
primeiro que aparecesse seria o que ninguem revisou.

E O SEGREDO?

Continua onde sempre esteve: em lugar nenhum que o Regente alcance. `gh` guarda
o token no chaveiro do sistema depois de `gh auth login`; o que o Regente grava
e a REFERENCIA `helper:gh`, e o material e pedido no momento do uso. Conectar
com um clique nao criou caminho novo ate segredo -- ele usa o que ja existia.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from ..ports import AdapterError, Capability, Port
from .secrets import _minimal_env

#: Quanto esperar por uma pergunta rapida a uma ferramenta local.
TIMEOUT = 20


# ===========================================================================
# o vocabulario
# ===========================================================================

@dataclass(frozen=True, slots=True)
class Passo:
    """O que falta, dito para quem vai fazer.

    Nao e um codigo de erro. Cada passo carrega o que a pessoa VE e o que ela
    FAZ -- porque "nao conectado" sem o proximo movimento e a mesma coisa que
    silencio.
    """
    #: `instalar`, `autorizar`, `escolher`, `pronto`.
    codigo: str
    titulo: str
    detalhe: str = ""
    #: O comando equivalente, para quem preferir o terminal. Nunca obrigatorio.
    comando: str = ""
    #: Se seguir daqui abre um navegador.
    navegador: bool = False

    @property
    def conectavel(self) -> bool:
        return self.codigo in ("escolher", "pronto")

    def as_dict(self) -> dict:
        return {"code": self.codigo, "title": self.titulo,
                "detail": self.detalhe, "command": self.comando,
                "opens_browser": self.navegador}


@dataclass(frozen=True, slots=True)
class Conta:
    """Uma conta ou organizacao que esta identidade alcanca."""
    id: str
    nome: str
    tipo: str = "conta"

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.nome, "kind": self.tipo}


@dataclass(frozen=True, slots=True)
class Proposta:
    """O que CONECTAR vai gravar. Montada pelo conector, gravada pelo motor.

    Separar as duas coisas nao e cerimonia: e o que mantem a escrita passando
    por identidade, concessao, capacidade e policy. O conector propoe; quem tem
    autoridade e que grava.
    """
    #: O papel na configuracao: `repository`, `tasks`, `runner`.
    papel: str
    #: O que vai para `providers[papel]`.
    provider: dict[str, Any]
    #: Onde o segredo vive. Sempre uma referencia, nunca material.
    secret_ref: str = ""
    #: O que essa credencial passa a poder fazer.
    capacidades: tuple[str, ...] = ()
    #: Como a credencial se chama na listagem.
    nome_credencial: str = "principal"

    @property
    def precisa_credencial(self) -> bool:
        return bool(self.secret_ref)


class Conector(Port):
    """Um servico que a pessoa conecta. Um clique, e o resto e por conta dele."""

    capability: Capability = Capability.DISCOVERY
    #: Como aparece na tela.
    titulo: str = ""
    descricao: str = ""
    #: Que papel da configuracao ele preenche.
    papel: str = ""

    def estado(self) -> Passo: ...
    def autorizar(self) -> Passo: ...
    def contas(self) -> list[Conta]: ...
    def proposta(self, conta: str) -> Proposta: ...


# ===========================================================================
# ferramenta de linha de comando, com cuidado
# ===========================================================================

def _rodar(comando: list[str]) -> subprocess.CompletedProcess:
    """Pergunta a ferramenta local. Com o ambiente MINIMO, como um ajudante.

    O `env` nao e detalhe: o processo do Regente pode carregar material -- foi
    para isso que a disciplina existe -- e um subprocesso que herda o ambiente
    leva junto o que ninguem quis dar a ele. `gh` precisa se localizar (PATH,
    HOME, o diretorio onde guarda a propria sessao) e nada mais.

    A lista e de INCLUSAO. Uma de exclusao esquece o que aparecer amanha.
    """
    return subprocess.run(comando, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=TIMEOUT,
                          env=_minimal_env())


def _abrir_em_console(comando: list[str]) -> bool:
    """Roda um comando INTERATIVO onde a pessoa consiga ve-lo.

    `gh auth login --web` mostra um codigo de uso unico e espera. Rodado sem
    console, ele nao tem onde mostrar o codigo -- e a pessoa veria o navegador
    abrir pedindo algo que ninguem lhe deu.

    No Windows abre um console proprio. Fora dele nao ha forma portatil de
    fazer isso, e a tela cai no comando escrito -- que e honesto, e nao um
    botao que finge.
    """
    if sys.platform != "win32":
        return False
    try:
        subprocess.Popen(comando, creationflags=0x00000010,  # CREATE_NEW_CONSOLE
                         env=_minimal_env())
        return True
    except (OSError, ValueError):
        return False


# ===========================================================================
# GitHub
# ===========================================================================

@dataclass(slots=True)
class GitHubConector(Conector):
    """O GitHub pela ferramenta que a pessoa provavelmente ja tem.

    POR QUE PELO `gh`, E NAO POR OAUTH PROPRIO: um fluxo OAuth exigiria um
    aplicativo registrado, um `client_id`, uma URL de retorno e um segredo de
    cliente que, num programa distribuido, nao e segredo nenhum. O `gh` ja fez
    tudo isso -- ele tem o proprio aplicativo, faz o fluxo no navegador, e
    guarda o resultado no chaveiro do sistema.

    O que o Regente ganha e exatamente o que ele quer: um clique, um navegador,
    e nenhum segredo em lugar nenhum que ele alcance.
    """
    cli: str = "gh"
    nome: str = "github"
    name: str = "github"
    titulo: str = "GitHub"
    descricao: str = "Onde o Regente lê o código e abre pull requests."
    papel: str = "repository"

    # ------------------------------------------------------------------
    def estado(self) -> Passo:
        if shutil.which(self.cli) is None:
            return Passo(
                "instalar", "Instale o GitHub CLI",
                "O Regente conversa com o GitHub pelo `gh`, que também é quem "
                "guarda a sua autorização com segurança.",
                comando=("winget install --id GitHub.cli" if sys.platform == "win32"
                         else "brew install gh"))
        try:
            saida = _rodar([self.cli, "auth", "status"])
        except (OSError, subprocess.SubprocessError) as e:
            return Passo("instalar", "O GitHub CLI não respondeu",
                         f"{type(e).__name__}: {e}")

        if saida.returncode != 0:
            return Passo(
                "autorizar", "Autorize o Regente a usar sua conta",
                "Abre o github.com no seu navegador. Você aprova uma vez, e a "
                "autorização fica no chaveiro do seu computador.",
                comando=f"{self.cli} auth login --web", navegador=True)

        quem = self._conta_atual()
        return Passo("escolher", "Escolha a conta ou organização",
                     f"Autorizado como {quem}." if quem else "Autorizado.")

    # ------------------------------------------------------------------
    def autorizar(self) -> Passo:
        """Abre o navegador. Devolve o passo seguinte, e nao um sucesso."""
        if shutil.which(self.cli) is None:
            return self.estado()

        comando = [self.cli, "auth", "login", "--web", "--hostname",
                   "github.com", "--git-protocol", "https"]
        if _abrir_em_console(comando):
            return Passo(
                "autorizar", "Terminamos no navegador",
                "Abriu uma janela com um código de uso único e o github.com. "
                "Aprove por lá e volte aqui.",
                comando=" ".join(comando), navegador=True)
        return Passo(
            "autorizar", "Rode este comando uma vez",
            "Ele abre o navegador para você aprovar. Depois volte aqui.",
            comando=" ".join(comando), navegador=True)

    # ------------------------------------------------------------------
    def contas(self) -> list[Conta]:
        """A conta pessoal e as organizacoes, na ordem em que se escolhe.

        A pessoal vem primeiro porque e onde a maioria comeca, e porque e a
        unica que existe com certeza.
        """
        achadas: list[Conta] = []
        eu = self._conta_atual()
        if eu:
            achadas.append(Conta(id=eu, nome=eu, tipo="pessoal"))
        for org in self._organizacoes():
            achadas.append(Conta(id=org, nome=org, tipo="organizacao"))
        return achadas

    # ------------------------------------------------------------------
    def proposta(self, conta: str) -> Proposta:
        if not str(conta).strip():
            raise AdapterError("escolha uma conta ou organização")
        return Proposta(
            papel=self.papel,
            provider={"name": "github", "org": str(conta).strip()},
            # A REFERENCIA, e nunca o token. `gh` continua sendo quem guarda.
            secret_ref="helper:gh",
            # Listar e ler. Escrever -- empurrar, abrir pull request -- e outra
            # conexao, com outra credencial: conectar para ver nao deveria, por
            # tabela, autorizar a mexer.
            capacidades=("repo.discover", "repo.read"),
            nome_credencial="github")

    # ------------------------------------------------------------------
    def _conta_atual(self) -> str:
        try:
            saida = _rodar([self.cli, "api", "user"])
            if saida.returncode != 0:
                return ""
            return str(json.loads(saida.stdout or "{}").get("login") or "")
        except (OSError, subprocess.SubprocessError, ValueError):
            return ""

    def _organizacoes(self) -> list[str]:
        try:
            saida = _rodar([self.cli, "api", "user/orgs", "--paginate"])
            if saida.returncode != 0:
                return []
            bruto = json.loads(saida.stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError):
            # Sem organizacao NAO e erro: a maioria das contas nao tem nenhuma,
            # e a conta pessoal ja basta para trabalhar.
            return []
        return [str(o.get("login")) for o in bruto if o.get("login")]

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

#: Por quanto tempo a resposta de `estado()` continua valendo.
#:
#: A tela le a lista de conectores a cada 5 segundos. Sem esta memoria curta,
#: cada leitura roda `gh auth status` e `gh api user` -- doze subprocessos por
#: minuto para responder uma pergunta que muda uma vez por semana.
#:
#: Curta de proposito: quem acabou de autorizar no navegador espera ver a tela
#: mudar, e nao esperar um minuto. Autorizar limpa a memoria na hora.
VALIDADE_DO_ESTADO = 20.0


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
    #: A ultima resposta de `estado()`, e ate quando ela vale. Memoria curta e
    #: por instancia -- nao ha nada de tenant aqui, so o que a FERRAMENTA local
    #: respondeu sobre a sessao de quem esta na maquina.
    _lembrado: tuple[float, Passo] | None = None
    nome: str = "github"
    #: O ADAPTER que este conector configura. Muda por papel: ler e `github`,
    #: empurrar e `github-write`, acompanhar checks e `github-checks`.
    name: str = "github"
    titulo: str = "GitHub"
    descricao: str = "Onde o Regente lê o código e abre pull requests."
    papel: str = "repository"
    #: O que a credencial DESTE papel passa a poder. Ler nao autoriza empurrar,
    #: e empurrar nao autoriza ler checks -- tres credenciais, tres capacidades.
    capacidades: tuple[str, ...] = ("repo.discover", "repo.read")

    # ------------------------------------------------------------------
    def estado(self) -> Passo:
        import time

        if self._lembrado is not None and time.monotonic() < self._lembrado[0]:
            return self._lembrado[1]
        passo = self._perguntar_estado()
        self._lembrado = (time.monotonic() + VALIDADE_DO_ESTADO, passo)
        return passo

    def _perguntar_estado(self) -> Passo:
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
        # A memoria vale ate alguem mexer -- e autorizar e exatamente mexer.
        self._lembrado = None
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
            provider={"name": self.name, "org": str(conta).strip()},
            # A REFERENCIA, e nunca o token. `gh` continua sendo quem guarda.
            secret_ref="helper:gh",
            capacidades=self.capacidades,
            nome_credencial=self.name)

    # ------------------------------------------------------------------
    @classmethod
    def os_tres(cls) -> tuple["GitHubConector", ...]:
        """Um conector por papel que o GitHub preenche.

        Tres instancias da MESMA classe, e nao tres classes: o que muda entre
        elas e o adapter, a capacidade e a frase. A conversa com o `gh` -- que e
        onde mora o risco -- e uma so.
        """
        return (
            cls(nome="github", name="github", papel="repository",
                titulo="GitHub",
                descricao="Onde o Regente lê o código e abre pull requests.",
                capacidades=("repo.discover", "repo.read")),
            cls(nome="github-write", name="github-write",
                papel="repository_write", titulo="GitHub (publicar)",
                descricao="Para enviar commits e abrir pull requests.",
                capacidades=("repo.push", "repo.pr")),
            cls(nome="github-checks", name="github-checks", papel="cicd",
                titulo="GitHub Actions",
                descricao="Para acompanhar os checks que rodam a cada commit.",
                capacidades=("ci.read",)),
        )

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

# ===========================================================================
# O agente
# ===========================================================================

@dataclass(slots=True)
class AgenteConector(Conector):
    """O agente que ja esta instalado nesta maquina.

    "Onde eu acho o executavel?" era uma pergunta que o formulario fazia e o
    computador sabia responder. Quem instala o Claude Code nao guarda o caminho
    -- ele entra no PATH, e e o PATH que sabe onde.

    NAO PRECISA DE CREDENCIAL, e a ausencia e a resposta a outra duvida: quem
    entrou no Claude Code com a propria conta ja esta autenticado, e o Regente
    usa a sessao que esta la. Nao ha token para registrar, nao ha cofre para
    encher, e conectar e so dizer "use este".
    """
    #: O comando, como ele aparece no PATH.
    cli: str = "claude"
    _lembrado: "tuple[float, Passo] | None" = None
    nome: str = "claude-code"
    name: str = "claude-code"
    titulo: str = "Claude Code"
    descricao: str = "O modelo que lê a task e escreve a mudança."
    papel: str = "runner"
    #: O modelo que a conexao grava. Um so, e o do meio: quem quiser trocar
    #: troca em Configurar, com a lista que a tela ja oferece.
    modelo: str = "sonnet"

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name,
                "cli": self.cli}

    # ------------------------------------------------------------------
    def estado(self) -> Passo:
        import time

        if self._lembrado is not None and time.monotonic() < self._lembrado[0]:
            return self._lembrado[1]
        passo = self._perguntar_estado()
        self._lembrado = (time.monotonic() + VALIDADE_DO_ESTADO, passo)
        return passo

    def _perguntar_estado(self) -> Passo:
        onde = shutil.which(self.cli)
        if onde is None:
            return Passo(
                "instalar", f"Instale o {self.titulo}",
                f"O Regente chama o `{self.cli}` que estiver no seu PATH. "
                f"Depois de instalar, abra um terminal novo e volte aqui.",
                comando=self.COMO_INSTALAR.get(self.nome, ""))
        # Encontrado. NAO se pergunta se ele esta autenticado: o agente resolve
        # a propria sessao, e o `doctor` ja tem uma checagem de prontidao que
        # responde isso melhor do que um palpite daqui.
        return Passo("escolher", f"{self.titulo} encontrado", onde)

    def autorizar(self) -> Passo:
        """Nao ha o que autorizar: quem autentica o agente e o proprio agente."""
        self._lembrado = None
        return self.estado()

    def contas(self) -> list[Conta]:
        """Uma "conta" so: a instalacao encontrada.

        A tela pergunta de qual conta em todo conector, e responder com o
        caminho encontrado mantem o mesmo fluxo -- em vez de um caso especial
        que so este conector tem.
        """
        onde = shutil.which(self.cli)
        return [Conta(id=self.cli, nome=onde or self.cli, tipo="programa")] if onde else []

    def proposta(self, conta: str) -> Proposta:
        return Proposta(
            papel=self.papel,
            provider={"name": self.name, "cli": str(conta).strip() or self.cli,
                      "model": self.modelo},
            # Sem credencial: a sessao do proprio agente e quem autentica.
            secret_ref="", capacidades=())

    #: Como instalar, por ferramenta. Comando, e nao link: quem esta no
    #: terminal copia e cola; quem nao esta pesquisa o nome mesmo assim.
    COMO_INSTALAR = {
        "claude-code": "npm install -g @anthropic-ai/claude-code",
        "codex-cli": "npm install -g @openai/codex",
    }

    @classmethod
    def os_dois(cls) -> tuple["AgenteConector", ...]:
        return (
            cls(cli="claude", nome="claude-code", name="claude-code",
                titulo="Claude Code",
                descricao="O modelo que lê a task e escreve a mudança."),
            cls(cli="codex", nome="codex-cli", name="codex-cli",
                titulo="Codex CLI", modelo="",
                descricao="O modelo que lê a task e escreve a mudança."),
        )

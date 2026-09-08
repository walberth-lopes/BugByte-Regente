# -*- coding: utf-8 -*-
"""Descoberta de recursos, por fornecedor.

Cada classe aqui responde a uma pergunta so: *o que esta identidade alcanca?*
Ela e vizinha da leitura de conteudo, e nao a mesma coisa -- e a separacao e o
assunto do modulo.

DOIS PROVEDORES DE PROPOSITO, E COM ARVORES DIFERENTES:

    GitHub   conta -> repositorio
    Jira     projeto -> board

Se a abstracao so coubesse numa arvore, o segundo nao entraria. O criterio do
marco e que `adapters/clickup/` (workspace -> space -> list) caiba depois sem
tocar em tela, motor ou policy.

A CREDENCIAL E OUTRA, E ISSO E O DESENHO. Descobrir usa `repo.discover` /
`task.discover`; ler conteudo usa `repo.read` / `task.read`. Uma credencial de
descoberta serve para uma pessoa escolher o que o workspace vai usar, e nao
serve para o motor trabalhar. Sem essa separacao, conectar um provedor
concederia leitura de tudo o que a credencial alcanca -- que e exatamente o que
este marco recusa.

O QUE ESTAS CLASSES NAO FAZEM: resolver credencial. Elas chamam quem ja recebeu
o `CredentialBroker` vinculado a quem age e a que workspace. Nenhuma linha aqui
alcanca ambiente, arquivo, chaveiro ou helper.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..core.resource import Falha, Inventario, Kind, Resource, ResourceRef
from ..ports import Capability
from ..ports.discovery import ResourceDiscovery
from ..ports.support import CredentialDenied
from .tasks.transport import AuthFailure


def _agora() -> datetime:
    return datetime.now(timezone.utc)


#: Como uma recusa do caminho governado vira uma `Falha` de dominio.
#:
#: As chaves sao os valores de `Refusal`, que e a mesma palavra que a trilha
#: grava. A tabela mora aqui, e nao no motor, porque quem conhece a excecao do
#: adapter e o adapter -- e o motor nao deve importar excecao de ninguem.
_POR_RECUSA: dict[str, Falha] = {
    "UNAUTHENTICATED": Falha.SEM_CREDENCIAL,
    "NOT_FOUND": Falha.SEM_CREDENCIAL,
    "INVALID": Falha.SEM_CREDENCIAL,
    "EXPIRED": Falha.CREDENCIAL_EXPIRADA,
    "REVOKED": Falha.CREDENCIAL_REVOGADA,
    "NO_CAPABILITY": Falha.SEM_CAPACIDADE,
    "FORBIDDEN": Falha.SEM_CAPACIDADE,
    "POLICY_DENIED": Falha.POLICY_RECUSOU,
    "SOURCE_UNAVAILABLE": Falha.PROVEDOR_INDISPONIVEL,
}


def _falha_de(erro: Exception) -> tuple[Falha, str]:
    """Traduz o que foi levantado para o vocabulario do dominio.

    Por TIPO de excecao, e nunca por texto da mensagem. A primeira versao deste
    modulo procurava "expirad" e "revoked" dentro da string; bastaria um
    provedor mudar a redacao para uma credencial revogada passar a ser reportada
    como instabilidade -- e a pessoa iria investigar a rede.

    A distincao que importa: NENHUMA excecao vira lista vazia. Um provedor fora
    do ar, uma credencial recusada e uma conta realmente sem repositorios sao
    tres coisas, e so a terceira e uma lista vazia.
    """
    if isinstance(erro, CredentialDenied):
        # O Regente recusou ANTES de a rede ser tocada. O motivo ja veio no
        # vocabulario certo -- so falta traduzi-lo.
        return _POR_RECUSA.get(erro.refusal, Falha.SEM_CREDENCIAL), erro.reason
    if isinstance(erro, AuthFailure):
        # O PROVEDOR recusou a credencial. Diferente do caso acima, e a
        # diferenca manda a pessoa a lugares diferentes: aqui, trocar o token.
        return Falha.SEM_CREDENCIAL, str(erro)
    return Falha.PROVEDOR_INDISPONIVEL, f"{type(erro).__name__}: {erro}"


def _nao_deu(nome: str, kind: str, erro: Exception) -> Inventario:
    falha, detalhe = _falha_de(erro)
    return Inventario.nao_deu(nome, kind, falha, detalhe, at=_agora())


# ===========================================================================
# GitHub: conta -> repositorio
# ===========================================================================

@dataclass(slots=True)
class GitHubDiscovery(ResourceDiscovery):
    """O que esta credencial alcanca no GitHub.

    A arvore tem dois niveis. A CONTA existe para dar contexto -- quem esta
    olhando precisa saber de qual organizacao sao aqueles repositorios -- e nao
    e selecionavel: selecionar uma organizacao inteira daria ao motor tudo o que
    ela tem hoje E tudo o que ela ganhar amanha, que e o oposto do que este
    marco veio fazer.
    """
    #: O provider de repositorio ja composto, com o broker vinculado.
    repos: Any
    #: A organizacao configurada. E ela que forma a raiz da arvore.
    org: str
    capability: Capability = Capability.DISCOVERY
    name: str = "github"

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name,
                "arvore": " -> ".join(self.discovers()), "org": self.org}

    def discovers(self) -> tuple[str, ...]:
        return ("account", "repository")

    def discover(self, kind: str,
                 parent: ResourceRef | None = None) -> Inventario:
        if kind == "account":
            # Nao custa rede: a organizacao e o que a configuracao ja declarou.
            return Inventario.achou(self.name, kind, [Resource(
                ref=ResourceRef(self.name, "account", self.org),
                name=self.org, display_name=self.org, role=Kind.CONTAINER,
                path=self.org, url=f"https://github.com/{self.org}",
                selectable=False)], at=_agora())

        if kind != "repository":
            return Inventario.nao_deu(
                self.name, kind, Falha.NAO_SUPORTADO,
                f"este provedor descobre {', '.join(self.discovers())}",
                at=_agora())

        buscar = getattr(self.repos, "discover_repositories", None)
        if buscar is None:
            # Sem o metodo de DESCOBERTA nao ha descoberta. Cair para
            # `list_repositories` funcionaria -- e usaria a credencial de
            # LEITURA para varrer a organizacao, que e a porta dos fundos que
            # este marco existe para fechar.
            return Inventario.nao_deu(
                self.name, kind, Falha.NAO_SUPORTADO,
                "este adapter de repositorio nao descobre recursos", at=_agora())

        try:
            achados = buscar()
        except Exception as e:                            # noqa: BLE001
            # Tudo vira `Inventario`, inclusive o inesperado. Engolir aqui e
            # devolver `[]` seria o defeito que este marco existe para impedir.
            return _nao_deu(self.name, kind, e)

        conta = ResourceRef(self.name, "account", self.org)
        itens = [
            Resource(
                ref=ResourceRef(self.name, "repository", r.ref.key),
                name=r.ref.key, display_name=r.name, role=Kind.CODE,
                parent=conta, path=r.ref.key, url=r.url or "",
                capabilities=frozenset(c.value for c in r.capabilities),
                data={"base_branch": r.base_branch, "archived": r.archived,
                      "private": r.private})
            for r in achados
        ]
        return Inventario.achou(self.name, kind, itens, at=_agora())


# ===========================================================================
# Jira: projeto -> board
# ===========================================================================

@dataclass(slots=True)
class JiraDiscovery(ResourceDiscovery):
    """O que esta credencial alcanca num site Jira.

    Arvore diferente da do GitHub, e de proposito: la a raiz e uma conta e a
    folha e o que se escolhe; aqui a RAIZ e que se escolhe, e a folha e uma
    fatia dela. Forcar a mesma forma nos dois apagaria essa diferenca, e o
    terceiro provedor nao caberia em nenhuma das duas.

    Hoje so o PROJETO e selecionavel. Nao por ser mais importante: e porque a
    leitura de issues sabe dizer de qual projeto uma task veio, e nao sabe dizer
    de qual board. Escolher um board seria ligar um interruptor desligado do
    resto -- ver `RECORTE_POR_BOARD`.

    O TRANSPORTE E PROPRIO. Ele e construido pela composicao com a credencial de
    DESCOBERTA, e nao e o mesmo objeto que le issues. Sao duas capacidades, e
    compartilhar o transporte faria as duas virarem uma.
    """
    #: Transporte de leitura ja autenticado com a capacidade de descoberta.
    transport: Any
    site: str = ""
    max_results: int = 100
    capability: Capability = Capability.DISCOVERY
    name: str = "jira"

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name,
                "arvore": " -> ".join(self.discovers()), "site": self.site}

    def discovers(self) -> tuple[str, ...]:
        return ("project", "board")

    def discover(self, kind: str,
                 parent: ResourceRef | None = None) -> Inventario:
        if kind == "project":
            return self._buscar(kind, "/rest/api/3/project/search",
                                {"maxResults": self.max_results})
        if kind == "board":
            params: dict[str, Any] = {"maxResults": self.max_results}
            if parent is not None:
                # Navegar para dentro do projeto escolhido, e nao varrer o site.
                params["projectKeyOrId"] = parent.id
            return self._buscar(kind, "/rest/agile/1.0/board", params)
        return Inventario.nao_deu(
            self.name, kind, Falha.NAO_SUPORTADO,
            f"este provedor descobre {', '.join(self.discovers())}",
            at=_agora())

    # ------------------------------------------------------------------
    def _buscar(self, kind: str, rota: str, params: dict) -> Inventario:
        try:
            body = self.transport.get(rota, params)
        except Exception as e:                            # noqa: BLE001
            return _nao_deu(self.name, kind, e)

        if not isinstance(body, dict):
            return Inventario.nao_deu(
                self.name, kind, Falha.PROVEDOR_INDISPONIVEL,
                f"a resposta veio como {type(body).__name__}, esperava objeto",
                at=_agora())

        itens = []
        for bruto in (body.get("values") or []):
            recurso = self._recurso(kind, bruto)
            if recurso is not None:
                itens.append(recurso)
        return Inventario.achou(self.name, kind, itens, at=_agora())

    #: Por que um board aparece e nao se escolhe.
    #:
    #: Ele e um recurso legitimo, e nao um no de navegacao inventado. O que
    #: falta e deste lado: a leitura de issues nao sabe dizer de qual board uma
    #: task veio, entao escolher um board seria ligar um interruptor que nao
    #: liga nada -- ou, pior, esvaziaria a fila em silencio, porque nenhuma task
    #: conseguiria provar que pertence ao que foi escolhido.
    RECORTE_POR_BOARD = ("este adapter ainda nao sabe dizer de qual board uma "
                         "task veio; escolha o projeto")

    def _recurso(self, kind: str, bruto: dict) -> Resource | None:
        ident = str(bruto.get("id") or bruto.get("key") or "").strip()
        if not ident:
            # Uma linha sem identidade nao pode ser escolhida nem lembrada.
            # Descartada em silencio de proposito: derrubar a listagem inteira
            # por causa de uma linha esconderia as outras trinta que serviam.
            return None
        nome = str(bruto.get("key") or bruto.get("name") or ident)
        localizacao = bruto.get("location") or {}
        pai = None
        if kind == "board" and localizacao.get("projectId"):
            pai = ResourceRef(self.name, "project",
                              str(localizacao["projectId"]))
        e_projeto = kind == "project"
        return Resource(
            ref=ResourceRef(self.name, kind, ident),
            name=nome,
            display_name=str(bruto.get("name") or nome),
            # So o PROJETO recorta trabalho hoje. O board fica como conteiner
            # visivel, com o motivo escrito -- ver `RECORTE_POR_BOARD`.
            role=Kind.TASK_SOURCE if e_projeto else Kind.CONTAINER,
            parent=pai,
            path=" / ".join(x for x in (self.site, nome) if x),
            url=str(bruto.get("self") or ""),
            selectable=e_projeto,
            note="" if e_projeto else self.RECORTE_POR_BOARD,
            data={k: v for k, v in bruto.items()
                  if k in ("type", "projectTypeKey", "simplified")})

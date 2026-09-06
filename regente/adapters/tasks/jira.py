# -*- coding: utf-8 -*-
"""TaskProvider para Jira Cloud. SOMENTE LEITURA.

Este arquivo e o unico do motor que sabe o que e um `issuelink`, um `parent`,
uma `statusCategory` ou um ADF. Nada disso atravessa a porta.

**Autoridade operacional: nenhuma.** Os metodos de escrita da porta levantam
`SomenteLeitura`. E a garantia de verdade nao esta aqui e sim no transporte, que
so tem `get` -- este adapter nao poderia mutar o Jira nem se o codigo tentasse.

Tres fatos do Jira real que o desenho precisa respeitar, todos medidos em
06/09/2026 contra o site em uso:

1. **Resposta estoura contexto.** Uma busca de 100 issues com os campos
   necessarios devolveu 726 KB. Por isso a lista de campos e sempre enxuta e
   explicita: pedir `*all` e como pedir o banco inteiro.
2. **Paginacao e por cursor**, `nextPageToken`, e nao `startAt`. Paginar errado
   devolve a primeira pagina para sempre -- e o board parece ter 100 itens.
3. **Hierarquia domina o board.** 95 de 100 issues tinham `parent` e havia 118
   links `Relates` contra 28 `Blocks`. Tratar hierarquia ou relacionamento como
   dependencia travaria tudo; so `Blocks` vira ordem de execucao.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ...ports import AdapterError, ReadOnlyRefused
from ...ports.tasks import (BLOCKS, DUPLICATES, PARENT, RELATED, Comment, ExternalTask,
                            ExternalStatus, TaskProvider, TaskRef)
from .transport import Transport

# ---------------------------------------------------------------------------
# MAPEAMENTO -- externo -> interno. Todo desvio esta declarado aqui.
# ---------------------------------------------------------------------------

#: STATUS EXTERNO -> SITUACAO INTERNA.
#:
#: Os nomes foram lidos do board real, nao presumidos. Status fora deste mapa
#: NAO e coagido para o vizinho: vira DESCONHECIDA e sobe como anomalia. Um
#: status novo significa que alguem mudou o processo, e o motor precisa dizer
#: isso em vez de fingir que entendeu.
STATUS_MAP: dict[str, ExternalStatus] = {
    "TO DO": ExternalStatus.NOT_STARTED,
    "BACKLOG": ExternalStatus.NOT_STARTED,
    "PLANNING": ExternalStatus.IN_ANALYSIS,
    "CODING": ExternalStatus.IN_PROGRESS,
    "IN PROGRESS": ExternalStatus.IN_PROGRESS,
    "REVIEWING": ExternalStatus.IN_REVIEW,
    "IN REVIEW": ExternalStatus.IN_REVIEW,
    "QA STAGING": ExternalStatus.IN_VALIDATION,
    "QA PRODUCTION": ExternalStatus.IN_VALIDATION,
    "DONE": ExternalStatus.COMPLETED,
    "CANCELLED": ExternalStatus.CANCELLED,
    "WON'T DO": ExternalStatus.CANCELLED,
}

#: Rede de seguranca por categoria de status. O Jira classifica todo status em
#: `new` / `indeterminate` / `done`, e essa classificacao existe mesmo para
#: status que ninguem mapeou. Usa-la para `done` evita o pior error possivel --
#: despachar trabalho que ja acabou -- sem fingir precisao nos demais:
#: `indeterminate` continua DESCONHECIDA, porque "esta no meio" nao diz se e
#: codigo, revisao ou validacao, e chutar isso e pior que admitir a ignorancia.
CATEGORY_MAP: dict[str, ExternalStatus] = {
    "new": ExternalStatus.NOT_STARTED,
    "done": ExternalStatus.COMPLETED,
}

#: PRIORIDADE EXTERNA -> PRIORIDADE INTERNA (menor roda antes).
#: Espacado de 20 em 20 de proposito: da lugar para um planejador ajustar sem
#: colidir com o valor vindo da origem.
PRIORITY_MAP: dict[str, int] = {
    "HIGHEST": 10, "BLOCKER": 10, "CRITICAL": 10,
    "HIGH": 30, "MAJOR": 30,
    "MEDIUM": 50, "NORMAL": 50,
    "LOW": 70, "MINOR": 70,
    "LOWEST": 90, "TRIVIAL": 90,
}
DEFAULT_PRIORITY = 50

#: TIPO DE LINK EXTERNO -> TIPO INTERNO.
#:
#: A direcao importa e e facil inverter. No Jira, o link vive na issue A com uma
#: ponta: `inwardIssue` significa "A <inward> B". Para `Blocks`, inward e "is
#: blocked by" -- ou seja, **A depende de B**. Outward e "blocks": B depende de
#: A, e o registro correto e na outra issue, que tera seu proprio inward.
#: Registrar os dois lados como bloqueio inverteria metade do grafo.
LINK_MAP: dict[str, str] = {
    "Blocks": BLOCKS,
    "Duplicate": DUPLICATES,
    "Relates": RELATED,
    "Cloners": RELATED,
    "Problem/Incident": RELATED,
}

#: Campos da LISTAGEM. Enxuto por obrigacao: ver fato 1 no topo do arquivo.
#: `description` fica de fora de proposito -- ela e uma arvore ADF, e cem delas
#: transformam uma busca de rotina numa transferencia de megabytes.
LIST_FIELDS = ("summary", "status", "issuetype", "priority", "labels", "parent",
                "issuelinks", "assignee", "project", "updated")

#: Campos do DETALHE. Pagos uma issue por vez, quando alguem de fato vai
#: trabalhar nela. E a unica hora em que a descricao completa vale o custo.
DETAIL_FIELDS = LIST_FIELDS + ("description",)


def _text_from(content: Any, limit: int = 4000) -> str:
    """Descricao do Jira pode vir como texto ou como ADF (arvore JSON).

    Extrai texto legivel dos dois casos. Nao tenta reconstruir formatacao: o que
    o motor precisa e o conteudo, e ADF renderizado por regex vira ruido.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:limit]
    pedacos: list[str] = []

    def anda(no: Any) -> None:
        if len(" ".join(pedacos)) > limit:
            return
        if isinstance(no, dict):
            if no.get("type") == "text" and isinstance(no.get("text"), str):
                pedacos.append(no["text"])
            for filho in (no.get("content") or []):
                anda(filho)
        elif isinstance(no, list):
            for filho in no:
                anda(filho)

    anda(content)
    return " ".join(pedacos)[:limit]


@dataclass(slots=True)
class JiraTasks(TaskProvider):
    """Le trabalho de um site Jira Cloud. Nunca escreve."""

    transporte: Transport
    #: JQL que define o que este workspace considera trabalho seu. Vem da
    #: configuracao: e o unico lugar onde a nocao de "relevante" e declarada.
    jql: str = "statusCategory != Done ORDER BY updated DESC"
    #: De onde sai a chave de recurso usada pelo scheduler. Ver `_recursos`.
    resources_by: str = "parent"
    #: Teto de paginas. Existe para que um JQL solto nao vire uma varredura de
    #: board inteiro num tick.
    max_pages: int = 10
    per_page: int = 100
    site: str = ""
    name: str = "jira"

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name,
                "modo": "somente-leitura", "site": self.site}

    def verify(self) -> None:
        """Prova credencial e alcance com a chamada mais barata que existe."""
        try:
            self.transporte.get("/rest/api/3/myself", {"expand": ""})
        except AdapterError:
            raise
        except Exception as e:  # noqa: BLE001
            raise AdapterError(f"transporte falhou: {type(e).__name__}: {e}") from e

    # ---- leitura ---------------------------------------------------------

    def list_tasks(self, filtro: dict[str, Any] | None = None) -> list[ExternalTask]:
        f = filtro or {}
        jql = f.get("jql") or self.jql
        if f.get("apenas_minhas"):
            jql = f"assignee = currentUser() AND ({jql})"

        items: list[ExternalTask] = []
        cursor: str | None = None
        for _ in range(self.max_pages):
            body = self.transporte.get("/rest/api/3/search/jql", {
                "jql": jql,
                "fields": ",".join(LIST_FIELDS),
                "maxResults": self.per_page,
                "nextPageToken": cursor,
            })
            if not isinstance(body, dict):
                raise AdapterError(f"busca devolveu {type(body).__name__}, esperava objeto")
            for bruto in (body.get("issues") or []):
                items.append(self._normalize(bruto, partial=True))
            cursor = body.get("nextPageToken")
            # `isLast` nem sempre vem; ausencia de cursor e o sinal confiavel.
            if not cursor:
                break
        return items

    def get_task(self, key: str) -> ExternalTask:
        body = self.transporte.get(f"/rest/api/3/issue/{key}",
                                    {"fields": ",".join(DETAIL_FIELDS)})
        if not isinstance(body, dict) or "fields" not in body:
            raise AdapterError(f"issue {key} veio sem 'fields'")
        return self._normalize(body)

    def get_comments(self, key: str) -> list[Comment]:
        body = self.transporte.get(f"/rest/api/3/issue/{key}/comment",
                                    {"maxResults": 50, "orderBy": "created"})
        output = []
        for c in (body.get("comments") or []):
            output.append(Comment(
                author=((c.get("author") or {}).get("displayName") or "?"),
                text=_text_from(c.get("body")),
                criado_em=str(c.get("created") or ""),
                id=str(c.get("id") or "")))
        return output

    # ---- escrita: recusada ----------------------------------------------
    # Nao sao `NotImplementedError`. Sao recusas explicitas, para que um
    # chamador que tente escrever receba a razao -- e para que a intencao fique
    # legivel neste arquivo, e nao apenas no arquivo de policy.

    def _refuse(self, operation: str) -> None:
        raise ReadOnlyRefused(
            f"{self.name} esta montado somente para leitura; '{operation}' nao e "
            f"executavel neste marco. Nenhuma mutacao no sistema externo.")

    def update_task(self, key: str, campos: dict[str, Any]) -> None:
        self._refuse("update_task")

    def transition_task(self, key: str, destination: str) -> None:
        self._refuse("transition_task")

    def add_comment(self, key: str, text: str) -> None:
        self._refuse("add_comment")

    def add_label(self, key: str, label: str) -> None:
        self._refuse("add_label")

    # ---- normalizacao ----------------------------------------------------

    def _normalize(self, bruto: dict[str, Any], partial: bool = False) -> ExternalTask:
        campos = bruto.get("fields") or {}
        key = str(bruto.get("key") or "")
        if not key:
            raise AdapterError("issue sem 'key' -- impossivel dar identidade")

        status = campos.get("status") or {}
        nome_status = str(status.get("name") or "")
        category = str((status.get("statusCategory") or {}).get("key") or "")
        status = STATUS_MAP.get(nome_status.strip().upper())
        if status is None:
            status = CATEGORY_MAP.get(category, ExternalStatus.UNKNOWN)

        prioridade_nome = str((campos.get("priority") or {}).get("name") or "")
        priority = PRIORITY_MAP.get(prioridade_nome.strip().upper(), DEFAULT_PRIORITY)

        links = self._links_of(campos)
        labels = tuple(str(x) for x in (campos.get("labels") or []))

        return ExternalTask(
            key=key,
            title=str(campos.get("summary") or ""),
            status=status,
            external_status=nome_status,
            description=_text_from(campos.get("description")),
            url=f"{self.site.rstrip('/')}/browse/{key}" if self.site else None,
            priority=priority,
            project=str((campos.get("project") or {}).get("key") or ""),
            assignee=((campos.get("assignee") or {}).get("displayName") or None),
            links=links,
            resources=self._resources_of(key, campos),
            labels=labels,
            partial=partial,
            data={
                "tipo": str((campos.get("issuetype") or {}).get("name") or ""),
                "subtarefa": bool((campos.get("issuetype") or {}).get("subtask")),
                "categoria_status": category,
                "atualizada_em": str(campos.get("updated") or ""),
                "prioridade_externa": prioridade_nome,
            })

    def _links_of(self, campos: dict[str, Any]) -> tuple[TaskRef, ...]:
        output: list[TaskRef] = []

        pai = campos.get("parent") or {}
        if pai.get("key"):
            # Hierarquia, nao ordem: a subtarefa NAO espera a mae terminar.
            output.append(TaskRef(key=str(pai["key"]), kind=PARENT))

        for link in (campos.get("issuelinks") or []):
            tipo_externo = str((link.get("type") or {}).get("name") or "")
            kind = LINK_MAP.get(tipo_externo, RELATED)
            dentro, fora = link.get("inwardIssue"), link.get("outwardIssue")
            if dentro and dentro.get("key"):
                # "esta issue <inward> aquela". Para Blocks: "is blocked by".
                output.append(TaskRef(key=str(dentro["key"]), kind=kind))
            elif fora and fora.get("key"):
                # "esta issue <outward> aquela". Para Blocks: "blocks" -- quem
                # depende e a OUTRA issue, e ela registrara o proprio inward.
                # Registrar como bloqueio aqui inverteria a aresta.
                output.append(TaskRef(key=str(fora["key"]),
                                     kind=RELATED if kind == BLOCKS else kind))
        return tuple(output)

    def _resources_of(self, key: str, campos: dict[str, Any]) -> tuple[str, ...]:
        """Chave de exclusao mutua para o scheduler.

        Um provedor de tasks nao sabe quais arquivos serao tocados -- inventar
        isso seria exatamente o tipo de conserto que corrompe o motor. O que ele
        sabe e hierarquia, e ela e um sinal real: duas subtarefas da mesma mae
        quase sempre mexem no mesmo codigo.

        `parent` (padrao): serializa irmas, paraleliza maes diferentes.
        `project`: serializa o projeto inteiro -- conservador, quase sem ganho.
        `nenhum`:  nao declara conflito; so para quem enriquece isso depois.

        A precisao de verdade vem de um agente de analise lendo o codigo. Ate
        la, isto e uma heuristica declarada -- e esta escrito que e.
        """
        if self.resources_by == "nenhum":
            return ()
        if self.resources_by == "project":
            return (f"project:{(campos.get('project') or {}).get('key') or '?'}",)
        pai = (campos.get("parent") or {}).get("key")
        return (f"parent:{pai}",) if pai else (f"issue:{key}",)

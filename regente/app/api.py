# -*- coding: utf-8 -*-
"""A API de leitura da Mission Control.

    Store / Engine  ->  Read Model  ->  **API**  ->  Mission Control

Duas responsabilidades, e so essas duas: **aplicar escopo** e **traduzir para
HTTP**. Nenhuma regra de negocio mora aqui. Se a API precisar decidir alguma
coisa sobre o trabalho -- se uma task esta bloqueada, se um CI passou, se um
lease vale -- e sinal de que a resposta deveria ter vindo pronta do motor.

**Escopo antes do read model, sempre.** A ordem importa e nao e detalhe de
implementacao: primeiro se pergunta se este principal pode ler este workspace;
so depois se lê qualquer coisa. Invertido, o vazamento acontece antes da
verificacao e o codigo continua parecendo certo.

**Somente leitura.** Nao ha rota que escreva. Nao e uma limitacao temporaria a
ser preenchida quando der: as autoridades deste sistema -- commit, push, PR,
aprovacao, merge, deploy, resolucao de task -- pertencem ao motor e ao humano,
e nenhuma delas ganha uma porta nova so porque uma tela tem um botao. Quando uma
acao humana entrar, ela passa pelos mesmos ports e gates que ja existem.

Por que HTTP de biblioteca padrao e nao um framework: a superficie e um punhado
de GETs devolvendo JSON. O que um framework traria -- validacao de corpo,
injecao, documentacao automatica, assincronia -- nao tem uso numa API sem
escrita. O que ele custaria e uma dependencia no caminho de subir a UI, e a
impossibilidade de testar o roteamento sem um cliente de teste no meio.
`resolve()` e uma funcao pura: os testes de tenancy exercitam exatamente o
codigo que o servidor executa, sem camada intermediaria.
"""

from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlsplit

from ..core.principal import ANONYMOUS, Principal
from ..core.resource import Kind, ResourceRef
from ..core.access import PrincipalRef
from ..core.credential import Use
from ..engine.access import AccessService, Refusal
from ..engine.credentials import CredentialService
from ..engine.credentials import Refusal as CredRefusal
from ..engine.decision import Decision, DecisionService, Denial
from ..engine.readmodel import ReadModel

if TYPE_CHECKING:                      # a API conhece a PORTA, nunca o adapter
    from ..ports.identity import IdentityProvider

#: Onde moram os arquivos da Mission Control. Servidos por este mesmo processo:
#: uma segunda porta para servir HTML seria infraestrutura sem beneficio.
UI_ROOT = Path(__file__).resolve().parent / "ui"

#: Extensoes que a UI pode conter. Lista fechada: servir um diretorio inteiro
#: sem restricao e como um caminho arbitrario vira leitura de arquivo arbitrario.
UI_TYPES = {".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon"}


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    payload: Any = None
    content_type: str = "application/json; charset=utf-8"
    body: bytes | None = None

    def rendered(self) -> bytes:
        if self.body is not None:
            return self.body
        return json.dumps(self.payload, ensure_ascii=False,
                          default=_json_default).encode("utf-8")


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"nao serializavel: {type(value).__name__}")


def _error(status: int, code: str, detail: str) -> Response:
    return Response(status, {"error": code, "detail": detail})


#: Como cada recusa do Core vira HTTP.
#:
#: `FORBIDDEN` nao aparece para recurso de outro tenant: la a resposta e 404,
#: porque um 403 confirmaria que o recurso existe. Ele fica para o caso em que o
#: principal E deste workspace e ainda assim nao pode -- onde nao ha o que
#: esconder e ha o que explicar.
DENIAL_STATUS = {
    Denial.UNAUTHENTICATED: 401,
    Denial.FORBIDDEN: 403,
    Denial.NOT_FOUND: 404,
    Denial.INVALID_STATE: 422,
    Denial.POLICY_DENIED: 403,
    Denial.CONFLICT: 409,
}

#: O mesmo, para as recusas de administracao de acesso. Tabela separada porque
#: os vocabularios sao de camadas diferentes e uni-los criaria um acoplamento
#: em que acrescentar um motivo numa muda o significado da outra.
#: As recusas de credencial. Tabela propria: `EXPIRED` e `REVOKED` mandam a
#: pessoa fazer coisas diferentes, e `SOURCE_UNAVAILABLE` nao diz nada sobre a
#: credencial -- diz que nao foi possivel perguntar.
CREDENTIAL_STATUS = {
    CredRefusal.UNAUTHENTICATED: 401,
    CredRefusal.FORBIDDEN: 403,
    CredRefusal.NOT_FOUND: 404,
    CredRefusal.INVALID: 422,
    CredRefusal.POLICY_DENIED: 403,
    CredRefusal.CONFLICT: 409,
    CredRefusal.EXPIRED: 410,
    CredRefusal.REVOKED: 410,
    CredRefusal.NO_CAPABILITY: 403,
    CredRefusal.SOURCE_UNAVAILABLE: 503,
}

#: Como uma recusa de recurso vira HTTP.
#:
#: `SOURCE_UNAVAILABLE` -> 503 e a linha que importa. Ela e a resposta para
#: "nao deu para perguntar ao provedor", e responder 404 ali faria a tela
#: dizer que os repositorios sumiram quando o que caiu foi a rede.
RESOURCE_STATUS = {
    CredRefusal.UNAUTHENTICATED: 401,
    CredRefusal.FORBIDDEN: 403,
    CredRefusal.NOT_FOUND: 404,
    CredRefusal.INVALID: 422,
    CredRefusal.POLICY_DENIED: 403,
    CredRefusal.CONFLICT: 409,
    CredRefusal.NO_CAPABILITY: 403,
    CredRefusal.SOURCE_UNAVAILABLE: 503,
}

REFUSAL_STATUS = {
    Refusal.UNAUTHENTICATED: 401,
    Refusal.FORBIDDEN: 403,
    Refusal.NOT_FOUND: 404,
    Refusal.INVALID: 422,
    Refusal.POLICY_DENIED: 403,
    Refusal.CONFLICT: 409,
}


#: Resposta unica para "nao existe" e para "existe e nao e seu".
#:
#: Distinguir as duas seria confirmar a existencia de um workspace de outro
#: cliente para quem tentou adivinhar -- que e informacao, e informacao vazada.
def _not_found(what: str) -> Response:
    return _error(404, "not_found", f"{what} nao encontrado neste escopo")


@dataclass(slots=True)
class Api:
    """Roteamento puro sobre o read model.

    `resolve` nao toca em socket, nao le cabecalho e nao imprime. E por isso que
    um teste de tenancy consegue provar a rota exata que o servidor executa.
    """
    read: ReadModel
    ui_root: Path = field(default=UI_ROOT)
    #: Como este servidor autentica, em uma frase.
    #:
    #: Aparece na saude porque um mecanismo de desenvolvimento que ninguem
    #: consegue distinguir de um real e pior que nenhum: cria a sensacao de que
    #: ha autenticacao. Quem olha a tela precisa poder ver o que a protege.
    identity_note: str = ""
    identity_is_development: bool = False
    #: De onde a pagina recebe o segredo desta sessao.
    #:
    #: Injetado no HTML servido, e nao numa rota: quem consegue GET na origem ja
    #: e local. O que isso IMPEDE e outra coisa -- um site qualquer aberto no
    #: navegador do operador pode disparar um POST para o loopback, mas nao pode
    #: LER esta pagina (nao ha CORS), entao nao alcanca o token e o POST forjado
    #: chega sem credencial. E a defesa contra pedido forjado de outra origem.
    session_token: str = ""
    #: A unica escrita sobre trabalho. `None` quando esta composicao nao
    #: concede nenhuma -- e uma API sem servico recusa a rota, em vez de
    #: fingir que ela nao existe.
    decisions: DecisionService | None = None
    #: Administracao de acesso. Mesma regra.
    access: AccessService | None = None
    #: Administracao de credenciais. Mesma regra.
    credentials: CredentialService | None = None
    #: Ligar, pausar e parar o processamento. Mesma regra: sem servico, a rota
    #: recusa em vez de fingir que nao existe.
    #:
    #: A tela grava INTENCAO. Ela nao cria processo -- um botao que subisse um
    #: processo daria a uma pagina web o poder de criar processos na maquina de
    #: alguem, que e a autoridade paralela que os marcos 13 a 16 eliminaram.
    operations: "OperationService | None" = None
    #: Configuracao do workspace: provider, mapeamento de status, prioridade.
    #: Sem servico, a rota recusa em vez de fingir que nao existe.
    settings: "SettingsService | None" = None
    #: Como provar uma credencial contra o provedor. Vem da COMPOSICAO, e nao
    #: daqui: escolher a sonda exigiria a API saber o que e um Jira, e o marco
    #: 15 recusou a rota justamente por isso. Agora a composicao entrega, do
    #: mesmo jeito que entrega os servicos.
    probe_for: object | None = None
    #: A configuracao efetiva, para a tela mostrar procedencia. So leitura.
    config: object | None = None
    #: `(nome_do_adapter) -> precisa de credencial?`. Vem da composicao: saber
    #: que um provider de arquivos nao alcanca nada fora da maquina e
    #: conhecimento de fornecedor, e a API nao pode te-lo.
    needs_credential: object | None = None
    #: O catalogo de provedores: o que existe, e o que cada um precisa saber.
    #:
    #: Vem da COMPOSICAO pelo mesmo motivo que `needs_credential` e `probe_for`:
    #: saber que o Jira quer um endereco de site e um email e conhecimento de
    #: fornecedor, e esta camada nao pode importar adapter nenhum.
    #:
    #: Ele existe para a tela poder OFERECER um formulario em vez de uma caixa
    #: de JSON. Sem ele, a lista de campos de cada provedor viveria no frontend
    #: -- uma segunda definicao do formato, que divergiria da primeira e
    #: aceitaria o que o motor recusa.
    catalog: dict | None = None
    #: Descobrir e escolher recursos. `None` quando a composicao nao os
    #: administra -- e a tela mostra que integracoes nao sao editaveis aqui,
    #: em vez de oferecer um botao que responde 500.
    resources: "ResourceService | None" = None
    #: `{provedor: (tipos que ele descobre)}`. Vem da COMPOSICAO pelo mesmo
    #: motivo que `catalog`: saber que o Jira tem projetos e boards e o GitHub
    #: tem contas e repositorios e conhecimento de fornecedor, e esta camada
    #: nao importa adapter nenhum.
    discovery_trees: dict | None = None
    #: Sessao declarada somente-leitura pela composicao.
    #:
    #: NAO substitui nenhuma barreira: e uma recusa ADICIONAL, antes das do
    #: Core. Quem roda `regente ui --read-only` esta dizendo "nesta sessao,
    #: nada"; as concessoes continuam valendo em qualquer outra.
    read_only: bool = False

    # ------------------------------------------------------------------
    def resolve(self, method: str, path: str,
                query: dict[str, list[str]] | None = None,
                principal: Principal | None = None,
                body: dict | None = None) -> Response:
        # ANONIMO por default, nunca "operador local". Um default permissivo
        # aqui faria toda requisicao sem credencial passar como o dono.
        who = principal if principal is not None else ANONYMOUS
        query = query or {}

        parts = [unquote(p) for p in path.strip("/").split("/") if p]

        if method in ("POST", "DELETE"):
            # Escritas NOMEADAS, uma a uma. Tudo o que nao for uma delas
            # continua recusado com a mesma frase de antes: a ausencia de
            # escrita nao e uma lacuna a preencher quando der.
            if self.read_only:
                return _error(403, "read_only_session",
                              "esta sessao foi aberta como somente leitura")
            if (method == "POST" and len(parts) == 6
                    and parts[3] == "approvals" and parts[5] == "decision"):
                return self._decide(parts, body, who)
            if (len(parts) >= 4 and parts[:2] == ["api", "workspaces"]
                    and parts[3] == "access"):
                return self._access_write(method, parts, body, who)
            if (len(parts) >= 4 and parts[:2] == ["api", "workspaces"]
                    and parts[3] == "credentials"):
                return self._credential_write(method, parts, body, who)
            if (method == "POST" and len(parts) == 5
                    and parts[:2] == ["api", "workspaces"]
                    and parts[3:] == ["operation", "intent"]):
                return self._operation_write(parts[2], body, who)
            if (len(parts) == 5 and parts[:2] == ["api", "workspaces"]
                    and parts[3] == "settings"):
                return self._settings_write(method, parts[2], parts[4],
                                            body, who)
            if (len(parts) >= 4 and parts[:2] == ["api", "workspaces"]
                    and parts[3] == "resources"):
                return self._resource_write(method, parts, body, who)
            return _error(405, "read_only",
                          "esta API e somente leitura; autoridade de escrita "
                          "pertence ao motor e ao humano, nao a uma tela")

        if method not in ("GET", "HEAD"):
            return _error(405, "read_only",
                          "esta API e somente leitura; autoridade de escrita "
                          "pertence ao motor e ao humano, nao a uma tela")

        if not parts or parts[0] != "api":
            return self._static(parts)

        rest = parts[1:]
        if rest == ["health"]:
            return self._global_health(who)
        if rest == ["clients"]:
            return Response(200, {"clients": [c.as_dict()
                                              for c in self.read.clients()
                                              if self._visible(c, who)]})
        if rest == ["workspaces"]:
            return Response(200, {"workspaces": [
                w.as_dict() for w in self.read.workspaces()
                if who.may_read(w.id)]})

        if rest == ["catalog"]:
            # Leitura publica desta origem, e sem escopo de workspace: o
            # catalogo diz o que o Regente SABE FAZER, e nao o que alguem tem.
            # Nao ha nada aqui que pertenca a um cliente.
            if self.catalog is None:
                return _error(501, "no_catalog",
                              "esta composicao nao entregou catalogo de "
                              "provedores; configure pelo arquivo regente.yaml")
            return Response(200, self.catalog)

        if len(rest) >= 2 and rest[0] == "workspaces":
            return self._workspace_route(rest[1], rest[2:], query, who)

        return _not_found("recurso")

    # ------------------------------------------------------------------
    def _workspace_route(self, workspace_id: str, rest: list[str],
                         query: dict[str, list[str]],
                         who: Principal) -> Response:
        """Escopo primeiro, leitura depois.

        As duas linhas seguintes sao a fronteira inteira de multi-tenancy desta
        API. Elas vem antes de qualquer chamada ao read model de proposito: um
        `may_read` avaliado depois da leitura protege o log, nao o dado.
        """
        if not who.may_read(workspace_id):
            return _not_found("workspace")
        if self.read.store.workspace(workspace_id) is None:
            return _not_found("workspace")

        if not rest:
            overview = self.read.overview(workspace_id)
            return (Response(200, overview.as_dict()) if overview
                    else _not_found("workspace"))

        head, tail = rest[0], rest[1:]

        if head == "overview" and not tail:
            overview = self.read.overview(workspace_id)
            return (Response(200, overview.as_dict()) if overview
                    else _not_found("workspace"))

        if head == "health" and not tail:
            view = self.read.health(workspace_id)
            return Response(200, view.as_dict()) if view else _not_found("workspace")

        if head == "settings" and not tail:
            return self._settings_read(workspace_id, who)

        if head == "connections" and not tail:
            return self._connections(workspace_id, who)

        if head == "queue" and not tail:
            return self._queue_preview(workspace_id, who)

        if head == "operation" and not tail:
            # "o servidor HTTP esta vivo" e "o motor esta processando" sao
            # coisas diferentes, e esta rota existe para a tela nao confundir as
            # duas. Se ela nao existisse, uma pagina que carrega pareceria um
            # motor que trabalha.
            if self.operations is None:
                return Response(200, {
                    "phase": "STOPPED", "intent": "STOPPED",
                    "explain": "esta composicao nao acompanha o processamento",
                    "controllable": False})
            from ..core.operation import explain

            op, beat, fase = self.operations.state(workspace_id)
            return Response(200, {
                "phase": fase.value,
                "intent": op.intent.value,
                "explain": explain(fase, beat),
                "needs_attention": fase.needs_attention,
                "interval_seconds": op.interval_seconds,
                "changed_by": op.changed_by,
                "changed_at": op.changed_at,
                "controllable": True,
                "process": ({"pid": beat.pid, "host": beat.host,
                             "ticks": beat.ticks, "at": beat.at,
                             "detail": beat.detail} if beat else None)})

        if head == "tasks":
            if not tail:
                state = _one(query, "state")
                return Response(200, {"tasks": [
                    t.as_dict()
                    for t in self.read.tasks(workspace_id, state)]})
            if len(tail) == 1:
                detail = self.read.task(workspace_id, tail[0])
                return (Response(200, detail.as_dict()) if detail
                        else _not_found("task"))

        if head == "runs":
            if not tail:
                return Response(200, {"runs": [
                    r.as_dict()
                    for r in self.read.runs(workspace_id, _int(query, "limit", 100))]})
            if len(tail) == 1:
                detail = self.read.run(workspace_id, tail[0])
                return (Response(200, detail.as_dict()) if detail
                        else _not_found("run"))

        if head == "deliveries" and not tail:
            return Response(200, {"deliveries": [
                d.as_dict()
                for d in self.read.deliveries(workspace_id, _one(query, "task"))]})

        if head == "events" and not tail:
            return Response(200, {"events": [
                e.as_dict()
                for e in self.read.events(workspace_id, _int(query, "limit", 100))]})

        if head == "access" and not tail:
            if self.access is None:
                return _error(403, "no_authority",
                              "esta composicao nao administra acesso")
            found = self.access.listing(who, workspace_id)
            if not isinstance(found, list):
                return _error(REFUSAL_STATUS.get(found.refusal, 403),
                              found.refusal.value.lower(), found.reason)
            return Response(200, {"access": [_grant_dict(g) for g in found]})

        if head == "credentials" and not tail:
            if self.credentials is None:
                return _error(403, "no_authority",
                              "esta composicao nao administra credenciais")
            found = self.credentials.listing(who, workspace_id)
            if not isinstance(found, list):
                return _error(CREDENTIAL_STATUS.get(found.refusal, 403),
                              found.refusal.value.lower(), found.reason)
            at = self.read.clock()
            return Response(200, {"credentials": [_credential_dict(c, at)
                                                  for c in found]})

        if head == "resources":
            return self._resources_read(workspace_id, tail, query, who)

        if head == "escalations" and not tail:
            return Response(200, {"escalations": [
                e.as_dict() for e in self.read.escalations(workspace_id)]})

        return _not_found("recurso")

    # ------------------------------------------------------------------
    def _operation_write(self, workspace_id: str, body: dict | None,
                         who: Principal) -> Response:
        """Grava a intencao de operacao. Mesmo caminho do terminal.

        A tela nao decide se pode: ela pergunta ao mesmo servico que o
        `regente engine` chama, e mostra a resposta. Uma segunda avaliacao aqui
        seria uma segunda autoridade, e as duas divergiriam.
        """
        from ..core.operation import Intent

        if self.operations is None:
            return _error(501, "sem_servico",
                          "esta composicao nao concede controle do motor")
        if not who.may_read(workspace_id):
            return _not_found("workspace")

        bruto = str((body or {}).get("intent") or "").strip().upper()
        try:
            intent = Intent(bruto)
        except ValueError:
            return _error(400, "invalid_argument",
                          f"intencao {bruto!r} nao existe; use "
                          f"{', '.join(i.value for i in Intent)}")

        intervalo = (body or {}).get("interval_seconds")
        saida = self.operations.set_intent(
            who, workspace_id, intent,
            note=str((body or {}).get("note") or "")[:300],
            interval_seconds=int(intervalo) if intervalo is not None else None)
        if not saida.accepted:
            return _error(OPERATION_STATUS.get(saida.refusal, 400),
                          saida.refusal.lower(), saida.reason)
        return Response(200, {"intent": saida.intent.value,
                              "detail": saida.detail})

    def _settings_write(self, method: str, workspace_id: str, key: str,
                        body: dict | None, who: Principal) -> Response:
        """`POST .../settings/{chave}` grava; `DELETE` devolve ao arquivo."""
        if self.settings is None:
            return _error(501, "sem_servico",
                          "esta composicao nao configura workspaces")
        if not who.may_read(workspace_id):
            return _not_found("workspace")

        if method == "DELETE":
            saida = self.settings.clear(who, workspace_id, key)
        else:
            if not isinstance(body, dict) or "value" not in body:
                return _error(400, "invalid_body",
                              "corpo precisa ser {\"value\": ...}")
            saida = self.settings.put(who, workspace_id, key, body["value"])

        if not saida.accepted:
            return _error(SETTINGS_STATUS.get(saida.refusal, 400),
                          saida.refusal.lower(), saida.reason)
        return Response(200, {"key": saida.key, "detail": saida.detail})

    # ---- integracoes -------------------------------------------------
    #
    # Duas rotas de leitura e tres de escrita, e a fronteira entre elas e o
    # assunto do marco: DESCOBRIR pergunta ao provedor o que a identidade
    # alcanca; ESCOLHER grava o que este workspace passa a usar. Descobrir nao
    # seleciona nada, e nenhuma rota aqui escreve sem passar pelo servico.

    def _resources_read(self, workspace_id: str, tail: list[str],
                        query: dict[str, list[str]],
                        who: Principal) -> Response:
        """`GET .../resources` e `GET .../resources/providers`."""
        if tail == ["providers"]:
            # O que cada provedor SABE descobrir, e em que ordem de arvore.
            # A tela precisa disto para navegar sem conhecer fornecedor: ela le
            # a arvore daqui em vez de trazer um `if provider === 'github'`.
            arvores = self.discovery_trees or {}
            return Response(200, {"providers": [
                {"provider": nome, "tree": list(tipos),
                 "root": (list(tipos)[0] if tipos else "")}
                for nome, tipos in sorted(arvores.items())]})

        if tail:
            return _not_found("recurso")

        if self.resources is None:
            # Sem servico, a resposta e uma lista vazia com o aviso -- e nao um
            # erro. Um workspace que nao administra integracoes por aqui
            # continua sendo um workspace valido.
            return Response(200, {"resources": [], "editable": False,
                                  "explain": "esta composicao nao administra "
                                             "integracoes"})

        papel = (query.get("role") or [""])[0].strip()
        try:
            filtro = Kind(papel) if papel else None
        except ValueError:
            return _error(400, "invalid_argument",
                          f"papel invalido; use um de "
                          f"{', '.join(k.value for k in Kind)}")

        found = self.resources.selected(who, workspace_id, filtro)
        if not isinstance(found, list):
            return _error(RESOURCE_STATUS.get(found.refusal, 403),
                          found.refusal.value.lower(), found.reason)
        return Response(200, {
            "editable": True,
            "resources": [{
                "provider": s.ref.provider, "kind": s.ref.kind, "id": s.ref.id,
                "ref": str(s.ref), "name": s.name, "role": s.role.value,
                "selected_by": s.selected_by, "selected_at": s.selected_at,
                "last_seen_at": s.last_seen_at,
            } for s in found]})

    def _resource_write(self, method: str, parts: list[str], body: dict | None,
                        who: Principal) -> Response:
        """`POST .../resources/discover`, `.../select`; `DELETE .../{ref}`.

        Descobrir e POST por um motivo, e nao por descuido: ele custa uma ida
        ao provedor e deixa um evento na trilha. Um GET que faz isso seria
        disparado por qualquer recarga de pagina, e o rastro ficaria cheio de
        descobertas que ninguem pediu.
        """
        if self.resources is None:
            return _error(403, "no_authority",
                          "esta composicao nao administra integracoes")
        workspace_id = parts[2]
        if not who.may_read(workspace_id):
            return _not_found("workspace")

        if method == "DELETE":
            if len(parts) != 5:
                return _error(405, "read_only", "rota inexistente")
            try:
                ref = ResourceRef.parse(parts[4])
            except ValueError as e:
                return _error(400, "invalid_argument", str(e))
            return self._resource_outcome(
                self.resources.unselect(who, workspace_id, ref))

        if len(parts) != 5 or not isinstance(body, dict):
            return _error(400, "invalid_body", "corpo precisa ser um objeto JSON")

        # Nem ator, nem escopo vem do corpo. A mesma recusa das credenciais, e
        # pelo mesmo motivo: quem age e quem a identidade disser, e nunca quem
        # a requisicao afirmar ser.
        for proibido in ("actor", "selected_by", "workspace_id", "client_id"):
            if proibido in body:
                return _error(400, "invalid_body",
                              f"'{proibido}' nao e aceito: ator e escopo nao "
                              f"vem da requisicao")

        provider = str(body.get("provider") or "").strip()
        kind = str(body.get("kind") or "").strip()
        if not provider or not kind:
            return _error(400, "invalid_body",
                          "'provider' e 'kind' sao obrigatorios")

        pai = None
        if body.get("parent"):
            try:
                pai = ResourceRef.parse(str(body["parent"]))
            except ValueError as e:
                return _error(400, "invalid_argument", str(e))

        if parts[4] == "discover":
            achado = self.resources.discover(who, workspace_id, provider, kind,
                                             pai)
            # 200 mesmo quando a descoberta falhou, e isso e deliberado: a
            # resposta CARREGA a falha junto do que ja estava selecionado. Um
            # 503 aqui apagaria a lista, e a tela mostraria um workspace vazio
            # quando o que houve foi o provedor fora do ar.
            return Response(200, achado.as_dict())

        if parts[4] == "select":
            ids = body.get("ids")
            if not isinstance(ids, list):
                return _error(400, "invalid_body",
                              "'ids' precisa ser uma lista de identificadores")
            return self._resource_outcome(self.resources.select_refs(
                who, workspace_id, provider, kind,
                [str(i) for i in ids], pai))

        return _error(405, "read_only", "rota inexistente")

    def _resource_outcome(self, saida) -> Response:
        if not saida.accepted:
            return _error(RESOURCE_STATUS.get(saida.refusal, 400),
                          saida.refusal.value.lower() if saida.refusal
                          else "recusado", saida.reason)
        return Response(200, {"detail": saida.reason,
                              "refs": [str(r) for r in saida.refs]})

    def _settings_read(self, workspace_id: str, who: Principal) -> Response:
        """A configuracao efetiva, campo a campo, COM a procedencia.

        A procedencia nao e enfeite: sem ela alguem edita o `regente.yaml`, nada
        muda, e a conclusao razoavel e que o Regente esta quebrado.
        """
        from ..core.settings import OVERRIDABLE, Source, describe, effective

        overlay = (self.settings.overlay(workspace_id)
                   if self.settings is not None else None)
        do_arquivo = _file_values(self.config)

        campos = {}
        for chave in OVERRIDABLE:
            campo = effective(chave, do_arquivo.get(chave), overlay)
            campos[chave] = {
                "value": campo.value,
                "source": campo.source.value,
                "overridden": campo.overridden,
                "conflicts": campo.conflicts,
                "shadowed": campo.shadowed,
                "explain": describe(campo, chave),
            }
        return Response(200, {
            "fields": campos,
            "editable": self.settings is not None,
            "changed_by": overlay.changed_by if overlay else "",
            "changed_at": overlay.changed_at if overlay else None})

    def _connections(self, workspace_id: str, who: Principal) -> Response:
        """Prontidao por provider, vinda dos servicos REAIS.

        Nunca "conectado" porque existe configuracao. Um provider configurado e
        sem credencial e um provider que nao funciona, e dizer o contrario faria
        a pessoa procurar o problema no lugar errado.
        """
        from ..core.credential import Status, Use

        do_arquivo = _file_values(self.config)
        overlay = (self.settings.overlay(workspace_id)
                   if self.settings is not None else None)
        from ..core.settings import effective

        configurados = effective("providers", do_arquivo.get("providers"),
                                 overlay).value or {}

        credenciais = []
        if self.credentials is not None:
            saida = self.credentials.listing(who, workspace_id)
            if isinstance(saida, list):
                credenciais = saida

        agora = self.read.clock()
        conexoes = []
        for papel, pede_por_papel in PROVIDER_ROLES:
            conf = configurados.get(papel)
            nome = (conf.get("name") if isinstance(conf, dict)
                    else getattr(conf, "name", None))
            # O ADAPTER decide, e nao o papel: um provider de tasks em arquivo
            # nao alcanca nada fora da maquina. Sem isto a tela cobraria uma
            # credencial que nao existe, e mandaria a pessoa procurar problema
            # onde nao ha.
            precisa = (pede_por_papel and bool(nome)
                       and (self.needs_credential(nome)
                            if self.needs_credential is not None else True))
            deste = [c for c in credenciais if c.provider == papel]
            viva = [c for c in deste if c.status(agora) is Status.ACTIVE]
            conexoes.append({
                "role": papel,
                "adapter": nome or "",
                "needs_credential": precisa,
                "state": _connection_state(nome, precisa, deste, viva, agora),
                "credentials": len(deste),
                "live_credentials": len(viva),
                "capabilities": sorted({u.value for c in viva
                                        for u in c.capabilities}),
            })
        return Response(200, {"connections": conexoes})

    def _queue_preview(self, workspace_id: str, who: Principal) -> Response:
        """O que o motor escolheria AGORA, e por que. Nao executa nada.

        Le do estado ja descoberto; nao chama provider e nao despacha. Uma
        previa que executasse trabalho para se mostrar seria a pior forma de
        explicar uma configuracao.
        """
        # Reavalia com as regras de AGORA, e nao com o veredito do ultimo tick.
        #
        # Mostrar o veredito guardado faria editar uma regra e nao ver nada
        # mudar -- que e a confusao exata que esta pagina existe para evitar. A
        # base e a prioridade DA ORIGEM, para os deltas nao se comporem.
        from ..core.selection import Selectable
        from ..core.settings import effective

        regras = _selection_of(self.config, self.settings, workspace_id)
        linhas = []
        for t in self.read.tasks(workspace_id, None):
            v = regras.evaluate(
                Selectable(title=t.title, key=t.key, project=t.project,
                           status=t.external_status, labels=list(t.labels),
                           priority=t.origin_priority),
                base_priority=t.origin_priority)
            linhas.append({
                "key": t.key, "title": t.title,
                "external_status": t.external_status,
                "internal_state": t.state.name,
                "eligible": v.eligible, "priority": v.priority,
                "why": list(v.reasons), "excluded_by": v.excluded_by,
                "applied": t.priority == v.priority and t.eligible == v.eligible,
            })
        # A MESMA ordem do scheduler: prioridade, depois chave. Uma previa que
        # ordenasse diferente do motor seria pior que nenhuma.
        elegiveis = sorted((l for l in linhas if l["eligible"]),
                           key=lambda l: (l["priority"], l["key"]))
        fora = sorted((l for l in linhas if not l["eligible"]),
                      key=lambda l: l["key"])
        return Response(200, {"queue": elegiveis, "excluded": fora,
                              "discovered": len(linhas)})

    def _decide(self, parts: list[str], body: dict | None,
                who: Principal) -> Response:
        """`POST /api/workspaces/{id}/approvals/{id}/decision`.

        O que este metodo NAO faz e o ponto: nao verifica identidade, nao
        verifica escopo, nao consulta policy e nao olha estado. Tudo isso e do
        Core, e repetir qualquer uma dessas verificacoes aqui criaria uma
        segunda regra que um dia discorda da primeira -- sendo a daqui a que
        ninguem lembra de atualizar.

        A API valida a FORMA da requisicao e traduz a recusa para HTTP.
        """
        if (len(parts) != 6 or parts[:2] != ["api", "workspaces"]
                or parts[3] != "approvals" or parts[5] != "decision"):
            return _error(405, "read_only",
                          "esta API e somente leitura; autoridade de escrita "
                          "pertence ao motor e ao humano, nao a uma tela")

        if self.decisions is None:
            return _error(403, "no_authority",
                          "esta composicao nao concede autoridade de decisao")

        workspace_id, approval_id = parts[2], parts[4]
        if not isinstance(body, dict):
            return _error(400, "invalid_body", "corpo precisa ser um objeto JSON")
        choice = body.get("choice")
        note = body.get("note") or ""
        if not isinstance(choice, str) or not choice.strip():
            return _error(400, "invalid_body", "'choice' e obrigatorio")
        if not isinstance(note, str) or len(note) > 2000:
            return _error(400, "invalid_body",
                          "'note' precisa ser texto de ate 2000 caracteres")

        # O corpo NAO pode dizer quem esta decidindo. Aceitar isso seria
        # trocar autenticacao por digitacao.
        for forbidden in ("principal", "subject", "decided_by", "per", "actor",
                          "workspace_id", "method"):
            if forbidden in body:
                return _error(400, "invalid_body",
                              f"'{forbidden}' nao e aceito: identidade e escopo "
                              f"nao vem do corpo da requisicao")

        outcome = self.decisions.decide(who, workspace_id, approval_id,
                                        choice.strip(), note)
        if outcome.accepted:
            # A resposta descreve o que foi persistido e NAO substitui a
            # leitura: a tela le de novo antes de mostrar.
            return Response(200, {
                "accepted": True, "reason": outcome.reason,
                "approval_id": outcome.approval_id,
                "task_key": outcome.task_key, "choice": outcome.choice,
                "decided_by": outcome.decided_by,
                "decided_at": outcome.decided_at,
                "previous_state": outcome.previous_state,
                "new_state": outcome.new_state,
                "task_state": outcome.task_state,
                "next": "o proximo tick retoma a task a partir daqui"})
        return _error(DENIAL_STATUS.get(outcome.denial, 403),
                      (outcome.denial.value.lower() if outcome.denial
                       else "denied"), outcome.reason)

    def _access_write(self, method: str, parts: list[str], body: dict | None,
                      who: Principal) -> Response:
        """`POST .../access` concede; `DELETE .../access/{principal}` revoga.

        Como no caminho de decisao, este metodo NAO verifica identidade, escopo,
        policy nem autoridade. Ele valida a forma e traduz a recusa. Repetir uma
        verificacao aqui criaria uma segunda regra que um dia discorda da
        primeira -- sendo a daqui a que ninguem lembra de atualizar.
        """
        if self.access is None:
            return _error(403, "no_authority",
                          "esta composicao nao administra acesso")
        workspace_id = parts[2]

        if method == "DELETE":
            if len(parts) != 5:
                return _error(405, "read_only", "rota inexistente")
            alvo = self._ref(parts[4])
            if alvo is None:
                return _error(422, "invalid", "identidade alvo invalida")
            return self._as_response(
                self.access.revoke(who, workspace_id, alvo))

        if len(parts) != 4 or not isinstance(body, dict):
            return _error(400, "invalid_body", "corpo precisa ser um objeto JSON")

        # O corpo nao diz quem esta concedendo, nem onde. Aceitar isso seria
        # trocar autenticacao e escopo por digitacao.
        for proibido in ("actor", "granted_by", "principal_actor", "method",
                         "workspace_id", "client_id", "abilities"):
            if proibido in body:
                return _error(400, "invalid_body",
                              f"'{proibido}' nao e aceito: ator, escopo e "
                              f"capacidades nao vem do corpo da requisicao")

        alvo = self._ref(body.get("principal"))
        if alvo is None:
            return _error(422, "invalid",
                          "'principal' precisa ter a forma provedor:sujeito")
        papel = body.get("role")
        if not isinstance(papel, str) or not papel.strip():
            return _error(400, "invalid_body", "'role' e obrigatorio")
        nota = body.get("note") or ""
        if not isinstance(nota, str) or len(nota) > 2000:
            return _error(400, "invalid_body", "'note' e texto de ate 2000")

        return self._as_response(
            self.access.grant(who, workspace_id, alvo, papel.strip(), nota))

    @staticmethod
    def _ref(raw) -> "PrincipalRef | None":
        """Le `provedor:sujeito`, ou admite que nao da para ler.

        Uma identidade sem provedor seria uma chave ambigua, e uma chave
        ambigua casa com quem nao devia.
        """
        if not isinstance(raw, str) or ":" not in raw:
            return None
        try:
            return PrincipalRef.parse(raw.strip())
        except ValueError:
            return None

    def _as_response(self, outcome) -> Response:
        if outcome.accepted:
            return Response(200, {
                "accepted": True, "reason": outcome.reason,
                "actor": outcome.actor, "target": outcome.target,
                "grant": _grant_dict(outcome.grant) if outcome.grant else None})
        return _error(REFUSAL_STATUS.get(outcome.refusal, 403),
                      outcome.refusal.value.lower() if outcome.refusal
                      else "refused", outcome.reason)

    def _credential_write(self, method: str, parts: list[str],
                          body: dict | None, who: Principal) -> Response:
        """`POST .../credentials` registra; `DELETE .../credentials/{id}` revoga.

        Nao existe rota que devolva material secreto, e a ausencia nao e uma
        lacuna: uma tela nunca precisa do valor para administrar a autoridade
        dele. `POST .../credentials/{id}/test` prova a credencial contra o
        provedor e devolve quatro fatos -- nenhum deles o segredo.
        """
        if self.credentials is None:
            return _error(403, "no_authority",
                          "esta composicao nao administra credenciais")
        workspace_id = parts[2]

        if method == "DELETE":
            if len(parts) != 5:
                return _error(405, "read_only", "rota inexistente")
            return self._credential_response(
                self.credentials.revoke(who, workspace_id, parts[4],
                                        reason=(body or {}).get("reason", "")
                                        if isinstance(body, dict) else ""))

        if len(parts) == 6 and parts[5] == "test":
            # A sonda continua sendo da COMPOSICAO -- a API nao escolhe qual
            # usar, ela recebe uma pronta. Sem sonda entregue, a recusa do marco
            # 15 continua valendo palavra por palavra.
            if self.probe_for is None:
                return _error(501, "no_probe",
                              "esta composicao nao entregou sonda; o teste roda "
                              "pelo terminal: `regente credentials testar`")
            from ..core.credential import Use

            provider = str((body or {}).get("provider") or "")
            try:
                uso = Use(str((body or {}).get("use") or ""))
            except ValueError:
                return _error(400, "invalid_argument",
                              f"uso invalido; use um de "
                              f"{', '.join(u.value for u in Use)}")
            r = self.credentials.test_connection(
                who, workspace_id, provider, uso, self.probe_for(provider))
            # Quatro fatos separados, e nenhum deles o segredo. Reduzir a
            # "erro de conexao" apagaria a diferenca entre "a credencial nao
            # serve" e "nao deu para perguntar".
            return Response(200, {
                "authorized": r.authorized, "reach": r.reach.value,
                "capability_supported": r.capability_supported,
                "usable": r.usable, "detail": r.detail,
                "refusal": r.refusal.value if r.refusal else ""})

        if len(parts) != 4 or not isinstance(body, dict):
            return _error(400, "invalid_body", "corpo precisa ser um objeto JSON")

        # Nem ator, nem escopo, nem material vem do corpo.
        for proibido in ("actor", "granted_by", "workspace_id", "client_id",
                         "secret", "material", "token", "value"):
            if proibido in body:
                return _error(400, "invalid_body",
                              f"'{proibido}' nao e aceito: ator, escopo e "
                              f"material secreto nao vem da requisicao")

        nome = body.get("name")
        provider = body.get("provider")
        referencia = body.get("secret_ref")
        if not all(isinstance(x, str) and x.strip()
                   for x in (nome, provider, referencia)):
            return _error(400, "invalid_body",
                          "'name', 'provider' e 'secret_ref' sao obrigatorios")

        expira = None
        dias = body.get("expires_in_days")
        if isinstance(dias, (int, float)) and dias > 0:
            expira = self.read.clock() + timedelta(days=float(dias))

        return self._credential_response(self.credentials.register(
            who, workspace_id, name=nome.strip(), provider=provider.strip(),
            secret_ref=referencia.strip(),
            capabilities=body.get("capabilities") or [],
            kind=str(body.get("kind") or "token"),
            expires_at=expira, note=str(body.get("note") or "")))

    def _credential_response(self, outcome) -> Response:
        if outcome.accepted:
            return Response(200, {
                "accepted": True, "reason": outcome.reason,
                "actor": outcome.actor, "target": outcome.target,
                "credential": (_credential_dict(outcome.credential,
                                                self.read.clock())
                               if outcome.credential else None)})
        return _error(CREDENTIAL_STATUS.get(outcome.refusal, 403),
                      outcome.refusal.value.lower() if outcome.refusal
                      else "refused", outcome.reason)

    def _global_health(self, who: Principal) -> Response:
        """Saude de cada workspace visivel, sem agregado que esconda.

        Nao existe um numero unico aqui de proposito. Somar a saude de varios
        workspaces produziria um verde que sobrevive a um cliente parado, e e
        exatamente esse cliente que precisa aparecer.
        """
        rows = [w for w in self.read.workspaces() if who.may_read(w.id)]
        worst = "OK"
        rank = {"OK": 0, "ATTENTION": 1, "UNKNOWN": 2, "STUCK": 3}
        for w in rows:
            if rank.get(w.health, 0) > rank.get(worst, 0):
                worst = w.health
        return Response(200, {
            "level": worst,
            "workspaces": [w.as_dict() for w in rows],
            # Quem sou eu, como fui provado, e o que me foi concedido. E a
            # unica resposta que descreve o LEITOR e nao o motor -- e a que
            # torna impossivel confundir "nao ha nada" com "nao posso ver".
            "identity": {
                "subject": who.subject,
                "display": who.display,
                "method": who.method or "nao autenticado",
                "authenticated": who.authenticated,
                "mechanism": self.identity_note,
                "development_only": self.identity_is_development,
                "issuer": who.issuer,
                "authenticated_at": who.authenticated_at,
                "reads": (None if who.workspaces is None
                          else sorted(who.workspaces)),
                # O que foi CONCEDIDO, por workspace. A tela usa isto para
                # esconder o que nao adianta oferecer -- e esconder e UX; a
                # barreira continua sendo a API.
                "abilities": {w: sorted(a.value for a in abs_)
                              for w, abs_ in who.abilities.items()},
                "decides": sorted(who.decides),
            }})

    def _visible(self, client, who: Principal) -> bool:
        return any(who.may_read(w.id) for w in client.workspaces)

    # ------------------------------------------------------------------
    def _static(self, parts: list[str]) -> Response:
        """Arquivos da Mission Control, de uma lista fechada de extensoes.

        Qualquer parte suspeita e recusada antes de tocar o disco, e o caminho
        resolvido e conferido contra a raiz: um `..` que escapa vira leitura de
        arquivo arbitrario no servidor.
        """
        if any(p in ("..", ".") or "\\" in p or ":" in p for p in parts):
            return _not_found("arquivo")

        target = self.ui_root.joinpath(*parts) if parts else self.ui_root / "index.html"
        if target.is_dir():
            target = target / "index.html"

        try:
            resolved = target.resolve()
            resolved.relative_to(self.ui_root.resolve())
        except (ValueError, OSError):
            return _not_found("arquivo")

        kind = UI_TYPES.get(resolved.suffix.lower())
        if kind is None or not resolved.is_file():
            return _not_found("arquivo")

        data = resolved.read_bytes()
        if resolved.name == "index.html" and self.session_token:
            data = data.replace(b"{{SESSION_TOKEN}}",
                                self.session_token.encode("utf-8"))
        return Response(200, content_type=kind, body=data)


#: Os papeis de provider que a tela mostra, e se cada um precisa de credencial.
#: `workspace_provider` e local -- uma pasta ou um clone -- e por isso nao pede
#: credencial nenhuma; dizer que pede faria a tela cobrar o que nao existe.
PROVIDER_ROLES: tuple[tuple[str, bool], ...] = (
    ("tasks", True), ("repository", True), ("repository_write", True),
    ("cicd", True), ("runner", True), ("workspace_provider", False),
)

#: Como cada recusa do servico de configuracao vira HTTP.
SETTINGS_STATUS: dict[str, int] = {
    "UNAUTHENTICATED": 401, "NOT_FOUND": 404,
    "POLICY_DENIED": 403, "INVALID": 400,
}


def _selection_of(config, settings, workspace_id):
    """As regras EFETIVAS: arquivo, com o que a tela sobrepos.

    A mesma composicao que `apply_overlay` faz para o motor. Se a previa lesse
    so o arquivo, ela mostraria uma ordem e o tick produziria outra.
    """
    from ..core.selection import Selection, rules_from
    from ..core.settings import effective

    overlay = settings.overlay(workspace_id) if settings is not None else None
    do_arquivo = _file_values(config).get("selection")
    campo = effective("selection", do_arquivo, overlay)
    if not campo.value:
        return Selection()
    try:
        return rules_from(campo.value)
    except ValueError:
        # Regras invalidas nao derrubam a previa: ela mostra a ordem sem elas,
        # e o editor acima ja recusou o que nao presta na hora de salvar.
        return Selection()


def _file_values(config) -> dict:
    """O que o ARQUIVO diz, nas chaves sobreponiveis. Vazio sem config."""
    if config is None:
        return {}
    provedores = {
        k: {"name": v.name, **dict(v.options)}
        for k, v in (getattr(config, "providers", {}) or {}).items()}
    tasks = (getattr(config, "providers", {}) or {}).get("tasks")
    return {
        "providers": provedores,
        "status_map": dict(tasks.options.get("status_map") or {}) if tasks else {},
        "selection": [
            {"name": r.name, "field": r.field_name, "match": r.match.value,
             "value": r.value, "effect": r.effect.value, "delta": r.delta}
            for r in getattr(getattr(config, "selection", None), "rules", ())],
    }


def _connection_state(nome, precisa, deste, viva, agora) -> str:
    """O estado de um provider. Nunca "conectado" por existir configuracao."""
    from ..core.credential import Status

    if not nome:
        return "NAO_CONFIGURADO"
    if not precisa:
        return "PRONTO"
    if not deste:
        return "SEM_CREDENCIAL"
    if viva:
        # PRONTO nao e "conectado": e "ha credencial viva com capacidade".
        # Provar contra o provedor exige o teste de conexao, e ele e outra
        # pergunta -- a tela mostra as duas separadas.
        return "PRONTO"
    if any(c.status(agora) is Status.REVOKED for c in deste):
        return "REVOGADA"
    if any(c.status(agora) is Status.EXPIRED for c in deste):
        return "EXPIRADA"
    return "SEM_CREDENCIAL"


#: Como cada recusa do servico de operacao vira HTTP. Tabela de traducao, e
#: nao classificacao: quem decide e o motor, e a API so escolhe o numero.
OPERATION_STATUS: dict[str, int] = {
    "UNAUTHENTICATED": 401,
    "NOT_FOUND": 404,
    "POLICY_DENIED": 403,
    "INVALID": 400,
}


def _credential_dict(credential, at) -> dict:
    """Uma credencial, dita para fora.

    `secret_ref` entra porque e um ENDERECO -- `helper:github` diz onde
    procurar e nao vale nada para quem nao esta nesta maquina. O material nunca
    entra, e nao ha caminho aqui que o alcance: ele nao esta no objeto.
    """
    return {
        "id": credential.id,
        "name": credential.name,
        "provider": credential.provider,
        "kind": credential.kind,
        "secret_ref": credential.secret_ref.text,
        "status": credential.status(at).value,
        "capabilities": sorted(u.value for u in credential.capabilities),
        "granted_by": credential.granted_by,
        "granted_at": credential.granted_at,
        "expires_at": credential.expires_at,
        "revoked_by": credential.revoked_by,
        "revoked_at": credential.revoked_at,
        "note": credential.note,
    }


def _grant_dict(grant) -> dict:
    """Uma concessao, dita para fora. Com historia, nunca um booleano."""
    return {
        "id": grant.id,
        "principal": grant.principal.key,
        "provider": grant.principal.provider,
        "abilities": sorted(a.value for a in grant.abilities),
        "granted_by": grant.granted_by,
        "granted_at": grant.granted_at,
        "revoked_by": grant.revoked_by,
        "revoked_at": grant.revoked_at,
        "active": grant.active,
        "note": grant.note,
    }


def _one(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name) or []
    return values[0] if values else None


def _int(query: dict[str, list[str]], name: str, default: int) -> int:
    raw = _one(query, name)
    try:
        return max(1, min(1000, int(raw))) if raw else default
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

#: Tamanho maximo de um corpo aceito. Uma decisao e um objeto minusculo; ler
#: mais que isto so serviria para alguem encher a memoria do processo.
MAX_BODY = 64 * 1024


def handler_for(api: Api, identity: "IdentityProvider | None" = None,
                fallback: Principal | None = None
                ) -> type[BaseHTTPRequestHandler]:
    """O handler autentica a requisicao ANTES de entregar ao roteador.

    `fallback` e o principal usado quando nao ha provedor de identidade
    configurado -- o caso das leituras locais antes de M13 existir. Ele nunca
    decide: `Principal.decides` so e preenchido por um provedor.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "Regente"
        sys_version = ""

        def do_GET(self) -> None:      # noqa: N802 - assinatura da stdlib
            self._answer(self._resolve())

        def do_HEAD(self) -> None:     # noqa: N802
            self._answer(self._resolve(), body=False)

        # Os metodos de escrita sao respondidos por NOS, e nao pelo 501
        # generico da stdlib. A diferenca nao e cosmetica: "501 metodo nao
        # suportado" le-se como "ainda nao implementado", e alguem acabaria
        # implementando. A recusa precisa dizer que nao existe escrita aqui.
        def do_POST(self) -> None:     # noqa: N802
            self._answer(self._resolve())

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

        def _authorize(self, who: Principal) -> Principal:
            """Autenticado primeiro, autorizado depois -- nunca junto.

            O provedor diz quem e; as concessoes gravadas dizem o que pode. E
            por isso que revogar fecha a porta na requisicao seguinte, sem
            depender de a tela esconder um botao.
            """
            if api.access is None or not who.authenticated:
                return who
            return api.access.authorize(who)

        # ---- identidade ------------------------------------------------
        def _principal(self) -> Principal:
            """Do cabecalho para um principal, ou anonimo.

            O cliente apresenta um SEGREDO; ele nao declara um nome. Nenhum
            campo desta requisicao escolhe quem e o portador -- quem escolhe e o
            provedor, a partir do que o segredo prova.
            """
            if identity is None:
                return fallback if fallback is not None else ANONYMOUS
            raw = self.headers.get("Authorization") or ""
            token = raw[7:].strip() if raw[:7].lower() == "bearer " else ""
            found = identity.authenticate(token or None)
            if found is None:
                return ANONYMOUS
            return self._authorize(identity.principal(found))

        def _body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > MAX_BODY:
                return None
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, OSError):
                return None

        def _resolve(self) -> Response:
            url = urlsplit(self.path)
            body = self._body() if self.command == "POST" else None
            return api.resolve(self.command, url.path,
                               parse_qs(url.query), self._principal(), body)

        def _answer(self, response: Response, body: bool = True) -> None:
            data = response.rendered()
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(data)))
            # A Mission Control e servida por este mesmo processo; nao ha
            # origem cruzada legitima. Sem CORS aberto, um site qualquer no
            # navegador do operador nao le o estado dos clientes dele.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(data)

        def log_message(self, fmt: str, *args: Any) -> None:
            """Silencio por padrao: o log de acesso da stdlib vai para stderr e
            se mistura com a saida do motor. Quem quiser observabilidade da API
            tem os eventos do proprio motor."""

    return Handler


def serve(read: ReadModel, host: str = "127.0.0.1", port: int = 8787,
          principal: Principal | None = None,
          identity: "IdentityProvider | None" = None,
          decisions: DecisionService | None = None,
          access: AccessService | None = None,
          credentials: CredentialService | None = None,
          operations: "OperationService | None" = None,
          settings: "SettingsService | None" = None,
          probe_for: object | None = None,
          config: object | None = None,
          needs_credential: object | None = None,
          catalog: dict | None = None,
          resources: "ResourceService | None" = None,
          discovery_trees: dict | None = None,
          session_token: str = "",
          read_only: bool = False) -> ThreadingHTTPServer:
    """Sobe o servidor. Loopback por padrao, e isso continua sendo uma decisao.

    Com `identity`, toda requisicao e autenticada e a escrita passa a existir.
    Sem ele, o servidor volta ao comportamento de leitura do marco anterior: um
    principal fixo, que nao decide nada -- `decides` so e preenchido por um
    provedor de identidade.
    """
    mimetypes.init()
    api = Api(read=read, decisions=decisions, access=access,
              credentials=credentials, operations=operations,
              settings=settings, probe_for=probe_for, config=config,
              needs_credential=needs_credential, catalog=catalog,
              resources=resources, discovery_trees=discovery_trees,
              read_only=read_only, session_token=session_token,
              identity_note=(identity.describe() if identity is not None
                             else "sem provedor de identidade"),
              identity_is_development=bool(
                  identity is not None and identity.development_only))
    return ThreadingHTTPServer(
        (host, port), handler_for(api, identity, principal))

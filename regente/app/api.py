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

        if head == "escalations" and not tail:
            return Response(200, {"escalations": [
                e.as_dict() for e in self.read.escalations(workspace_id)]})

        return _not_found("recurso")

    # ------------------------------------------------------------------
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
            return _error(501, "no_probe",
                          "o teste de conexao roda pelo terminal: "
                          "`regente credentials testar`. A sonda pertence a "
                          "composicao, e a API nao escolhe qual usar")

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
              credentials=credentials,
              read_only=read_only, session_token=session_token,
              identity_note=(identity.describe() if identity is not None
                             else "sem provedor de identidade"),
              identity_is_development=bool(
                  identity is not None and identity.development_only))
    return ThreadingHTTPServer(
        (host, port), handler_for(api, identity, principal))

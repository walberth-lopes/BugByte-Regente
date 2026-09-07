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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..engine.readmodel import ReadModel

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
class Principal:
    """Quem esta perguntando, e o que pode ver.

    Existe agora, com uma implementacao mínima, porque a fronteira e o que fica
    dificil de acrescentar depois. Uma API que nasce sem nocao de identidade
    espalha `workspace_id` vindo do cliente por toda parte, e o dia em que
    alguem precisa restringir descobre que nao ha onde.

    `workspaces=None` significa "todos os que o store guarda" -- o operador
    local, que e o unico caso desta versao. Restringir depois e trocar esse
    campo, nao reescrever as rotas.
    """
    name: str = "local"
    workspaces: frozenset[str] | None = None

    def may_read(self, workspace_id: str) -> bool:
        return self.workspaces is None or workspace_id in self.workspaces


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

    # ------------------------------------------------------------------
    def resolve(self, method: str, path: str,
                query: dict[str, list[str]] | None = None,
                principal: Principal | None = None) -> Response:
        who = principal or Principal()
        query = query or {}

        if method not in ("GET", "HEAD"):
            # Nao e "ainda nao implementado". Nao existe escrita nesta API.
            return _error(405, "read_only",
                          "esta API e somente leitura; autoridade de escrita "
                          "pertence ao motor e ao humano, nao a uma tela")

        parts = [unquote(p) for p in path.strip("/").split("/") if p]

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

        if head == "escalations" and not tail:
            return Response(200, {"escalations": [
                e.as_dict() for e in self.read.escalations(workspace_id)]})

        return _not_found("recurso")

    # ------------------------------------------------------------------
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
        return Response(200, {"level": worst,
                              "workspaces": [w.as_dict() for w in rows]})

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
        return Response(200, content_type=kind, body=resolved.read_bytes())


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

def handler_for(api: Api, principal: Principal) -> type[BaseHTTPRequestHandler]:
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

        def _resolve(self) -> Response:
            url = urlsplit(self.path)
            return api.resolve(self.command, url.path,
                               parse_qs(url.query), principal)

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
          principal: Principal | None = None) -> ThreadingHTTPServer:
    """Sobe o servidor. Loopback por padrao, e isso e uma decisao.

    Esta versao nao tem autenticacao: o principal e o operador local. Abrir para
    a rede sem autenticar exporia o estado de todos os clientes a quem alcancar
    a porta, entao o default nao abre. Quem precisar expor precisa antes trocar
    o `Principal` por um que venha de uma identidade real -- e a fronteira ja
    existe para isso.
    """
    mimetypes.init()
    api = Api(read=read)
    return ThreadingHTTPServer((host, port),
                               handler_for(api, principal or Principal()))

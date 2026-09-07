# -*- coding: utf-8 -*-
"""Um servidor git HTTP de verdade, no loopback, exigindo Basic auth.

Existe porque o caminho de entrega foi provado por cinco marcos contra um dublê
-- e o dublê nao tem a linha que derrubava o push real. Um `FakeAreas` responde
o que o teste combinou; um `git push` de verdade responde o que o `git` faz.

Serve `git http-backend`, que e o proprio servidor do git. O que este arquivo
acrescenta e uma porta trancada: sem o cabecalho `Authorization` esperado, 401.

Guarda todo cabecalho `Authorization` que chegou -- e a evidencia de que o
material atravessou de verdade, e nao de que alguem montou um dicionario.
"""

from __future__ import annotations

import base64
import http.server
import os
import subprocess
import threading
from pathlib import Path


class GitServer:
    """Um repositorio nu servido por HTTP, atras de Basic auth."""

    def __init__(self, root: Path, user: str, token: str, name: str = "thing.git"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.user = user
        self.token = token
        self.esperado = "Basic " + base64.b64encode(
            f"{user}:{token}".encode()).decode()
        #: Todo `Authorization` visto, na ordem. `""` quando nao veio nenhum.
        self.authorizations: list[str] = []
        #: Todo caminho pedido. Vazio prova que nada saiu da maquina.
        self.requests: list[str] = []

        self.bare = self.root / name
        _git("init", "--bare", "-q", str(self.bare))
        _git("config", "http.receivepack", "true", cwd=self.bare)

        self._servidor = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _handler(self))
        self._servidor.daemon_threads = True
        self.port = self._servidor.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/{name}"
        threading.Thread(target=self._servidor.serve_forever, daemon=True).start()

    # ---- o que o mundo diz que aconteceu --------------------------------

    def refs(self) -> dict[str, str]:
        """As refs que o repositorio nu REALMENTE tem. A prova do push."""
        saida = _git("for-each-ref", "--format=%(refname)\t%(objectname)",
                     cwd=self.bare)
        return {l.split("\t")[0]: l.split("\t")[1]
                for l in saida.splitlines() if "\t" in l}

    @property
    def untouched(self) -> bool:
        """Nada chegou: nenhuma requisicao, nenhuma ref."""
        return not self.requests and not self.refs()

    def close(self) -> None:
        self._servidor.shutdown()
        self._servidor.server_close()


def _git(*args: str, cwd: Path | None = None) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                       capture_output=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {p.stderr}")
    return p.stdout


def _handler(servidor: GitServer):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def handle_error(self, *a):
            # O `git` fecha a conexao assim que termina; o traco de pilha
            # resultante e ruido que faz um teste verde parecer quebrado.
            pass

        def _atender(self):
            auth = self.headers.get("Authorization", "")
            servidor.authorizations.append(auth)
            servidor.requests.append(self.path)

            if auth != servidor.esperado:
                corpo = b"nao autorizado\n"
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="git"')
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)
                return

            caminho, _, consulta = self.path.partition("?")
            tamanho = int(self.headers.get("Content-Length") or 0)
            entrada = self.rfile.read(tamanho) if tamanho else b""
            p = subprocess.run(
                ["git", "http-backend"], input=entrada, capture_output=True,
                env={"GIT_PROJECT_ROOT": str(servidor.root),
                     "GIT_HTTP_EXPORT_ALL": "1",
                     "REQUEST_METHOD": self.command, "PATH_INFO": caminho,
                     "QUERY_STRING": consulta,
                     "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                     "CONTENT_LENGTH": str(tamanho),
                     "REMOTE_USER": servidor.user, "REMOTE_ADDR": "127.0.0.1",
                     "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                     "PATH": os.environ.get("PATH", "")})
            cabecalho, marca, corpo = p.stdout.partition(b"\r\n\r\n")
            if not marca:
                cabecalho, _, corpo = p.stdout.partition(b"\n\n")
            self.send_response(200)
            for linha in cabecalho.replace(b"\r", b"").split(b"\n"):
                if b":" in linha:
                    k, _, v = linha.partition(b":")
                    if k.strip().lower() != b"status":
                        self.send_header(k.decode().strip(), v.decode().strip())
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        do_GET = do_POST = _atender

    return H


def a_work_area(root: Path, remote: str, name: str = "fonte") -> Path:
    """Um clone de origem, com um commit, pronto para o provedor de area."""
    fonte = Path(root) / name
    fonte.mkdir(parents=True)
    for args in (("init", "-q", "-b", "main"),
                 ("config", "user.email", "t@example.invalid"),
                 ("config", "user.name", "T")):
        _git(*args, cwd=fonte)
    (fonte / "a.txt").write_text("um\n", encoding="utf-8")
    _git("add", "-A", cwd=fonte)
    _git("commit", "-q", "-m", "init", cwd=fonte)
    return fonte

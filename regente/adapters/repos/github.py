# -*- coding: utf-8 -*-
"""RepositoryProvider sobre a hospedagem remota, via CLI oficial. SOMENTE LEITURA.

Deliberadamente do tipo mais diferente possivel do adapter local: processo contra
API remota, autenticacao delegada, paginacao, limite de taxa, indisponibilidade.
Se o contrato do `RepositoryProvider` vale para os dois, ele vale.

**Somente leitura por construcao.** Toda invocacao passa por `_cli`, que recusa
qualquer subcomando fora de uma lista fechada, e recusa explicitamente qualquer
metodo HTTP diferente de GET quando a chamada e via `api`. Escrever exigiria
adicionar um verbo a lista -- mudanca visivel, revisavel, deliberada.

**Credencial nunca passa por aqui.** A CLI resolve a propria autenticacao com o
que o sistema ja tem. O adapter nunca ve, nunca carrega e nunca registra token --
o que tambem significa que uma falha de autenticacao aparece como error tipado, e
nao como lista vazia.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from ...ports import AdapterError, ReadOnlyRefused
from ...ports.repository import (READ_CAPS, Branch, RepoCapability, RepoInfo, RepoRef,
                                 RepositoryProvider)
from ..tasks.transport import (Call, AuthFailure, RateLimited,
                                NotFound, Observer, ProviderUnavailable,
                                MalformedResponse)
from ...core.credential import Use
from ...ports.support import CredentialBroker
from .cli_process import DEFAULT_CREDENTIAL_ENV
from . import cli_process
from .readonly import cli_is_read


#: Campos pedidos numa LISTAGEM. Enxuto: uma organizacao com centenas de
#: repositorios devolve megabytes se cada item trouxer tudo.
LIST_FIELDS = "name,nameWithOwner,defaultBranchRef,isArchived,isPrivate,url"

#: Campos do DETALHE. Pagos um repositorio por vez.
DETAIL_FIELDS = LIST_FIELDS + ",description,sshUrl,pushedAt,primaryLanguage"


@dataclass(slots=True)
class GitHubRepos(RepositoryProvider):
    """Le repositorios de uma organizacao na hospedagem remota."""

    org: str
    cli_path: str = "gh"
    name: str = "github"
    capabilities: frozenset[RepoCapability] = field(default_factory=lambda: READ_CAPS)
    observer: Observer | None = None
    timeout: int = 60
    list_limit: int = 200
    #: A porta governada, ja presa a quem age, a que workspace e a que provider.
    #: `None` significa: este adapter nao tem credencial -- e entao ele RECUSA,
    #: em vez de procurar uma no ambiente. Era exatamente essa procura que
    #: deixava a ferramenta se autenticar sozinha pelo chaveiro do sistema.
    credentials: CredentialBroker | None = None
    #: Sob que nome o material entra no processo filho. Da configuracao do
    #: workspace, com o default do fornecedor -- nunca de uma constante do motor.
    credential_env: tuple[str, ...] = DEFAULT_CREDENTIAL_ENV
    #: Onde a ferramenta procura a configuracao DELA. Vazio usa um caminho que
    #: nao existe, que e o que fecha a credencial propria.
    config_dir: str = ""

    def _launch(self, use):
        """O ambiente deste filho. Uma linha, um lugar, nenhuma alternativa."""
        ambiente = cli_process.child_environment(
            self.credential_env, self.credentials, self.config_dir)
        return ambiente.launch(use) if use is not None else ambiente.plain()

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name,
                "modo": "somente-leitura", "org": self.org,
                "credencial": "governada" if self.credentials else "nenhuma"}

    def verify(self) -> None:
        """Prova que a ferramenta EXISTE E RODA -- nao que ela esta autenticada.

        Eram a mesma checagem, e nao sao a mesma pergunta. Autenticacao se prova
        por `regente credentials testar`, que responde quatro fatos separados.
        """
        cli_process.refuse_if_config_reachable(self.config_dir)
        self._cli(["--version"], json_esperado=False, use=None)

    # ---- execucao --------------------------------------------------------

    def _cli(self, args: list[str], json_esperado: bool = True,
             use: Use | None = Use.REPO_READ) -> Any:
        # Por INVOCACAO INTEIRA, nao por verbo. `repo list` le; `repo delete`
        # apaga, e os dois comecam com `repo` -- foi assim que um `repo delete`
        # atravessou o portao em 06/09/2026.
        ok, reason = cli_is_read(args)
        if not ok:
            raise ReadOnlyRefused(
                f"'{self.cli_path} {' '.join(args)}' recusado: {reason}. "
                f"Nenhuma mutacao e executavel neste marco.")

        # Resolvido AQUI, imediatamente antes do processo, e nunca guardado.
        launch = self._launch(use)
        inicio = time.monotonic()
        try:
            p = subprocess.run(
                [self.cli_path, *args], capture_output=True, env=launch.env,
                encoding="utf-8", errors="replace", timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            self._notify_observer(args, inicio, False, None, f"timeout apos {self.timeout}s")
            raise ProviderUnavailable(f"cli estourou {self.timeout}s") from e
        except FileNotFoundError as e:
            self._notify_observer(args, inicio, False, None, "cli nao encontrada")
            raise AdapterError(f"'{self.cli_path}' nao esta no PATH") from e

        if p.returncode != 0:
            # Limpo ANTES de truncar: cortar primeiro pode deixar metade de um
            # token, e metade e a parte que ninguem deveria ter escrito.
            error = launch.scrub((p.stderr or "").strip())[:400]
            baixo = error.lower()
            self._notify_observer(args, inicio, False, None, error)
            # Traduzir a falha e o que permite o motor decidir: retentar,
            # esperar ou parar. Um error generico obriga a tratar tudo igual.
            if "authentication" in baixo or "not logged" in baixo or "401" in baixo:
                raise AuthFailure(f"credencial recusada: {error[:200]}")
            if "rate limit" in baixo or "429" in baixo:
                raise RateLimited(f"limite de taxa: {error[:200]}")
            if "not found" in baixo or "404" in baixo:
                raise NotFound(f"nao existe ou sem acesso: {error[:200]}")
            if any(m in baixo for m in ("timeout", "connection", "dial tcp",
                                        "no such host", "502", "503", "504")):
                raise ProviderUnavailable(f"indisponivel: {error[:200]}")
            raise AdapterError(f"cli rc={p.returncode}: {error}")

        self._notify_observer(args, inicio, True, 200, "")
        if not json_esperado:
            return p.stdout
        raw = (p.stdout or "").strip()
        if not raw:
            raise MalformedResponse("corpo vazio onde se esperava JSON")
        try:
            return json.loads(raw)
        except ValueError as e:
            raise MalformedResponse(f"saida nao e JSON: {raw[:200]}") from e

    def _notify_observer(self, args: list[str], inicio: float, ok: bool,
               status: int | None, error: str) -> None:
        if not self.observer:
            return
        self.observer(Call(
            operation="cli", path=" ".join(args[:2]),
            duration_ms=int((time.monotonic() - inicio) * 1000),
            success=ok, status=status,
            rate_limited="rate limit" in error.lower(), error=error[:200]))

    # ---- normalizacao ----------------------------------------------------

    def _build(self, raw: dict[str, Any], partial: bool) -> RepoInfo:
        key = raw.get("nameWithOwner") or ""
        if not key:
            raise AdapterError("repositorio sem identidade completa na resposta")
        base = ((raw.get("defaultBranchRef") or {}) or {}).get("name") or ""
        return RepoInfo(
            ref=RepoRef(provider=self.name, key=key),
            name=raw.get("name") or key.rsplit("/", 1)[-1],
            base_branch=base,
            clone_origin=f"https://github.com/{key}.git",
            url=raw.get("url"),
            archived=bool(raw.get("isArchived")),
            private=raw.get("isPrivate"),
            capabilities=self.capabilities,
            partial=partial,
            data={k: v for k, v in (
                ("descricao", raw.get("description")),
                ("empurrado_em", raw.get("pushedAt")),
                ("linguagem", (raw.get("primaryLanguage") or {}).get("name")),
            ) if v})

    # ---- descoberta ------------------------------------------------------

    def list_repositories(self, filtro: dict[str, Any] | None = None) -> list[RepoInfo]:
        f = filtro or {}
        args = ["repo", "list", self.org, "--limit", str(f.get("limite", self.list_limit)),
                "--json", LIST_FIELDS]
        if f.get("sem_arquivados", True):
            args.append("--no-archived")
        raw = self._cli(args)
        if not isinstance(raw, list):
            raise AdapterError(f"listagem devolveu {type(raw).__name__}, esperava lista")
        return [self._build(r, partial=True) for r in raw]

    def get_repository(self, key: str) -> RepoInfo:
        target = key if "/" in key else f"{self.org}/{key}"
        raw = self._cli(["repo", "view", target, "--json", DETAIL_FIELDS])
        if not isinstance(raw, dict):
            raise AdapterError(f"detalhe devolveu {type(raw).__name__}, esperava objeto")
        return self._build(raw, partial=False)

    def list_branches(self, key: str, filtro: dict[str, Any] | None = None) -> list[Branch]:
        target = key if "/" in key else f"{self.org}/{key}"
        base = self.get_repository(target).base_branch
        per_page = int((filtro or {}).get("por_pagina", 100))
        raw = self._cli(["api", f"repos/{target}/branches?per_page={per_page}"])
        if not isinstance(raw, list):
            raise AdapterError("listagem de branches devolveu forma inesperada")
        default_value = (filtro or {}).get("padrao", "")
        output = []
        for b in raw:
            name = b.get("name") or ""
            if default_value and default_value.lower() not in name.lower():
                continue
            output.append(Branch(name=name, sha=(b.get("commit") or {}).get("sha", ""),
                                e_base=(name == base)))
        return sorted(output, key=lambda x: x.name)

    def read_file(self, key: str, path: str, ref: str | None = None) -> str:
        import base64
        target = key if "/" in key else f"{self.org}/{key}"
        rota = f"repos/{target}/contents/{path}" + (f"?ref={ref}" if ref else "")
        raw = self._cli(["api", rota])
        if not isinstance(raw, dict) or "content" not in raw:
            raise AdapterError(f"{path} nao e um arquivo em {target}")
        return base64.b64decode(raw["content"]).decode("utf-8", "replace")

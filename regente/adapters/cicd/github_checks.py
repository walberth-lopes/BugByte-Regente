# -*- coding: utf-8 -*-
"""Observing CI on the hosted provider. Read only, by construction.

The whole adapter goes through the READ allowlist. There is no write path here
at all -- not a forbidden one, an absent one. Triggering, re-running and
cancelling a pipeline are mutations, and this milestone observes.

One rule shapes every method: **a failed read is never an empty result.** A
provider that cannot answer raises `ProviderUnavailable`. Returning "no checks"
because the request failed would let a network blip read as a clean bill of
health, which is the single most dangerous thing a CI observer can do.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from ...core.credential import Use
from ...ports import AdapterError
from ...ports.delivery import CICDProvider, Check, PipelineStatus
from ..repos import cli_process
from ..repos.readonly import cli_is_read
from ...ports.support import CredentialBroker
from ..tasks.transport import (AuthFailure, Call, MalformedResponse, NotFound,
                               Observer, ProviderUnavailable, RateLimited)


@dataclass(slots=True)
class GitHubChecks(CICDProvider):
    """Reads check runs for a SHA or a pull request."""

    org: str
    cli_path: str = "gh"
    name: str = "github-checks"
    observer: Observer | None = None
    timeout: int = 60
    #: A porta governada, ja presa a quem age, a que workspace e a que provider.
    #: `None` significa: este adapter nao tem credencial -- e entao ele RECUSA,
    #: em vez de procurar uma no ambiente. Era exatamente essa procura que
    #: deixava a ferramenta se autenticar sozinha pelo chaveiro do sistema.
    credentials: CredentialBroker | None = None
    #: Sob que nome o material entra no processo filho. Da configuracao do
    #: workspace, com o default do fornecedor -- nunca de uma constante do motor.
    credential_env: tuple[str, ...] = cli_process.DEFAULT_CREDENTIAL_ENV
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
                "mode": "observe-only", "org": self.org,
                "credential": "governed" if self.credentials else "none"}

    def verify(self) -> None:
        """The tool exists and runs. Authentication is a separate question."""
        cli_process.refuse_if_config_reachable(self.config_dir)
        self._cli(["--version"], json_expected=False, use=None)

    def _cli(self, args: list[str], json_expected: bool = True,
             use: Use | None = Use.CI_READ) -> Any:
        ok, reason = cli_is_read(args)
        if not ok:
            raise AdapterError(
                f"'{self.cli_path} {' '.join(args)}' refused: {reason}")

        # Resolved here, immediately before the process, and never held.
        launch = self._launch(use)
        started = time.monotonic()
        try:
            p = subprocess.run([self.cli_path, *args], capture_output=True,
                               encoding="utf-8", errors="replace",
                               env=launch.env, timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            self._notify(args, started, False, "timeout")
            raise ProviderUnavailable(f"cli exceeded {self.timeout}s") from e
        except FileNotFoundError as e:
            self._notify(args, started, False, "cli not found")
            raise AdapterError(f"'{self.cli_path}' is not on PATH") from e

        if p.returncode != 0:
            # Scrubbed before truncating: half a token is still the half nobody
            # should have written down.
            error = launch.scrub((p.stderr or "").strip())[:400]
            low = error.lower()
            self._notify(args, started, False, error)
            if "authentication" in low or "not logged" in low or "401" in low:
                raise AuthFailure(f"credential refused: {error[:200]}")
            if "rate limit" in low or "429" in low:
                raise RateLimited(f"rate limited: {error[:200]}")
            if "not found" in low or "404" in low:
                raise NotFound(f"absent or no access: {error[:200]}")
            # Everything else is treated as unavailability rather than as a CI
            # answer. An unexplained failure is the case where guessing "green"
            # is most tempting and most wrong.
            raise ProviderUnavailable(f"could not read checks: {error[:200]}")

        self._notify(args, started, True, "")
        raw = (p.stdout or "").strip()
        if not json_expected:
            return raw
        if not raw:
            raise MalformedResponse("empty body where JSON was expected")
        try:
            return json.loads(raw)
        except ValueError as e:
            raise MalformedResponse(f"output is not JSON: {raw[:200]}") from e

    def _notify(self, args: list[str], started: float, ok: bool, error: str) -> None:
        if not self.observer:
            return
        self.observer(Call(
            operation="cli", path=" ".join(args[:2]),
            duration_ms=int((time.monotonic() - started) * 1000),
            success=ok, status=200 if ok else None,
            rate_limited="rate limit" in error.lower(), error=error[:200]))

    # ---- observation -----------------------------------------------------

    def get_status(self, repo: str, reference: str) -> PipelineStatus:
        """`reference` is a commit SHA. Checks are read for that exact commit.

        A SHA and not a PR number: a PR's head moves, and asking about a PR
        answers about whatever is on it now, which may not be the commit the
        engine validated. Asking about a SHA cannot drift.
        """
        target = repo if "/" in repo else f"{self.org}/{repo}"
        raw = self._cli(["api",
                         f"repos/{target}/commits/{reference}/check-runs"
                         f"?per_page=100"])
        if not isinstance(raw, dict) or "check_runs" not in raw:
            raise MalformedResponse(
                f"check-runs returned an unexpected shape for {reference[:12]}")

        runs = raw.get("check_runs") or []
        checks = tuple(
            Check(name=c.get("name") or "?",
                  conclusion=(c.get("conclusion") or "").upper(),
                  state=(c.get("status") or "").upper(),
                  url=c.get("html_url") or "")
            for c in runs if isinstance(c, dict))

        # Zero check runs is a CONFIRMED absence: the call succeeded and the
        # provider said there are none. It is recorded as such and never as
        # success -- a repository with no checks has proved nothing about a
        # change, and in the reference implementation that was exactly where
        # security work lived.
        return PipelineStatus(
            reference=reference, checks=checks,
            confirmed_no_checks=not checks,
            data={"total": raw.get("total_count", len(runs))})

    def get_logs(self, repo: str, check: str, limit: int = 200) -> list[str]:
        target = repo if "/" in repo else f"{self.org}/{repo}"
        try:
            raw = self._cli(["api", f"repos/{target}/actions/jobs/{check}/logs"],
                            json_expected=False)
        except AdapterError:
            return []
        return str(raw).splitlines()[-limit:]

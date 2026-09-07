# -*- coding: utf-8 -*-
"""RepositoryProvider over the remote hosting, via the official CLI. READ ONLY.

Deliberately of the most different kind possible from the local adapter: a
process against a remote API, delegated authentication, pagination, rate
limiting, unavailability. If the `RepositoryProvider` contract holds for both, it
holds.

**Read-only by construction.** Every invocation goes through `_cli`, which
refuses any subcommand outside a closed list, and explicitly refuses any HTTP
method other than GET when the call goes via `api`. Writing would require adding
a verb to the list -- a visible, reviewable, deliberate change.

**Credentials never pass through here.** The CLI resolves its own authentication
with what the system already has. The adapter never sees, never carries and never
records a token -- which also means an authentication failure shows up as a typed
error, and not as an empty list.
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
from .readonly import cli_is_read


#: Fields requested in a LISTING. Trimmed: an organisation with hundreds of
#: repositories returns megabytes if every item carries everything.
LIST_FIELDS = "name,nameWithOwner,defaultBranchRef,isArchived,isPrivate,url"

#: Fields for the DETAIL. Paid for one repository at a time.
DETAIL_FIELDS = LIST_FIELDS + ",description,sshUrl,pushedAt,primaryLanguage"


@dataclass(slots=True)
class GitHubRepos(RepositoryProvider):
    """Reads repositories of an organisation on the remote hosting."""

    org: str
    cli_path: str = "gh"
    name: str = "github"
    capabilities: frozenset[RepoCapability] = field(default_factory=lambda: READ_CAPS)
    observer: Observer | None = None
    timeout: int = 60
    list_limit: int = 200

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name,
                "mode": "read-only", "org": self.org}

    def verify(self) -> None:
        """Proves authentication and reach with the cheapest call there is."""
        self._cli(["auth", "status"], expected_json=False)

    # ---- execution -------------------------------------------------------

    def _cli(self, args: list[str], expected_json: bool = True) -> Any:
        # Per WHOLE INVOCATION, not per verb. `repo list` reads; `repo delete`
        # deletes, and both start with `repo` -- that is how a `repo delete` got
        # through the gate on 06/09/2026.
        ok, reason = cli_is_read(args)
        if not ok:
            raise ReadOnlyRefused(
                f"'{self.cli_path} {' '.join(args)}' refused: {reason}. "
                f"No mutation is executable in this milestone.")

        started = time.monotonic()
        try:
            p = subprocess.run(
                [self.cli_path, *args], capture_output=True,
                encoding="utf-8", errors="replace", timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            self._notify_observer(args, started, False, None, f"timeout apos {self.timeout}s")
            raise ProviderUnavailable(f"cli timed out after {self.timeout}s") from e
        except FileNotFoundError as e:
            self._notify_observer(args, started, False, None, "cli not found")
            raise AdapterError(f"'{self.cli_path}' is not on the PATH") from e

        if p.returncode != 0:
            error = (p.stderr or "").strip()[:400]
            lowered = error.lower()
            self._notify_observer(args, started, False, None, error)
            # Translating the failure is what lets the engine decide: retry,
            # wait or stop. A generic error forces everything to be treated alike.
            if "authentication" in lowered or "not logged" in lowered or "401" in lowered:
                raise AuthFailure(f"credential refused: {error[:200]}")
            if "rate limit" in lowered or "429" in lowered:
                raise RateLimited(f"rate limit: {error[:200]}")
            if "not found" in lowered or "404" in lowered:
                raise NotFound(f"does not exist or no access: {error[:200]}")
            if any(m in lowered for m in ("timeout", "connection", "dial tcp",
                                        "no such host", "502", "503", "504")):
                raise ProviderUnavailable(f"unavailable: {error[:200]}")
            raise AdapterError(f"cli rc={p.returncode}: {error}")

        self._notify_observer(args, started, True, 200, "")
        if not expected_json:
            return p.stdout
        raw = (p.stdout or "").strip()
        if not raw:
            raise MalformedResponse("empty body where JSON was expected")
        try:
            return json.loads(raw)
        except ValueError as e:
            raise MalformedResponse(f"output is not JSON: {raw[:200]}") from e

    def _notify_observer(self, args: list[str], started: float, ok: bool,
               status: int | None, error: str) -> None:
        if not self.observer:
            return
        self.observer(Call(
            operation="cli", path=" ".join(args[:2]),
            duration_ms=int((time.monotonic() - started) * 1000),
            success=ok, status=status,
            rate_limited="rate limit" in error.lower(), error=error[:200]))

    # ---- normalisation ---------------------------------------------------

    def _build(self, raw: dict[str, Any], partial: bool) -> RepoInfo:
        key = raw.get("nameWithOwner") or ""
        if not key:
            raise AdapterError("repository without a complete identity in the response")
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
                ("description", raw.get("description")),
                ("pushed_at", raw.get("pushedAt")),
                ("language", (raw.get("primaryLanguage") or {}).get("name")),
            ) if v})

    # ---- discovery -------------------------------------------------------

    def list_repositories(self, filters: dict[str, Any] | None = None) -> list[RepoInfo]:
        f = filters or {}
        args = ["repo", "list", self.org, "--limit", str(f.get("limit", self.list_limit)),
                "--json", LIST_FIELDS]
        if f.get("no_archived", True):
            args.append("--no-archived")
        raw = self._cli(args)
        if not isinstance(raw, list):
            raise AdapterError(f"listing returned {type(raw).__name__}, expected a list")
        return [self._build(r, partial=True) for r in raw]

    def get_repository(self, key: str) -> RepoInfo:
        target = key if "/" in key else f"{self.org}/{key}"
        raw = self._cli(["repo", "view", target, "--json", DETAIL_FIELDS])
        if not isinstance(raw, dict):
            raise AdapterError(f"detail returned {type(raw).__name__}, expected an object")
        return self._build(raw, partial=False)

    def list_branches(self, key: str, filters: dict[str, Any] | None = None) -> list[Branch]:
        target = key if "/" in key else f"{self.org}/{key}"
        base = self.get_repository(target).base_branch
        per_page = int((filters or {}).get("per_page", 100))
        raw = self._cli(["api", f"repos/{target}/branches?per_page={per_page}"])
        if not isinstance(raw, list):
            raise AdapterError("branch listing returned an unexpected shape")
        default_value = (filters or {}).get("default", "")
        output = []
        for b in raw:
            name = b.get("name") or ""
            if default_value and default_value.lower() not in name.lower():
                continue
            output.append(Branch(name=name, sha=(b.get("commit") or {}).get("sha", ""),
                                is_base=(name == base)))
        return sorted(output, key=lambda x: x.name)

    def read_file(self, key: str, path: str, ref: str | None = None) -> str:
        import base64
        target = key if "/" in key else f"{self.org}/{key}"
        route = f"repos/{target}/contents/{path}" + (f"?ref={ref}" if ref else "")
        raw = self._cli(["api", route])
        if not isinstance(raw, dict) or "content" not in raw:
            raise AdapterError(f"{path} is not a file in {target}")
        return base64.b64decode(raw["content"]).decode("utf-8", "replace")

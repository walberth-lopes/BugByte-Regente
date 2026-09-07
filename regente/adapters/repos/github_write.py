# -*- coding: utf-8 -*-
"""The write path to the hosted repository. Separate from the read adapter.

A different class, a different allowlist, a different constructor argument. The
read adapter cannot be turned into a write adapter by passing a flag, and no
read call site can reach a write by accident.

What this can do: open exactly one pull request, and read back what it opened.
Nothing else. `pr merge`, `pr review`, `pr close`, `pr edit` are named in
`CLI_FORBIDDEN_INVOCATIONS` rather than merely absent, so a reader can see they
were considered and refused -- an absence looks like an oversight, a named
refusal looks like a rule.

**The marker is not decoration.** Every pull request this adapter opens carries
`workspace_id / task_key / run_id` in its body, machine-readable. It survives the
loss of local state, and it is what lets the engine recognise its own work after
a crash without adopting somebody else's.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from ...ports import AdapterError, ReadOnlyRefused
from ...ports.repository import (MARKER_PREFIX, FileChange, PullRequest,
                                 RepoRef, build_marker, read_marker)
from ..tasks.transport import (AuthFailure, Call, MalformedResponse, NotFound,
                               Observer, ProviderUnavailable, RateLimited)
from .readonly import cli_is_allowed_write, cli_is_read

#: Fields read back after creating a pull request. `headRefOid` is the one that
#: matters: it is the binding between a PR and the run that produced it.
PR_FIELDS = ("number,url,title,state,isDraft,headRefName,headRefOid,baseRefName,"
             "author,additions,deletions,body,files")

@dataclass(slots=True)
class GitHubWrite:
    """Opens pull requests. Cannot merge, approve, close or edit them."""

    org: str
    cli_path: str = "gh"
    name: str = "github-write"
    observer: Observer | None = None
    timeout: int = 120

    def describe(self) -> dict[str, str]:
        return {"adapter": self.name, "org": self.org,
                "writes": "pull request creation only"}

    def verify(self) -> None:
        self._cli(["auth", "status"], write=False, json_expected=False)

    # ---- execution -------------------------------------------------------

    def _cli(self, args: list[str], write: bool, json_expected: bool = True) -> Any:
        """One door per intent. `write` selects which allowlist applies.

        The caller must say which door it wants. A read call site can therefore
        never reach a write, however the arguments are shaped.
        """
        check = cli_is_allowed_write if write else cli_is_read
        ok, reason = check(args)
        if not ok:
            raise ReadOnlyRefused(
                f"'{self.cli_path} {' '.join(args)}' refused: {reason}")

        started = time.monotonic()
        try:
            p = subprocess.run([self.cli_path, *args], capture_output=True,
                               encoding="utf-8", errors="replace",
                               timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            self._notify(args, started, False, f"timed out after {self.timeout}s")
            raise ProviderUnavailable(f"cli exceeded {self.timeout}s") from e
        except FileNotFoundError as e:
            self._notify(args, started, False, "cli not found")
            raise AdapterError(f"'{self.cli_path}' is not on PATH") from e

        if p.returncode != 0:
            error = (p.stderr or "").strip()[:400]
            low = error.lower()
            self._notify(args, started, False, error)
            if "authentication" in low or "not logged" in low or "401" in low:
                raise AuthFailure(f"credential refused: {error[:200]}")
            if "rate limit" in low or "429" in low:
                raise RateLimited(f"rate limited: {error[:200]}")
            if "not found" in low or "404" in low:
                raise NotFound(f"absent or no access: {error[:200]}")
            if any(m in low for m in ("timeout", "connection", "dial tcp",
                                      "no such host", "502", "503", "504")):
                raise ProviderUnavailable(f"unavailable: {error[:200]}")
            raise AdapterError(f"cli rc={p.returncode}: {error}")

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
            operation="cli-write" if args[:1] != ["auth"] else "cli",
            path=" ".join(args[:2]),
            duration_ms=int((time.monotonic() - started) * 1000),
            success=ok, status=200 if ok else None,
            rate_limited="rate limit" in error.lower(), error=error[:200]))

    # ---- reading a pull request -----------------------------------------

    def _normalize(self, raw: dict[str, Any], repo: str) -> PullRequest:
        return PullRequest(
            number=int(raw.get("number", 0)),
            repo=RepoRef(provider="github", key=repo),
            title=raw.get("title") or "",
            url=raw.get("url") or "",
            state=(raw.get("state") or "").upper(),
            head_sha=raw.get("headRefOid") or "",
            branch=raw.get("headRefName") or "",
            base=raw.get("baseRefName") or "",
            draft=bool(raw.get("isDraft")),
            author=((raw.get("author") or {}) or {}).get("login") or "",
            additions=int(raw.get("additions") or 0),
            deletions=int(raw.get("deletions") or 0),
            files=tuple(
                FileChange(path=f.get("path", ""),
                           additions=int(f.get("additions") or 0),
                           deletions=int(f.get("deletions") or 0))
                for f in (raw.get("files") or []) if isinstance(f, dict)),
            data={"body": raw.get("body") or ""})

    def get_pull_request(self, repo: str, number: int) -> PullRequest:
        target = repo if "/" in repo else f"{self.org}/{repo}"
        raw = self._cli(["pr", "view", str(number), "--repo", target,
                         "--json", PR_FIELDS], write=False)
        if not isinstance(raw, dict):
            raise AdapterError(f"pr view returned {type(raw).__name__}")
        return self._normalize(raw, target)

    def find_pull_request_for_branch(self, repo: str, branch: str) -> PullRequest | None:
        """Any open PR already targeting this branch, whoever opened it.

        Asked BEFORE creating one. Finding somebody else's PR is a refusal, not
        an opportunity: the engine has no way to know their intent, and a second
        PR for the same branch quietly competes with a person's work.
        """
        target = repo if "/" in repo else f"{self.org}/{repo}"
        raw = self._cli(["pr", "list", "--repo", target, "--head", branch,
                         "--state", "open", "--limit", "10",
                         "--json", PR_FIELDS], write=False)
        if not isinstance(raw, list):
            raise AdapterError("pr list returned an unexpected shape")
        return self._normalize(raw[0], target) if raw else None

    def remote_branch_sha(self, repo: str, branch: str) -> str | None:
        """The commit a branch points at on the remote, or `None` if absent.

        A READ, through the read allowlist, and the only way to answer the
        question that makes a push idempotent: did the previous attempt land?
        A process that died between pushing and recording its push has no local
        way to tell, and pushing again blind is how a retry becomes a second
        mutation.
        """
        target = repo if "/" in repo else f"{self.org}/{repo}"
        try:
            raw = self._cli(["api", f"repos/{target}/git/ref/heads/{branch}"],
                            write=False)
        except NotFound:
            return None
        if not isinstance(raw, dict):
            raise AdapterError("git ref returned an unexpected shape")
        return ((raw.get("object") or {}).get("sha") or "") or None

    # ---- the one mutation ------------------------------------------------

    def create_pull_request(self, repo: str, branch: str, base: str, title: str,
                            body: str, marker: str) -> PullRequest:
        """Open exactly one pull request, carrying the run marker.

        `marker` is a required argument, not an option folded into `body`. A
        signature that lets the marker be forgotten produces pull requests the
        engine cannot recognise later -- and it would only be discovered after a
        crash, which is the worst moment to learn it.
        """
        if not marker or MARKER_PREFIX not in marker:
            raise AdapterError("refused: a pull request must carry a run marker")
        if base == branch:
            raise AdapterError(f"refused: base and head are both '{branch}'")

        target = repo if "/" in repo else f"{self.org}/{repo}"
        full_body = f"{body.rstrip()}\n\n{marker}\n"
        created = self._cli(
            ["pr", "create", "--repo", target, "--head", branch, "--base", base,
             "--title", title, "--body", full_body],
            write=True, json_expected=False)

        # `pr create` prints a URL, not JSON. The number is read back from the
        # remote rather than parsed out of it: the engine must confirm what was
        # created, not trust what it was told it created.
        number = _number_from_url(str(created))
        if number is None:
            raise MalformedResponse(f"could not read a PR number from: {created!r}")
        return self.get_pull_request(target, number)


def _number_from_url(text: str) -> int | None:
    m = re.search(r"/pull/(\d+)", text or "")
    return int(m.group(1)) if m else None

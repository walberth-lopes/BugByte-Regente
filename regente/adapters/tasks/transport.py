# -*- coding: utf-8 -*-
"""HTTP transport for task providers. Where shadow mode becomes a guarantee.

**The port only has `get`.** There is no `post`, `put` or `delete` in this
module. That is not discipline, it is impossibility: the adapter cannot mutate
the external system because there is no function that would. Turning writing on
would require adding a method -- a change visible in a diff, reviewable by a
human, and not an `if dry_run` somebody switches off by mistake at two in the
morning.

Two transports behind the same interface:

- `HttpTransport` -- a real network against the official API.
- `SnapshotTransport` -- replays REAL responses already captured on disk. It is
  not a simulation of the API: it is what the API returned, byte for byte. It is
  what makes it possible to test the contract and run the shadow without a
  credential and without touching the network.

Resilience lives here, not in the adapter: timeout, connection failure, HTTP
error, rate limit, authentication and malformed response all become typed errors.
The engine never breaks because the provider went down -- it records a tool
failure.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ...ports import AdapterError


class ProviderUnavailable(AdapterError):
    """A transient failure: timeout, connection, 5xx. Retrying makes sense."""


class RateLimited(AdapterError):
    """429. Carries how many seconds to wait, when the provider says."""

    def __init__(self, message: str, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class AuthFailure(AdapterError):
    """401/403. NEVER retry: repeating an invalid credential only locks the account."""


class MalformedResponse(AdapterError):
    """A 200 arrived with a body that is not the expected JSON."""


class NotFound(AdapterError):
    """404. Absence CONFIRMED by the provider -- different from a read failure."""


@dataclass(frozen=True, slots=True)
class Call:
    """What happened in one call. Becomes an observability event.

    It carries neither body nor credential: what needs diagnosing is latency,
    failure, retries and rate limiting. A response body in a log is a leak
    waiting to happen.
    """
    operation: str
    path: str
    duration_ms: int
    success: bool
    status: int | None = None
    attempts: int = 1
    rate_limited: bool = False
    request_id: str | None = None
    error: str = ""


#: The observer's signature. The transport does not know the Store: it reports,
#: and whoever listens decides what to do with the report.
Observer = Callable[[Call], None]


class Transport(Protocol):
    """Reading, and reading only."""

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any: ...


@dataclass(slots=True)
class HttpTransport:
    """A read-only HTTP client against a REST API authenticated with Basic.

    The credential is resolved through a callable, never stored in a readable
    attribute nor printed: what holds the secret is the SecretProvider, and it
    hands it over at the moment of use.
    """
    base_url: str
    #: Returns (user, secret). Called on every request, on purpose: a rotated
    #: credential takes effect without restarting the engine.
    credencial: Callable[[], tuple[str, str]]
    timeout: int = 30
    max_attempts: int = 3
    observer: Observer | None = None
    user_agent: str = "regente/0.1 (+leitura)"

    def _authorization(self) -> str:
        import base64
        user, segredo = self.credencial()
        if not user or not segredo:
            raise AuthFailure("credential missing or empty")
        raw = f"{user}:{segredo}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url.rstrip("/") + "/" + path.lstrip("/")
        if params:
            limpos = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(limpos, doseq=True)

        inicio = time.monotonic()
        last: Exception | None = None
        for tentativa in range(1, self.max_attempts + 1):
            try:
                data, status, req_id = self._one_attempt(url)
            except AuthFailure as e:
                self._notify_observer(path, inicio, False, tentativa, error=str(e), status=401)
                raise
            except NotFound as e:
                self._notify_observer(path, inicio, False, tentativa, error=str(e), status=404)
                raise
            except RateLimited as e:
                last = e
                if tentativa == self.max_attempts:
                    self._notify_observer(path, inicio, False, tentativa, error=str(e),
                                status=429, rate_limited=True)
                    raise
                # Respect the provider's Retry-After; without it, exponential backoff.
                time.sleep(e.retry_after_seconds if e.retry_after_seconds is not None
                           else min(30.0, 2.0 ** tentativa))
            except ProviderUnavailable as e:
                last = e
                if tentativa == self.max_attempts:
                    self._notify_observer(path, inicio, False, tentativa, error=str(e))
                    raise
                time.sleep(min(15.0, 1.5 ** tentativa))
            else:
                self._notify_observer(path, inicio, True, tentativa, status=status,
                            request_id=req_id)
                return data
        raise last or ProviderUnavailable("no attempts left")

    def _one_attempt(self, url: str) -> tuple[Any, int, str | None]:
        request = urllib.request.Request(url, method="GET", headers={
            "Authorization": self._authorization(),
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as r:
                raw = r.read()
                req_id = r.headers.get("X-Arequestid") or r.headers.get("X-Request-Id")
                status = r.status
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = (e.read() or b"").decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if e.code in (401, 403):
                raise AuthFailure(f"HTTP {e.code}: credential refused") from e
            if e.code == 404:
                raise NotFound(f"HTTP 404: {url.split('?')[0]}") from e
            if e.code == 429:
                espera = e.headers.get("Retry-After")
                raise RateLimited("HTTP 429: rate limit",
                                   float(espera) if (espera or "").strip().isdigit() else None) from e
            if e.code >= 500:
                raise ProviderUnavailable(f"HTTP {e.code}: {body}") from e
            raise AdapterError(f"HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise ProviderUnavailable(f"connection failure: {e.reason}") from e
        except TimeoutError as e:
            raise ProviderUnavailable(f"timeout after {self.timeout}s") from e

        if not raw:
            raise MalformedResponse("empty body where JSON was expected")
        try:
            return json.loads(raw.decode("utf-8")), status, req_id
        except (ValueError, UnicodeDecodeError) as e:
            raise MalformedResponse(f"body is not valid JSON: {e}") from e

    def _notify_observer(self, path: str, inicio: float, ok: bool, attempts: int,
               status: int | None = None, rate_limited: bool = False,
               request_id: str | None = None, error: str = "") -> None:
        if not self.observer:
            return
        self.observer(Call(
            operation="GET", path=path.split("?")[0],
            duration_ms=int((time.monotonic() - inicio) * 1000),
            success=ok, status=status, attempts=attempts,
            rate_limited=rate_limited, request_id=request_id, error=error[:200]))


@dataclass(slots=True)
class SnapshotTransport:
    """Replays REAL responses recorded on disk.

    Each file is the body the API actually returned. That makes it possible to
    exercise the whole adapter -- pagination, mapping, crooked data -- against
    what the world really answered, with no credential and no network.

    `failures` injects an error on a specific route, and that is how the contract
    tests exercise resilience: the transport raises the same kind of error the
    network would raise.
    """
    directory: Path
    observer: Observer | None = None
    #: path -> exception to raise instead of answering
    failures: dict[str, Exception] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    @staticmethod
    def nome_de(path: str, params: dict[str, Any] | None) -> str:
        """Route + the parameters that change the response -> a stable file name."""
        base = path.strip("/").replace("/", "_")
        cursor = (params or {}).get("nextPageToken")
        if cursor:
            # Real pagination needs a snapshot per page, otherwise the second
            # call would return the first page for ever.
            #
            # `hashlib`, not `hash()`: Python's str hash is randomised per
            # process. Using it would generate one name on capture and another on
            # read -- working on the machine that recorded it and on no other.
            import hashlib
            mark = hashlib.sha1(str(cursor).encode("utf-8")).hexdigest()[:8]
            base += "__p" + mark
        return base + ".json"

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        inicio = time.monotonic()
        self.calls.append(path)
        for rota, error in self.failures.items():
            if rota in path:
                self._notify_observer(path, inicio, False, str(error))
                raise error
        file = self.directory / self.nome_de(path, params)
        if not file.is_file():
            self._notify_observer(path, inicio, False, "snapshot missing")
            raise NotFound(f"no snapshot for {path} in {self.directory}")
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except ValueError as e:
            self._notify_observer(path, inicio, False, str(e))
            raise MalformedResponse(f"{file.name}: {e}") from e
        self._notify_observer(path, inicio, True, "")
        return data

    def _notify_observer(self, path: str, inicio: float, ok: bool, error: str) -> None:
        if self.observer:
            self.observer(Call(
                operation="GET", path=path.split("?")[0],
                duration_ms=int((time.monotonic() - inicio) * 1000),
                success=ok, status=200 if ok else None, error=error[:200]))

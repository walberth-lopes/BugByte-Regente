# -*- coding: utf-8 -*-
"""Transporte HTTP para provedores de task. Onde o shadow mode vira garantia.

**A porta so tem `get`.** Não existe `post`, `put` nem `delete` neste modulo.
Isso não é disciplina, é impossibilidade: o adapter não pode mutar o sistema
externo porque não há função que o faça. Ligar escrita exigiria adicionar um
metodo -- uma mudança visivel num diff, revisavel por um humano, e não um
`if dry_run` que alguem desliga por engano às duas da manhã.

Dois transportes atrás da mesma interface:

- `TransporteHTTP`  -- rede de verdade contra a API oficial.
- `TransporteInstantaneo` -- reproduz respostas REAIS ja capturadas em disco.
  Não é simulação da API: é o que a API devolveu, byte a byte. É o que permite
  testar contrato e rodar sombra sem credencial e sem tocar na rede.

Resiliência mora aqui, e não no adapter: timeout, falha de conexão, error HTTP,
limite de taxa, autenticação e resposta malformada viram erros tipados. O motor
nunca quebra porque o provedor caiu -- ele registra falha de ferramenta.
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
    """Falha transitoria: timeout, conexao, 5xx. Retentar faz sentido."""


class RateLimited(AdapterError):
    """429. Carrega quantos segundos esperar, quando o provedor informa."""

    def __init__(self, message: str, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class AuthFailure(AdapterError):
    """401/403. NUNCA retentar: repetir credencial invalida so bloqueia a conta."""


class MalformedResponse(AdapterError):
    """Veio 200 com corpo que nao e o JSON esperado."""


class NotFound(AdapterError):
    """404. E ausencia CONFIRMADA pelo provedor -- diferente de falha de leitura."""


@dataclass(frozen=True, slots=True)
class Call:
    """O que aconteceu numa chamada. Vira evento de observabilidade.

    Nao carrega corpo nem credencial: o que se quer diagnosticar e latencia,
    falha, retentativa e limite de taxa. Corpo de resposta em log e vazamento
    esperando acontecer.
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


#: Assinatura do observador. O transporte nao conhece o Store: ele avisa, e quem
#: escuta decide o que fazer com o aviso.
Observer = Callable[[Call], None]


class Transport(Protocol):
    """Leitura, e so leitura."""

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any: ...


@dataclass(slots=True)
class HttpTransport:
    """Cliente HTTP de leitura contra uma API REST autenticada por Basic.

    A credencial e resolvida por callable, nunca guardada em atributo legivel
    nem impressa: quem tem o segredo e o SecretProvider, e ele entrega no
    momento do uso.
    """
    base_url: str
    #: Devolve (usuario, segredo). Chamado a cada requisicao, de proposito:
    #: credencial rotacionada passa a valer sem reiniciar o motor.
    credencial: Callable[[], tuple[str, str]]
    timeout: int = 30
    max_attempts: int = 3
    observador: Observer | None = None
    user_agent: str = "regente/0.1 (+leitura)"

    def _authorization(self) -> str:
        import base64
        user, segredo = self.credencial()
        if not user or not segredo:
            raise AuthFailure("credencial ausente ou vazia")
        bruto = f"{user}:{segredo}".encode("utf-8")
        return "Basic " + base64.b64encode(bruto).decode("ascii")

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url.rstrip("/") + "/" + path.lstrip("/")
        if params:
            limpos = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(limpos, doseq=True)

        inicio = time.monotonic()
        ultimo: Exception | None = None
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
                ultimo = e
                if tentativa == self.max_attempts:
                    self._notify_observer(path, inicio, False, tentativa, error=str(e),
                                status=429, rate_limited=True)
                    raise
                # Respeitar Retry-After do provedor; sem ele, recuo exponencial.
                time.sleep(e.retry_after_seconds if e.retry_after_seconds is not None
                           else min(30.0, 2.0 ** tentativa))
            except ProviderUnavailable as e:
                ultimo = e
                if tentativa == self.max_attempts:
                    self._notify_observer(path, inicio, False, tentativa, error=str(e))
                    raise
                time.sleep(min(15.0, 1.5 ** tentativa))
            else:
                self._notify_observer(path, inicio, True, tentativa, status=status,
                            request_id=req_id)
                return data
        raise ultimo or ProviderUnavailable("sem tentativas restantes")

    def _one_attempt(self, url: str) -> tuple[Any, int, str | None]:
        pedido = urllib.request.Request(url, method="GET", headers={
            "Authorization": self._authorization(),
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        })
        try:
            with urllib.request.urlopen(pedido, timeout=self.timeout) as r:
                bruto = r.read()
                req_id = r.headers.get("X-Arequestid") or r.headers.get("X-Request-Id")
                status = r.status
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = (e.read() or b"").decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if e.code in (401, 403):
                raise AuthFailure(f"HTTP {e.code}: credencial recusada") from e
            if e.code == 404:
                raise NotFound(f"HTTP 404: {url.split('?')[0]}") from e
            if e.code == 429:
                espera = e.headers.get("Retry-After")
                raise RateLimited("HTTP 429: limite de taxa",
                                   float(espera) if (espera or "").strip().isdigit() else None) from e
            if e.code >= 500:
                raise ProviderUnavailable(f"HTTP {e.code}: {body}") from e
            raise AdapterError(f"HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise ProviderUnavailable(f"falha de conexao: {e.reason}") from e
        except TimeoutError as e:
            raise ProviderUnavailable(f"timeout apos {self.timeout}s") from e

        if not bruto:
            raise MalformedResponse("corpo vazio onde se esperava JSON")
        try:
            return json.loads(bruto.decode("utf-8")), status, req_id
        except (ValueError, UnicodeDecodeError) as e:
            raise MalformedResponse(f"corpo nao e JSON valido: {e}") from e

    def _notify_observer(self, path: str, inicio: float, ok: bool, attempts: int,
               status: int | None = None, rate_limited: bool = False,
               request_id: str | None = None, error: str = "") -> None:
        if not self.observador:
            return
        self.observador(Call(
            operation="GET", path=path.split("?")[0],
            duration_ms=int((time.monotonic() - inicio) * 1000),
            success=ok, status=status, attempts=attempts,
            rate_limited=rate_limited, request_id=request_id, error=error[:200]))


@dataclass(slots=True)
class SnapshotTransport:
    """Reproduz respostas REAIS gravadas em disco.

    Cada arquivo e o corpo que a API devolveu de fato. Isso torna possivel
    exercitar o adapter inteiro -- paginacao, mapeamento, dados tortos -- contra
    o que o mundo realmente respondeu, sem credencial e sem rede.

    `falhas` injeta error numa rota especifica, e e assim que os testes de
    contrato exercitam resiliencia: o transporte levanta o mesmo tipo de error
    que a rede levantaria.
    """
    diretorio: Path
    observador: Observer | None = None
    #: caminho -> excecao a levantar em vez de responder
    failures: dict[str, Exception] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    @staticmethod
    def nome_de(path: str, params: dict[str, Any] | None) -> str:
        """Rota + parametros que mudam a resposta -> nome de arquivo estavel."""
        base = path.strip("/").replace("/", "_")
        cursor = (params or {}).get("nextPageToken")
        if cursor:
            # Paginacao real precisa de instantaneo por pagina, senao a segunda
            # chamada devolveria a primeira pagina para sempre.
            #
            # `hashlib`, e nao `hash()`: o hash de str do Python e randomizado a
            # cada processo. Usa-lo geraria um nome na captura e outro na
            # leitura -- funcionando na maquina que gravou e em nenhuma outra.
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
        arquivo = self.diretorio / self.nome_de(path, params)
        if not arquivo.is_file():
            self._notify_observer(path, inicio, False, "instantaneo ausente")
            raise NotFound(f"sem instantaneo para {path} em {self.diretorio}")
        try:
            data = json.loads(arquivo.read_text(encoding="utf-8"))
        except ValueError as e:
            self._notify_observer(path, inicio, False, str(e))
            raise MalformedResponse(f"{arquivo.name}: {e}") from e
        self._notify_observer(path, inicio, True, "")
        return data

    def _notify_observer(self, path: str, inicio: float, ok: bool, error: str) -> None:
        if self.observador:
            self.observador(Call(
                operation="GET", path=path.split("?")[0],
                duration_ms=int((time.monotonic() - inicio) * 1000),
                success=ok, status=200 if ok else None, error=error[:200]))

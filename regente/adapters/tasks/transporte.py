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

Resiliência mora aqui, e não no adapter: timeout, falha de conexão, erro HTTP,
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

from ...ports import AdapterErro


class ProvedorIndisponivel(AdapterErro):
    """Falha transitoria: timeout, conexao, 5xx. Retentar faz sentido."""


class LimiteDeTaxa(AdapterErro):
    """429. Carrega quantos segundos esperar, quando o provedor informa."""

    def __init__(self, mensagem: str, esperar_segundos: float | None = None):
        super().__init__(mensagem)
        self.esperar_segundos = esperar_segundos


class FalhaDeAutenticacao(AdapterErro):
    """401/403. NUNCA retentar: repetir credencial invalida so bloqueia a conta."""


class RespostaMalformada(AdapterErro):
    """Veio 200 com corpo que nao e o JSON esperado."""


class NaoEncontrado(AdapterErro):
    """404. E ausencia CONFIRMADA pelo provedor -- diferente de falha de leitura."""


@dataclass(frozen=True, slots=True)
class Chamada:
    """O que aconteceu numa chamada. Vira evento de observabilidade.

    Nao carrega corpo nem credencial: o que se quer diagnosticar e latencia,
    falha, retentativa e limite de taxa. Corpo de resposta em log e vazamento
    esperando acontecer.
    """
    operacao: str
    caminho: str
    duracao_ms: int
    sucesso: bool
    status: int | None = None
    tentativas: int = 1
    limitado: bool = False
    request_id: str | None = None
    erro: str = ""


#: Assinatura do observador. O transporte nao conhece o Store: ele avisa, e quem
#: escuta decide o que fazer com o aviso.
Observador = Callable[[Chamada], None]


class Transporte(Protocol):
    """Leitura, e so leitura."""

    def get(self, caminho: str, params: dict[str, Any] | None = None) -> Any: ...


@dataclass(slots=True)
class TransporteHTTP:
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
    max_tentativas: int = 3
    observador: Observador | None = None
    user_agent: str = "regente/0.1 (+leitura)"

    def _autorizacao(self) -> str:
        import base64
        usuario, segredo = self.credencial()
        if not usuario or not segredo:
            raise FalhaDeAutenticacao("credencial ausente ou vazia")
        bruto = f"{usuario}:{segredo}".encode("utf-8")
        return "Basic " + base64.b64encode(bruto).decode("ascii")

    def get(self, caminho: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url.rstrip("/") + "/" + caminho.lstrip("/")
        if params:
            limpos = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(limpos, doseq=True)

        inicio = time.monotonic()
        ultimo: Exception | None = None
        for tentativa in range(1, self.max_tentativas + 1):
            try:
                dados, status, req_id = self._uma_tentativa(url)
            except FalhaDeAutenticacao as e:
                self._avisa(caminho, inicio, False, tentativa, erro=str(e), status=401)
                raise
            except NaoEncontrado as e:
                self._avisa(caminho, inicio, False, tentativa, erro=str(e), status=404)
                raise
            except LimiteDeTaxa as e:
                ultimo = e
                if tentativa == self.max_tentativas:
                    self._avisa(caminho, inicio, False, tentativa, erro=str(e),
                                status=429, limitado=True)
                    raise
                # Respeitar Retry-After do provedor; sem ele, recuo exponencial.
                time.sleep(e.esperar_segundos if e.esperar_segundos is not None
                           else min(30.0, 2.0 ** tentativa))
            except ProvedorIndisponivel as e:
                ultimo = e
                if tentativa == self.max_tentativas:
                    self._avisa(caminho, inicio, False, tentativa, erro=str(e))
                    raise
                time.sleep(min(15.0, 1.5 ** tentativa))
            else:
                self._avisa(caminho, inicio, True, tentativa, status=status,
                            request_id=req_id)
                return dados
        raise ultimo or ProvedorIndisponivel("sem tentativas restantes")

    def _uma_tentativa(self, url: str) -> tuple[Any, int, str | None]:
        pedido = urllib.request.Request(url, method="GET", headers={
            "Authorization": self._autorizacao(),
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        })
        try:
            with urllib.request.urlopen(pedido, timeout=self.timeout) as r:
                bruto = r.read()
                req_id = r.headers.get("X-Arequestid") or r.headers.get("X-Request-Id")
                status = r.status
        except urllib.error.HTTPError as e:
            corpo = ""
            try:
                corpo = (e.read() or b"").decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if e.code in (401, 403):
                raise FalhaDeAutenticacao(f"HTTP {e.code}: credencial recusada") from e
            if e.code == 404:
                raise NaoEncontrado(f"HTTP 404: {url.split('?')[0]}") from e
            if e.code == 429:
                espera = e.headers.get("Retry-After")
                raise LimiteDeTaxa("HTTP 429: limite de taxa",
                                   float(espera) if (espera or "").strip().isdigit() else None) from e
            if e.code >= 500:
                raise ProvedorIndisponivel(f"HTTP {e.code}: {corpo}") from e
            raise AdapterErro(f"HTTP {e.code}: {corpo}") from e
        except urllib.error.URLError as e:
            raise ProvedorIndisponivel(f"falha de conexao: {e.reason}") from e
        except TimeoutError as e:
            raise ProvedorIndisponivel(f"timeout apos {self.timeout}s") from e

        if not bruto:
            raise RespostaMalformada("corpo vazio onde se esperava JSON")
        try:
            return json.loads(bruto.decode("utf-8")), status, req_id
        except (ValueError, UnicodeDecodeError) as e:
            raise RespostaMalformada(f"corpo nao e JSON valido: {e}") from e

    def _avisa(self, caminho: str, inicio: float, ok: bool, tentativas: int,
               status: int | None = None, limitado: bool = False,
               request_id: str | None = None, erro: str = "") -> None:
        if not self.observador:
            return
        self.observador(Chamada(
            operacao="GET", caminho=caminho.split("?")[0],
            duracao_ms=int((time.monotonic() - inicio) * 1000),
            sucesso=ok, status=status, tentativas=tentativas,
            limitado=limitado, request_id=request_id, erro=erro[:200]))


@dataclass(slots=True)
class TransporteInstantaneo:
    """Reproduz respostas REAIS gravadas em disco.

    Cada arquivo e o corpo que a API devolveu de fato. Isso torna possivel
    exercitar o adapter inteiro -- paginacao, mapeamento, dados tortos -- contra
    o que o mundo realmente respondeu, sem credencial e sem rede.

    `falhas` injeta erro numa rota especifica, e e assim que os testes de
    contrato exercitam resiliencia: o transporte levanta o mesmo tipo de erro
    que a rede levantaria.
    """
    diretorio: Path
    observador: Observador | None = None
    #: caminho -> excecao a levantar em vez de responder
    falhas: dict[str, Exception] = field(default_factory=dict)
    chamadas: list[str] = field(default_factory=list)

    @staticmethod
    def nome_de(caminho: str, params: dict[str, Any] | None) -> str:
        """Rota + parametros que mudam a resposta -> nome de arquivo estavel."""
        base = caminho.strip("/").replace("/", "_")
        cursor = (params or {}).get("nextPageToken")
        if cursor:
            # Paginacao real precisa de instantaneo por pagina, senao a segunda
            # chamada devolveria a primeira pagina para sempre.
            #
            # `hashlib`, e nao `hash()`: o hash de str do Python e randomizado a
            # cada processo. Usa-lo geraria um nome na captura e outro na
            # leitura -- funcionando na maquina que gravou e em nenhuma outra.
            import hashlib
            marca = hashlib.sha1(str(cursor).encode("utf-8")).hexdigest()[:8]
            base += "__p" + marca
        return base + ".json"

    def get(self, caminho: str, params: dict[str, Any] | None = None) -> Any:
        inicio = time.monotonic()
        self.chamadas.append(caminho)
        for rota, erro in self.falhas.items():
            if rota in caminho:
                self._avisa(caminho, inicio, False, str(erro))
                raise erro
        arquivo = self.diretorio / self.nome_de(caminho, params)
        if not arquivo.is_file():
            self._avisa(caminho, inicio, False, "instantaneo ausente")
            raise NaoEncontrado(f"sem instantaneo para {caminho} em {self.diretorio}")
        try:
            dados = json.loads(arquivo.read_text(encoding="utf-8"))
        except ValueError as e:
            self._avisa(caminho, inicio, False, str(e))
            raise RespostaMalformada(f"{arquivo.name}: {e}") from e
        self._avisa(caminho, inicio, True, "")
        return dados

    def _avisa(self, caminho: str, inicio: float, ok: bool, erro: str) -> None:
        if self.observador:
            self.observador(Chamada(
                operacao="GET", caminho=caminho.split("?")[0],
                duracao_ms=int((time.monotonic() - inicio) * 1000),
                sucesso=ok, status=200 if ok else None, erro=erro[:200]))

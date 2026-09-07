# -*- coding: utf-8 -*-
"""Testar uma credencial contra o provedor real. Uma leitura, nunca mais.

Vive em `adapters/` porque e o unico lugar onde um nome de fornecedor pode
aparecer, e porque falar com a rede e I/O. O motor recebe um veredito -- quatro
valores possiveis -- e nao sabe o que e um cabecalho HTTP.

**Somente leitura, e isso e uma afirmacao sobre o codigo.** Cada sonda chama um
endpoint que nao muta nada. Nao ha caminho aqui que escreva: autenticacao valida
nao e autorizacao para mutacao, e um teste de conexao que criasse alguma coisa
para "provar que funciona" teria provado outra coisa.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ..core.credential import Use
from ..engine.credentials import Reach
from ..ports import AdapterError

#: Quanto tempo esperar o provedor. Curto: um teste de conexao que demora e um
#: teste que ninguem roda.
TIMEOUT = 15


@dataclass(slots=True)
class HttpTokenProbe:
    """Autentica com um token e le UM recurso que nao muta nada."""

    name: str
    url: str
    #: Como o token entra no cabecalho. O formato e do fornecedor, e por isso
    #: mora aqui e nao no motor.
    scheme: str = "Bearer"
    #: O que este provedor sabe fazer. Separado da capacidade da credencial de
    #: proposito: uma credencial autorizada para push num provider que so le
    #: produz surpresa, a menos que alguem diga isso antes.
    supported: frozenset[Use] = field(default_factory=frozenset)

    def supports(self, use: Use) -> bool:
        return use in self.supported

    def check(self, material: str) -> tuple[Reach, str]:
        """Uma requisicao GET. O material nunca entra em log nem em excecao."""
        request = urllib.request.Request(self.url, method="GET")
        request.add_header("Authorization", f"{self.scheme} {material}")
        request.add_header("User-Agent", "regente")
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as r:
                corpo = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # Fato sobre a credencial: o provedor a recusou.
                return Reach.REJECTED, f"o provedor recusou ({e.code})"
            # Qualquer outro codigo nao diz nada sobre a credencial.
            return Reach.UNKNOWN, f"resposta inesperada ({e.code})"
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            # Nao deu para perguntar. Tratar isto como recusa mandaria alguem
            # trocar um token que estava bom.
            return Reach.UNAVAILABLE, f"{type(e).__name__}"
        except ValueError:
            return Reach.UNKNOWN, "o provedor respondeu algo que nao e JSON"

        quem = corpo.get("login") or corpo.get("name") or corpo.get("id") or ""
        return Reach.AUTHENTICATED, f"aceita como '{quem}'" if quem else "aceita"


@dataclass(slots=True)
class CommandProbe:
    """Pergunta a um programa local se a credencial serve.

    Existe para provedores que nao expoem um endpoint simples, e para provar
    que a sonda e uma PORTA: trocar HTTP por um comando nao muda nada acima.
    """

    name: str
    command: tuple[str, ...]
    supported: frozenset[Use] = field(default_factory=frozenset)

    def supports(self, use: Use) -> bool:
        return use in self.supported

    def check(self, material: str) -> tuple[Reach, str]:
        try:
            saida = subprocess.run(list(self.command), capture_output=True,
                                   text=True, timeout=TIMEOUT,
                                   # O material vai por stdin, nao por ambiente
                                   # nem por argumento: os dois aparecem na
                                   # lista de processos da maquina.
                                   input=material)
        except (OSError, subprocess.SubprocessError) as e:
            return Reach.UNAVAILABLE, type(e).__name__
        if saida.returncode == 0:
            return Reach.AUTHENTICATED, "o comando aceitou a credencial"
        return Reach.REJECTED, f"o comando recusou (codigo {saida.returncode})"


#: Sondas conhecidas, por provider da composicao. Nomes de fornecedor param
#: aqui: acima disto o motor so ve `check()` e `supports()`.
def probe_for(provider: str, config) -> object:
    """A sonda deste provider, ou uma que admite nao saber testar."""
    conf = getattr(config, "providers", {}).get(provider)
    nome = getattr(conf, "name", "") if conf else ""

    if nome in ("github", "github-write") or provider == "repository_write":
        return HttpTokenProbe(
            name="github", url="https://api.github.com/user",
            supported=frozenset({Use.REPO_READ, Use.REPO_PUSH, Use.REPO_PR}))
    if nome == "jira":
        site = str(getattr(conf, "options", {}).get("site", "")).rstrip("/")
        if site:
            return HttpTokenProbe(
                name="jira", url=f"{site}/rest/api/3/myself",
                supported=frozenset({Use.TASK_READ, Use.TASK_WRITE}))
    return _CannotTest(provider)


@dataclass(slots=True)
class _CannotTest:
    """A sonda que admite nao saber testar este provider.

    Devolver `AUTHENTICATED` aqui seria a mentira mais barata do arquivo: nada
    foi verificado, e a tela mostraria verde.
    """
    provider: str

    def supports(self, use: Use) -> bool:
        return False

    def check(self, material: str) -> tuple[Reach, str]:
        return (Reach.UNKNOWN,
                f"nao ha sonda de conexao para o provider '{self.provider}'")

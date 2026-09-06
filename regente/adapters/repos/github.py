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
o que tambem significa que uma falha de autenticacao aparece como erro tipado, e
nao como lista vazia.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from ...ports import AdapterErro, SomenteLeitura
from ...ports.repository import (LEITURA, Branch, CapacidadeRepo, RepoInfo, RepoRef,
                                 RepositoryProvider)
from ..tasks.transporte import (Chamada, FalhaDeAutenticacao, LimiteDeTaxa,
                                NaoEncontrado, Observador, ProvedorIndisponivel,
                                RespostaMalformada)
from .leitura import cli_e_leitura


#: Campos pedidos numa LISTAGEM. Enxuto: uma organizacao com centenas de
#: repositorios devolve megabytes se cada item trouxer tudo.
CAMPOS_LISTA = "name,nameWithOwner,defaultBranchRef,isArchived,isPrivate,url"

#: Campos do DETALHE. Pagos um repositorio por vez.
CAMPOS_DETALHE = CAMPOS_LISTA + ",description,sshUrl,pushedAt,primaryLanguage"


@dataclass(slots=True)
class GitHubRepos(RepositoryProvider):
    """Le repositorios de uma organizacao na hospedagem remota."""

    org: str
    caminho_cli: str = "gh"
    nome: str = "github"
    capacidades: frozenset[CapacidadeRepo] = field(default_factory=lambda: LEITURA)
    observador: Observador | None = None
    timeout: int = 60
    limite_listagem: int = 200

    def descreve(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.nome,
                "modo": "somente-leitura", "org": self.org}

    def verifica(self) -> None:
        """Prova autenticacao e alcance com a chamada mais barata que existe."""
        self._cli(["auth", "status"], json_esperado=False)

    # ---- execucao --------------------------------------------------------

    def _cli(self, args: list[str], json_esperado: bool = True) -> Any:
        # Por INVOCACAO INTEIRA, nao por verbo. `repo list` le; `repo delete`
        # apaga, e os dois comecam com `repo` -- foi assim que um `repo delete`
        # atravessou o portao em 06/09/2026.
        ok, motivo = cli_e_leitura(args)
        if not ok:
            raise SomenteLeitura(
                f"'{self.caminho_cli} {' '.join(args)}' recusado: {motivo}. "
                f"Nenhuma mutacao e executavel neste marco.")

        inicio = time.monotonic()
        try:
            p = subprocess.run(
                [self.caminho_cli, *args], capture_output=True,
                encoding="utf-8", errors="replace", timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            self._avisa(args, inicio, False, None, f"timeout apos {self.timeout}s")
            raise ProvedorIndisponivel(f"cli estourou {self.timeout}s") from e
        except FileNotFoundError as e:
            self._avisa(args, inicio, False, None, "cli nao encontrada")
            raise AdapterErro(f"'{self.caminho_cli}' nao esta no PATH") from e

        if p.returncode != 0:
            erro = (p.stderr or "").strip()[:400]
            baixo = erro.lower()
            self._avisa(args, inicio, False, None, erro)
            # Traduzir a falha e o que permite o motor decidir: retentar,
            # esperar ou parar. Um erro generico obriga a tratar tudo igual.
            if "authentication" in baixo or "not logged" in baixo or "401" in baixo:
                raise FalhaDeAutenticacao(f"credencial recusada: {erro[:200]}")
            if "rate limit" in baixo or "429" in baixo:
                raise LimiteDeTaxa(f"limite de taxa: {erro[:200]}")
            if "not found" in baixo or "404" in baixo:
                raise NaoEncontrado(f"nao existe ou sem acesso: {erro[:200]}")
            if any(m in baixo for m in ("timeout", "connection", "dial tcp",
                                        "no such host", "502", "503", "504")):
                raise ProvedorIndisponivel(f"indisponivel: {erro[:200]}")
            raise AdapterErro(f"cli rc={p.returncode}: {erro}")

        self._avisa(args, inicio, True, 200, "")
        if not json_esperado:
            return p.stdout
        bruto = (p.stdout or "").strip()
        if not bruto:
            raise RespostaMalformada("corpo vazio onde se esperava JSON")
        try:
            return json.loads(bruto)
        except ValueError as e:
            raise RespostaMalformada(f"saida nao e JSON: {bruto[:200]}") from e

    def _avisa(self, args: list[str], inicio: float, ok: bool,
               status: int | None, erro: str) -> None:
        if not self.observador:
            return
        self.observador(Chamada(
            operacao="cli", caminho=" ".join(args[:2]),
            duracao_ms=int((time.monotonic() - inicio) * 1000),
            sucesso=ok, status=status,
            limitado="rate limit" in erro.lower(), erro=erro[:200]))

    # ---- normalizacao ----------------------------------------------------

    def _monta(self, bruto: dict[str, Any], parcial: bool) -> RepoInfo:
        chave = bruto.get("nameWithOwner") or ""
        if not chave:
            raise AdapterErro("repositorio sem identidade completa na resposta")
        base = ((bruto.get("defaultBranchRef") or {}) or {}).get("name") or ""
        return RepoInfo(
            ref=RepoRef(provider=self.nome, key=chave),
            nome=bruto.get("name") or chave.rsplit("/", 1)[-1],
            branch_base=base,
            origem_de_clone=f"https://github.com/{chave}.git",
            url=bruto.get("url"),
            arquivado=bool(bruto.get("isArchived")),
            privado=bruto.get("isPrivate"),
            capacidades=self.capacidades,
            parcial=parcial,
            dados={k: v for k, v in (
                ("descricao", bruto.get("description")),
                ("empurrado_em", bruto.get("pushedAt")),
                ("linguagem", (bruto.get("primaryLanguage") or {}).get("name")),
            ) if v})

    # ---- descoberta ------------------------------------------------------

    def list_repositories(self, filtro: dict[str, Any] | None = None) -> list[RepoInfo]:
        f = filtro or {}
        args = ["repo", "list", self.org, "--limit", str(f.get("limite", self.limite_listagem)),
                "--json", CAMPOS_LISTA]
        if f.get("sem_arquivados", True):
            args.append("--no-archived")
        bruto = self._cli(args)
        if not isinstance(bruto, list):
            raise AdapterErro(f"listagem devolveu {type(bruto).__name__}, esperava lista")
        return [self._monta(r, parcial=True) for r in bruto]

    def get_repository(self, key: str) -> RepoInfo:
        alvo = key if "/" in key else f"{self.org}/{key}"
        bruto = self._cli(["repo", "view", alvo, "--json", CAMPOS_DETALHE])
        if not isinstance(bruto, dict):
            raise AdapterErro(f"detalhe devolveu {type(bruto).__name__}, esperava objeto")
        return self._monta(bruto, parcial=False)

    def list_branches(self, key: str, filtro: dict[str, Any] | None = None) -> list[Branch]:
        alvo = key if "/" in key else f"{self.org}/{key}"
        base = self.get_repository(alvo).branch_base
        por_pagina = int((filtro or {}).get("por_pagina", 100))
        bruto = self._cli(["api", f"repos/{alvo}/branches?per_page={por_pagina}"])
        if not isinstance(bruto, list):
            raise AdapterErro("listagem de branches devolveu forma inesperada")
        padrao = (filtro or {}).get("padrao", "")
        saida = []
        for b in bruto:
            nome = b.get("name") or ""
            if padrao and padrao.lower() not in nome.lower():
                continue
            saida.append(Branch(nome=nome, sha=(b.get("commit") or {}).get("sha", ""),
                                e_base=(nome == base)))
        return sorted(saida, key=lambda x: x.nome)

    def read_file(self, key: str, caminho: str, ref: str | None = None) -> str:
        import base64
        alvo = key if "/" in key else f"{self.org}/{key}"
        rota = f"repos/{alvo}/contents/{caminho}" + (f"?ref={ref}" if ref else "")
        bruto = self._cli(["api", rota])
        if not isinstance(bruto, dict) or "content" not in bruto:
            raise AdapterErro(f"{caminho} nao e um arquivo em {alvo}")
        return base64.b64decode(bruto["content"]).decode("utf-8", "replace")

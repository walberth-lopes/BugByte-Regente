# -*- coding: utf-8 -*-
"""A regra arquitetural com dentes.

Separacao de camadas escrita so no README dura ate a primeira pressa. Aqui ela e
verificada no codigo-fonte: se alguem importar um adapter dentro do Core, o teste
falha -- e falha em CI, nao em revisao de codigo, que e onde esse tipo de coisa
passa despercebido.

Duas regras:

1. `core/` nao importa nada do motor: nem adapters, nem engine, nem biblioteca de
   rede ou de banco. Ele e dominio puro.
2. `core/` e `engine/` nao mencionam fornecedor nenhum. O nome de uma ferramenta
   nessas pastas e a evidencia de que a abstracao vazou.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent / "regente"

#: Nomes de fornecedor. Se um deles aparecer no Core ou no Engine, a
#: abstracao falhou -- e o teste diz exatamente onde.
FORNECEDORES = [
    "jira", "github", "gitlab", "linear", "bitbucket", "asana",
    "gcloud", "bigquery", "cloudrun", "aws", "azure", "kubernetes",
    "postgres", "mysql", "anthropic", "openai", "slack",
]

#: Bibliotecas que caracterizam I/O. O dominio nao pode importar nenhuma.
IO_PROIBIDO = {"sqlite3", "httpx", "requests", "urllib", "socket", "subprocess", "yaml"}


def _modulos(pasta: str) -> list[Path]:
    return sorted((RAIZ / pasta).rglob("*.py"))


def _imports(caminho: Path) -> list[str]:
    arvore = ast.parse(caminho.read_text(encoding="utf-8"), filename=str(caminho))
    nomes: list[str] = []
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            nomes += [a.name for a in no.names]
        elif isinstance(no, ast.ImportFrom):
            nomes.append(("." * (no.level or 0)) + (no.module or ""))
    return nomes


def test_core_nao_importa_io():
    faltas = []
    for arq in _modulos("core"):
        for nome in _imports(arq):
            raiz = nome.lstrip(".").split(".", 1)[0]
            if raiz in IO_PROIBIDO:
                faltas.append(f"{arq.name} importa {nome}")
    assert not faltas, "o dominio precisa ser puro:\n  " + "\n  ".join(faltas)


def test_core_nao_importa_adapters_nem_engine():
    faltas = []
    for arq in _modulos("core"):
        for nome in _imports(arq):
            if "adapters" in nome or "engine" in nome or "ports" in nome:
                faltas.append(f"{arq.name} importa {nome}")
    assert not faltas, "core nao pode depender das bordas:\n  " + "\n  ".join(faltas)


def test_engine_nao_importa_adapters():
    """O motor conhece PORTAS. Quem escolhe implementacao e a raiz de composicao."""
    faltas = []
    for arq in _modulos("engine"):
        for nome in _imports(arq):
            if "adapters" in nome:
                faltas.append(f"{arq.name} importa {nome}")
    assert not faltas, "engine so pode falar com ports:\n  " + "\n  ".join(faltas)


def _linhas_de_prosa(arq: Path) -> set[int]:
    """Linhas ocupadas por docstring ou comentario.

    Documentacao PRECISA poder citar 'nem Jira, nem GitHub' -- e justamente onde
    a regra fica registrada. O que nao pode e o nome virar codigo: um import, um
    valor default, um literal de comparacao.
    """
    texto = arq.read_text(encoding="utf-8")
    prosa: set[int] = set()
    for n, linha in enumerate(texto.splitlines(), 1):
        if linha.lstrip().startswith("#"):
            prosa.add(n)

    # So DOCSTRING -- o primeiro statement de modulo, classe ou funcao. Uma
    # string qualquer no meio do codigo continua valendo como codigo, senao
    # `provider = "algum-fornecedor"` escaparia justamente por ser string.
    portadores = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for no in ast.walk(ast.parse(texto, filename=str(arq))):
        if not isinstance(no, portadores) or not no.body:
            continue
        primeiro = no.body[0]
        if (isinstance(primeiro, ast.Expr) and isinstance(primeiro.value, ast.Constant)
                and isinstance(primeiro.value.value, str)):
            prosa.update(range(primeiro.lineno,
                               (primeiro.end_lineno or primeiro.lineno) + 1))
    return prosa


def test_nenhum_fornecedor_no_codigo_do_core_nem_do_engine():
    padrao = re.compile("|".join(FORNECEDORES), re.IGNORECASE)
    faltas = []
    for pasta in ("core", "engine", "ports"):
        for arq in _modulos(pasta):
            prosa = _linhas_de_prosa(arq)
            for n, linha in enumerate(arq.read_text(encoding="utf-8").splitlines(), 1):
                if n in prosa:
                    continue
                achado = padrao.search(linha.split("#", 1)[0])
                if achado:
                    faltas.append(f"{pasta}/{arq.name}:{n} menciona '{achado.group()}'")
    assert not faltas, ("nome de ferramenta so existe em adapters/:\n  "
                        + "\n  ".join(faltas))


def test_registro_e_o_unico_a_importar_adapters():
    """Um segundo lugar importando adapter e o comeco do acoplamento."""
    culpados = []
    for arq in RAIZ.rglob("*.py"):
        relativo = arq.relative_to(RAIZ).as_posix()
        if relativo.startswith("adapters/"):
            continue
        if any("adapters" in nome for nome in _imports(arq)):
            culpados.append(relativo)
    assert culpados == ["adapters/registry.py"] or culpados == ["app/container.py"] or set(culpados) <= {
        "app/container.py", "cli.py"}, (
        f"quem importa adapters fora do registro: {culpados}")

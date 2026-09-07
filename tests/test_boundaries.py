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

ROOT = Path(__file__).resolve().parent.parent / "regente"

#: Nomes de fornecedor. Se um deles aparecer no Core ou no Engine, a
#: abstracao falhou -- e o teste diz exatamente onde.
FORNECEDORES = [
    "jira", "github", "gitlab", "linear", "bitbucket", "asana",
    "gcloud", "bigquery", "cloudrun", "aws", "azure", "kubernetes",
    "postgres", "mysql", "anthropic", "openai", "slack",
    # Coding agents. Added in the milestone that introduced one: the list had
    # no entry for this category, so `CLAUDE.md` sat in `engine/context.py`
    # through a full green suite. A guard is only as wide as its vocabulary.
    "claude", "codex", "aider", "cursor", "copilot", "gemini", "windsurf",
]

#: Bibliotecas que caracterizam I/O. O dominio nao pode importar nenhuma.
IO_PROIBIDO = {"sqlite3", "httpx", "requests", "urllib", "socket", "subprocess", "yaml"}


def _modulos(pasta: str) -> list[Path]:
    return sorted((ROOT / pasta).rglob("*.py"))


def _imports(path: Path) -> list[str]:
    arvore = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nomes: list[str] = []
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            nomes += [a.name for a in no.names]
        elif isinstance(no, ast.ImportFrom):
            nomes.append(("." * (no.level or 0)) + (no.module or ""))
    return nomes


def test_core_not_imports_io():
    faltas = []
    for arq in _modulos("core"):
        for name in _imports(arq):
            root = name.lstrip(".").split(".", 1)[0]
            if root in IO_PROIBIDO:
                faltas.append(f"{arq.name} importa {name}")
    assert not faltas, "o dominio precisa ser puro:\n  " + "\n  ".join(faltas)


def test_core_not_imports_adapters_nor_engine():
    faltas = []
    for arq in _modulos("core"):
        for name in _imports(arq):
            if "adapters" in name or "engine" in name or "ports" in name:
                faltas.append(f"{arq.name} importa {name}")
    assert not faltas, "core nao pode depender das bordas:\n  " + "\n  ".join(faltas)


def test_engine_not_imports_adapters():
    """O motor conhece PORTAS. Quem escolhe implementacao e a raiz de composicao."""
    faltas = []
    for arq in _modulos("engine"):
        for name in _imports(arq):
            if "adapters" in name:
                faltas.append(f"{arq.name} importa {name}")
    assert not faltas, "engine so pode falar com ports:\n  " + "\n  ".join(faltas)


def _linhas_de_prosa(arq: Path) -> set[int]:
    """Linhas ocupadas por docstring ou comentario.

    Documentacao PRECISA poder citar 'nem Jira, nem GitHub' -- e justamente onde
    a regra fica registrada. O que nao pode e o nome virar codigo: um import, um
    valor default, um literal de comparacao.
    """
    text = arq.read_text(encoding="utf-8")
    prosa: set[int] = set()
    for n, linha in enumerate(text.splitlines(), 1):
        if linha.lstrip().startswith("#"):
            prosa.add(n)

    # So DOCSTRING -- o primeiro statement de modulo, classe ou funcao. Uma
    # string qualquer no meio do codigo continua valendo como codigo, senao
    # `provider = "algum-fornecedor"` escaparia justamente por ser string.
    portadores = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for no in ast.walk(ast.parse(text, filename=str(arq))):
        if not isinstance(no, portadores) or not no.body:
            continue
        first = no.body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            prosa.update(range(first.lineno,
                               (first.end_lineno or first.lineno) + 1))
    return prosa


def test_no_vendor_in_code_of_core_nor_of_engine():
    default_value = re.compile("|".join(FORNECEDORES), re.IGNORECASE)
    faltas = []
    for pasta in ("core", "engine", "ports"):
        for arq in _modulos(pasta):
            prosa = _linhas_de_prosa(arq)
            for n, linha in enumerate(arq.read_text(encoding="utf-8").splitlines(), 1):
                if n in prosa:
                    continue
                achado = default_value.search(linha.split("#", 1)[0])
                if achado:
                    faltas.append(f"{pasta}/{arq.name}:{n} menciona '{achado.group()}'")
    assert not faltas, ("nome de ferramenta so existe em adapters/:\n  "
                        + "\n  ".join(faltas))


def test_registry_is_the_only_the_importar_adapters():
    """Um segundo lugar importando adapter e o comeco do acoplamento."""
    culpados = []
    for arq in ROOT.rglob("*.py"):
        relativo = arq.relative_to(ROOT).as_posix()
        if relativo.startswith("adapters/"):
            continue
        if any("adapters" in name for name in _imports(arq)):
            culpados.append(relativo)
    assert culpados == ["adapters/registry.py"] or culpados == ["app/container.py"] or set(culpados) <= {
        "app/container.py", "cli.py"}, (
        f"quem importa adapters fora do registro: {culpados}")


def test_swapping_of_provider_only_touches_in_adapter_is_configuration():
    """A revisao arquitetural do Marco 3, escrita como teste.

    Pergunta: se amanha o provedor de tasks for outro completamente diferente,
    o que precisa mudar? Resposta desejada -- e verificada aqui:

        core        NENHUMA mudanca
        ports       NENHUMA mudanca
        engine      NENHUMA mudanca
        adapters    implementacao nova
        config      provedor novo declarado

    O teste prova as tres primeiras linhas. As duas ultimas sao o trabalho.
    """
    # 1. Ninguem fora de adapters/ decide QUAL implementacao usar. As duas
    #    excecoes sao a raiz de composicao e a superficie que a exibe.
    escolhem = set()
    for arq in ROOT.rglob("*.py"):
        rel = arq.relative_to(ROOT).as_posix()
        if rel.startswith("adapters/"):
            continue
        if any("adapters" in name for name in _imports(arq)):
            escolhem.add(rel)
    assert escolhem == {"app/container.py", "cli.py"}, (
        f"quem mais escolhe implementacao: {escolhem - {'app/container.py', 'cli.py'}}")

    # 2. O motor fala com a PORTA, e a port nao conhece fornecedor.
    port = (ROOT / "ports" / "tasks.py").read_text(encoding="utf-8")
    prosa = _linhas_de_prosa(ROOT / "ports" / "tasks.py")
    for n, linha in enumerate(port.splitlines(), 1):
        if n in prosa:
            continue
        assert not re.search("|".join(FORNECEDORES), linha, re.IGNORECASE), (
            f"ports/tasks.py:{n} conhece um fornecedor: {linha.strip()}")

    # 3. Cada capacidade ja tem mais de uma implementacao possivel -- e uma
    #    abstracao com um unico implementador nunca foi testada de verdade.
    import regente.adapters.registry as reg
    assert len(reg.available()["tasks"]) >= 2, (
        "TaskProvider com um adapter so nao prova nada")


def test_repositoryprovider_not_knows_vendor():
    """A contraprova do Marco 4, no mesmo formato do Marco 3.

    Trocar a hospedagem de codigo por outra completamente diferente deve exigir
    adapter novo + configuracao, e nada mais.
    """
    arq = ROOT / "ports" / "repository.py"
    prosa = _linhas_de_prosa(arq)
    for n, linha in enumerate(arq.read_text(encoding="utf-8").splitlines(), 1):
        if n in prosa:
            continue
        achado = re.search("|".join(FORNECEDORES), linha, re.IGNORECASE)
        assert not achado, f"ports/repository.py:{n} conhece '{achado.group()}'"

    import regente.adapters.registry as reg
    assert len(reg.available()["repository"]) >= 2, (
        "RepositoryProvider com um adapter so nao prova nada")


def test_resolution_of_target_not_knows_vendor():
    """O elo task->repositorio e o mais tentador de acoplar: e onde daria vontade
    de olhar `nameWithOwner` ou um campo de um board especifico."""
    for name in ("target.py", "chain.py"):
        arq = ROOT / "engine" / name
        prosa = _linhas_de_prosa(arq)
        for n, linha in enumerate(arq.read_text(encoding="utf-8").splitlines(), 1):
            if n in prosa:
                continue
            achado = re.search("|".join(FORNECEDORES), linha, re.IGNORECASE)
            assert not achado, f"engine/{name}:{n} conhece '{achado.group()}'"

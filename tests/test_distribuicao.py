# -*- coding: utf-8 -*-
"""O Regente como COISA INSTALAVEL, e nao como repositorio que alguem clonou.

Um projeto Python tem duas identidades, e elas divergem em silencio. Ha o
repositorio -- onde se roda `pytest`, onde o codigo esta em `regente/`, e onde
tudo funciona porque o diretorio atual e este. E ha o PACOTE INSTALADO, que e o
que chega na maquina de outra pessoa: um wheel, um atalho no PATH, e nada mais.

O segundo e o produto. E ele quebra de formas que nenhum teste de comportamento
pega: um arquivo que o build nao inclui, uma dependencia que ninguem importa,
uma versao gravada em dois lugares que passam a discordar, um instalador que
aponta para um repositorio que mudou de nome.

Estas guardas leem os arquivos de empacotamento e de instalacao, e conferem que
eles descrevem o mesmo produto que o codigo.
"""

import re
import tomllib
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((RAIZ / "pyproject.toml").read_text(encoding="utf-8"))


# ===========================================================================
# O PACOTE
# ===========================================================================

def test_the_command_people_type_is_the_one_the_package_declares():
    """`regente` no terminal precisa existir por declaracao, e nao por acaso.

    Rodando do repositorio, `python -m regente` funciona porque o diretorio
    atual esta no caminho de import. Isso NAO se traduz num comando instalado:
    quem cria o atalho `regente` e o `project.scripts`.
    """
    scripts = _pyproject()["project"]["scripts"]
    assert scripts.get("regente") == "regente.cli:main", (
        "o atalho `regente` sumiu ou aponta para outro lugar; instalar o pacote "
        "deixaria de criar o comando")


def test_every_declared_dependency_is_actually_imported():
    """Dependencia declarada e nao usada e uma mentira no pacote.

    Ela faz o download crescer, aumenta a superficie, e -- pior -- ENSINA
    errado: houve aqui um extra `ui = [fastapi, uvicorn]` que ninguem
    importava, e ele dizia a quem lesse o pyproject que a Mission Control
    precisa de um servidor de aplicacao para subir. Ela e servida pelo
    `http.server` da biblioteca padrao.
    """
    projeto = _pyproject()["project"]
    declaradas = list(projeto.get("dependencies", []))
    for extra, pacotes in projeto.get("optional-dependencies", {}).items():
        if extra == "dev":       # ferramentas de teste nao sao importadas pelo produto
            continue
        declaradas += pacotes

    fonte = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (RAIZ / "regente").rglob("*.py")
    )
    #: O nome no PyPI nem sempre e o nome do modulo.
    MODULO = {"PyYAML": "yaml"}

    orfas = []
    for bruto in declaradas:
        nome = re.split(r"[<>=!\[ ]", bruto, maxsplit=1)[0]
        modulo = MODULO.get(nome, nome.lower().replace("-", "_"))
        if not re.search(rf"^\s*(import {modulo}\b|from {modulo}[ .])", fonte, re.M):
            orfas.append(f"{nome} (procurei por `import {modulo}`)")
    assert not orfas, (
        "dependencias declaradas que nenhum modulo importa:\n  "
        + "\n  ".join(orfas))


def test_the_built_screen_travels_inside_the_package():
    """A Mission Control precisa chegar junto, ja construida.

    Construir na instalacao faria `uv tool install regente` exigir Node na
    maquina de quem so quer abrir a interface. Por isso o build vai versionado
    -- e por isso ele precisa estar DENTRO do diretorio que o wheel empacota.
    """
    ui = RAIZ / "regente" / "app" / "ui"
    assert (ui / "index.html").is_file(), (
        "a casca da tela nao esta no pacote; `regente ui` serviria um 404")
    assets = list((ui / "assets").glob("*.js")) if (ui / "assets").is_dir() else []
    assert assets, (
        "o pacote nao tem o JavaScript construido da tela: rode `npm run build` "
        "em ui/ e versione o resultado")

    empacotados = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert "regente" in empacotados, (
        "o wheel deixou de empacotar `regente/`, e a tela nao viajaria junto")


def test_the_html_the_package_ships_points_at_files_that_exist():
    """Um asset com hash que nao existe mais serve uma pagina em branco.

    O nome do arquivo muda a cada build. Versionar o `index.html` de um build e
    os assets de outro produz exatamente isso -- sem erro no servidor, e com
    404 no navegador de quem instalou.
    """
    ui = RAIZ / "regente" / "app" / "ui"
    html = (ui / "index.html").read_text(encoding="utf-8")
    referidos = set(re.findall(r'["\'](?:\./)?/?(assets/[\w.-]+)["\']', html))
    assert referidos, "o index.html nao referencia asset nenhum"
    faltando = [a for a in referidos if not (ui / a).is_file()]
    assert not faltando, (
        "o index.html versionado aponta para arquivos que nao existem: "
        + ", ".join(sorted(faltando))
        + " -- o `index.html` e os `assets/` sao de builds diferentes")


def test_the_session_placeholder_survived_the_build():
    """O servidor troca `{{SESSION_TOKEN}}` ao servir a pagina.

    Se o build parasse de emitir o marcador, a tela subiria sem token -- e toda
    escrita chegaria sem credencial, com a pessoa vendo recusas sem entender por
    que. O CSRF depende deste marcador estar no HTML entregue.
    """
    html = (RAIZ / "regente" / "app" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "{{SESSION_TOKEN}}" in html, (
        "o marcador da sessao sumiu do HTML construido")


def test_the_version_comes_from_the_installed_package():
    """Uma versao escrita a mao no codigo e uma segunda fonte que envelhece.

    E o que ela informaria seria a versao do CODIGO-FONTE, nunca a do que a
    pessoa instalou -- que e justamente o que se quer saber ao receber um
    relato de defeito.
    """
    cli = (RAIZ / "regente" / "cli.py").read_text(encoding="utf-8")
    assert "metadata.version(\"regente\")" in cli, (
        "`--version` deixou de perguntar ao pacote instalado")
    assert not re.search(r'^\s*__version__\s*=\s*["\']', cli, re.M), (
        "apareceu um `__version__` no codigo: duas fontes para a mesma versao")


# ===========================================================================
# OS INSTALADORES
# ===========================================================================

INSTALADORES = ("install.sh", "install.ps1")


@pytest.mark.parametrize("arquivo", INSTALADORES)
def test_the_installer_exists_and_installs_this_package(arquivo):
    texto = (RAIZ / arquivo).read_text(encoding="utf-8")
    assert "uv tool install" in texto, (
        f"{arquivo} nao instala como FERRAMENTA; `uv pip install` num ambiente "
        f"qualquer nao poe o comando no PATH")
    assert "uv tool update-shell" in texto, (
        f"{arquivo} nao ajusta o PATH -- e o `'regente' is not recognized` "
        f"volta na cara de quem seguiu tudo certo")


@pytest.mark.parametrize("arquivo", INSTALADORES)
def test_the_installer_warns_that_an_open_terminal_keeps_the_old_path(arquivo):
    """O erro mais provavel de quem acabou de instalar, e ele nao e um erro.

    Um terminal so le o PATH quando abre. Quem instalou numa janela ja aberta
    digita `regente` e ve "nao reconhecido" -- e conclui que a instalacao
    falhou. Dizer isso ANTES custa tres linhas.
    """
    texto = (RAIZ / arquivo).read_text(encoding="utf-8").lower()
    assert "terminal" in texto and ("novo" in texto or "nova" in texto), (
        f"{arquivo} nao avisa que um terminal ja aberto nao conhece o PATH novo")


@pytest.mark.parametrize("arquivo", INSTALADORES)
def test_the_installer_points_at_this_repository(arquivo):
    """Um instalador que aponta para outro lugar instala outra coisa."""
    url = _pyproject()["project"]["urls"]["Repositorio"]
    texto = (RAIZ / arquivo).read_text(encoding="utf-8")
    assert url in texto, (
        f"{arquivo} nao aponta para {url}; o pyproject e o instalador discordam "
        f"sobre de onde vem o produto")


@pytest.mark.parametrize("arquivo", INSTALADORES)
def test_the_installer_can_be_pointed_somewhere_else_without_being_edited(arquivo):
    """Instalar de um fork, de um branch ou de uma pasta local sem editar nada.

    Sem isto, testar uma versao candidata exige alterar o instalador -- e o que
    se testa deixa de ser o instalador que as pessoas vao usar.
    """
    texto = (RAIZ / arquivo).read_text(encoding="utf-8")
    for variavel in ("REGENTE_FROM", "REGENTE_REF", "REGENTE_REPO"):
        assert variavel in texto, f"{arquivo} nao aceita {variavel}"


def test_the_readme_tells_people_to_run_the_installer_that_exists():
    """O comando do README precisa baixar um arquivo que esta no repositorio."""
    readme = (RAIZ / "README.md").read_text(encoding="utf-8")
    for arquivo in INSTALADORES:
        assert arquivo in readme, (
            f"o README nao menciona {arquivo}; quem le nao descobre como instalar")
    # O caminho PRINCIPAL -- do titulo "## Instalar" ate o primeiro subtitulo --
    # nao pode voltar a pedir ambiente virtual. Ele continua documentado logo
    # abaixo, em "Se voce vai mexer no codigo", que e onde faz sentido.
    principal = readme.split("## Instalar", 1)[1].split("\n### ", 1)[0]
    assert "uv venv" not in principal, (
        "a instalacao principal voltou a pedir ambiente virtual")
    assert "install.sh" in principal and "install.ps1" in principal, (
        "a instalacao principal deixou de mostrar o comando de um passo")

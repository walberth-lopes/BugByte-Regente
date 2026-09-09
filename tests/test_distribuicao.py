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


# ===========================================================================
# A LICENCA E O ALVO DA PUBLICACAO
# ===========================================================================

def test_the_package_carries_a_licence():
    """Publicar sem licenca e publicar algo que ninguem pode usar.

    Sem licenca, o padrao legal e "todos os direitos reservados": o codigo fica
    visivel e legalmente intocavel. E a licenca vai anexada A VERSAO -- corrigir
    depois exige publicar outra, porque uma versao no indice nao se substitui.
    """
    projeto = _pyproject()["project"]
    assert projeto.get("license"), "o pacote nao declara licenca"
    arquivo = RAIZ / "LICENSE"
    assert arquivo.is_file(), "a licenca e declarada e o arquivo nao existe"
    assert "LICENSE" in projeto.get("license-files", []), (
        "o arquivo da licenca nao viaja no pacote")


def test_the_licence_file_is_the_one_the_metadata_declares():
    """O texto e a declaracao precisam ser a MESMA licenca.

    Declarar Apache-2.0 e distribuir o texto da MIT nao produz erro nenhum: o
    indice mostra uma coisa, o arquivo diz outra, e quem for usar descobre a
    divergencia no pior momento possivel.
    """
    declarada = _pyproject()["project"]["license"]
    texto = (RAIZ / "LICENSE").read_text(encoding="utf-8")
    marcas = {
        "Apache-2.0": "Apache License",
        "MIT": "MIT License",
        "AGPL-3.0": "GNU AFFERO GENERAL PUBLIC LICENSE",
        "GPL-3.0": "GNU GENERAL PUBLIC LICENSE",
        "BSD-3-Clause": "Redistribution and use in source and binary forms",
    }
    marca = marcas.get(declarada)
    assert marca, (
        f"licenca '{declarada}' declarada e nao reconhecida aqui; acrescente a "
        f"marca dela a este teste para a guarda continuar valendo")
    assert marca in texto, (
        f"o pyproject declara {declarada} e o arquivo LICENSE nao e essa licenca")


def test_nothing_blocks_the_upload_by_accident():
    """`Private :: Do Not Upload` recusa o upload em qualquer indice.

    Ele era a trava de quando nao havia decisao de publicar. Deixa-lo depois da
    decisao produz uma falha no meio do upload, com uma mensagem sobre
    classificadores que nao explica nada.
    """
    classificadores = _pyproject()["project"].get("classifiers", [])
    privados = [c for c in classificadores if c.startswith("Private ::")]
    assert not privados, (
        "sobrou um classificador que o indice recusa: " + ", ".join(privados))


def test_the_publish_target_is_declared_and_not_left_to_a_default():
    """Qual indice recebe a publicacao nao pode depender de um argumento.

    Esta guarda ja teve o efeito para o qual foi escrita: ela apontava para o
    TestPyPI enquanto a decisao era ensaiar, e FALHOU no dia de publicar de
    verdade -- que era o ponto, porque subir para o indice real passou a exigir
    uma alteracao deliberada aqui.

    O que ela protege agora e o mesmo principio de outro angulo: existe UM alvo,
    ele esta escrito no arquivo, e ninguem depende de lembrar
    `--publish-url` na hora. Uma versao publicada no PyPI real nao pode ser
    substituida, e a diferenca entre os dois indices nao pode ser um argumento
    esquecido.
    """
    uv = _pyproject().get("tool", {}).get("uv", {})
    alvos = [uv["publish-url"]] if uv.get("publish-url") else []
    alvos += [i["publish-url"] for i in uv.get("index", []) if i.get("publish-url")]
    assert len(alvos) == 1, (
        f"esperava exatamente um alvo de publicacao declarado, achei {alvos}")
    assert alvos[0].startswith("https://"), "o alvo nao e uma URL segura"


def test_the_version_is_not_one_already_published():
    """Um numero de versao vale UMA VEZ, e para sempre.

    Reaproveitar um numero com conteudo diferente torna ambiguo qualquer relato
    de defeito: "regente 0.1.0" deixa de identificar um artefato. Foi por isso
    que a primeira publicacao no indice real saiu como `0.1.1` -- o `0.1.0` ja
    existia no ensaio, com outro conteudo.

    A guarda nao alcanca a rede: ela confere o que sabemos localmente.
    """
    ja_usadas = {
        "0.1.0",               # publicada no TestPyPI durante o ensaio
        "0.1.1",               # a primeira no indice real
        "0.2.0",               # integracoes autodescobriveis
        "0.3.0",               # conectar em um clique
        "0.3.1",               # a fusao das telas
    }
    versao = _pyproject()["project"]["version"]
    assert versao not in ja_usadas, (
        f"a versao {versao} ja foi publicada em algum indice com outro "
        f"conteudo; suba o numero antes de publicar")


@pytest.mark.parametrize("arquivo", INSTALADORES)
def test_the_installer_teaches_the_flow_that_exists(arquivo):
    """O instalador ensina o proximo passo, e ele precisa ser o passo REAL.

    Ele mandava rodar tres comandos -- `init`, `access inicial` e `ui`. O `init`
    passou a fazer os tres, e uma instrucao desatualizada num instalador e pior
    que nenhuma: ela e a primeira coisa que a pessoa le, e ensina um fluxo que o
    produto nao tem mais.
    """
    texto = (RAIZ / arquivo).read_text(encoding="utf-8")
    assert "regente init" in texto, f"{arquivo} nao diz o primeiro comando"
    assert "regente access inicial" not in texto, (
        f"{arquivo} ainda manda conceder acesso a mao; o `init` ja faz isso")


@pytest.mark.parametrize("arquivo", INSTALADORES)
def test_the_installer_pulls_from_the_index_and_not_from_git(arquivo):
    """Instalar nao deveria depender do GitHub estar no ar.

    Enquanto o pacote nao existia num indice, puxar do repositorio era a unica
    forma. Agora ha `regente` no PyPI: e mais curto, nao clona nada, e nao
    quebra quando o repositorio muda de nome ou de visibilidade.

    O caminho do git continua acessivel por `REGENTE_REF` -- que e o que serve
    para provar uma versao ANTES de publica-la.
    """
    texto = (RAIZ / arquivo).read_text(encoding="utf-8")
    assert "REGENTE_REF" in texto, (
        f"{arquivo} perdeu a forma de instalar de um branch")
    assert "git+" in texto, f"{arquivo} perdeu o caminho do git por completo"
    # O padrao, quando ninguem pede nada, e o nome no indice.
    assert "'regente'" in texto or '"regente"' in texto or ":-regente}" in texto, (
        f"{arquivo} nao instala do indice por padrao")


def test_the_publish_target_is_not_an_index_used_for_resolution():
    """Publicar precisa de um ENDERECO DE ENVIO, e nao de um indice de leitura.

    Isto mordeu de verdade: um `[[tool.uv.index]]` chamado `pypi-real` apontando
    para `https://pypi.org/simple/` quebrava a resolucao DENTRO do repositorio.
    `uv tool install regente` respondia "not found in the package registry"
    aqui, e funcionava em qualquer outra pasta -- um defeito que so aparece para
    quem trabalha no proprio projeto.
    """
    uv = _pyproject().get("tool", {}).get("uv", {})
    assert uv.get("publish-url", "").startswith("https://"), (
        "o destino de publicacao nao esta declarado em [tool.uv]")
    for indice in uv.get("index", []):
        assert "pypi.org/simple" not in indice.get("url", ""), (
            "ha um indice declarado apontando para o PyPI publico; isso "
            "atrapalha a resolucao dentro do repositorio")


@pytest.mark.parametrize("arquivo", INSTALADORES)
def test_the_installer_guarantees_the_python_the_package_needs(arquivo):
    """Instalar uma ferramenta nao pode exigir instalar um interpretador antes.

    O Regente pede Python 3.13, e o uv baixa um quando a maquina nao tem -- foi
    medido: `Downloading cpython-3.13.15 (20.9MiB)`. Mas quem tiver
    `python-downloads = never` na configuracao do uv veria a instalacao falhar
    com uma mensagem sobre versao de interpretador, sem pista nenhuma de que o
    proprio uv resolveria aquilo.

    Por isso o instalador diz `automatic` explicitamente, em vez de torcer para
    o padrao valer.
    """
    texto = (RAIZ / arquivo).read_text(encoding="utf-8")
    assert "UV_PYTHON_DOWNLOADS" in texto, (
        f"{arquivo} nao garante que o uv possa baixar o Python necessario")
    assert "automatic" in texto, f"{arquivo} nao pede o download automatico"


def test_the_installer_says_a_python_may_be_downloaded():
    """Um download de 25 MB sem aviso parece a instalacao travada.

    Quem roda `curl | sh` ve o cursor parado e nao sabe se algo quebrou. Uma
    linha antes resolve.
    """
    for arquivo in INSTALADORES:
        texto = (RAIZ / arquivo).read_text(encoding="utf-8")
        assert "baixa um" in texto and "MB" in texto, (
            f"{arquivo} nao avisa que pode baixar um Python")

# -*- coding: utf-8 -*-
"""A CLI: superficie fina, fiacao verificada, codigos de saida com significado.

Este arquivo existe por causa de um defeito que aconteceu QUATRO VEZES:

    marco 13  `decide`  parser: opcao, --por      handler: args.option, args.per
    marco 6   `sombra`  parser: --minhas, --saida handler: args.mine, args.output
    marco 6   `cadeia`  parser: --saida           handler: args.output
    marco 6   `cadeia`  parser: --limite          handler: args.limit

Nas quatro a suite estava verde. A quarta foi encontrada pelo primeiro teste
deste arquivo, minutos depois de ele existir. A fiacao de argumentos nao tinha teste
nenhum, e ela e exatamente onde uma renomeacao deixa restos -- o handler
continua compilando, e o comando so morre quando alguem o roda.

O teste que fecha a classe inteira e o primeiro: ele compara, comando a
comando, o que o parser DEFINE com o que o handler LE.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

import pytest

from regente import cli

FONTE = Path("regente/cli.py")


# ---------------------------------------------------------------------------
# 1. A fiacao: nenhum handler le o que o parser nao define
# ---------------------------------------------------------------------------

def _lidos_por_handler() -> dict[str, set[str]]:
    """`args.X` que cada `cmd_*` le, sem os que tem default via getattr."""
    arvore = ast.parse(FONTE.read_text(encoding="utf-8"), filename=str(FONTE))
    saida: dict[str, set[str]] = {}
    for no in ast.walk(arvore):
        if not isinstance(no, ast.FunctionDef) or not no.name.startswith("cmd_"):
            continue
        nomes: set[str] = set()
        com_default: set[str] = set()
        for x in ast.walk(no):
            if (isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name)
                    and x.value.id == "args"):
                nomes.add(x.attr)
            if (isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
                    and x.func.id == "getattr" and len(x.args) >= 3
                    and isinstance(x.args[0], ast.Name) and x.args[0].id == "args"
                    and isinstance(x.args[1], ast.Constant)):
                com_default.add(x.args[1].value)
        saida[no.name] = nomes - com_default
    return saida


def _dests(p: argparse.ArgumentParser) -> set[str]:
    return {a.dest for a in p._actions if a.dest and a.dest != "help"}


def _subcomandos(p: argparse.ArgumentParser) -> dict:
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a.choices
    return {}


def test_no_handler_reads_an_argument_the_parser_never_defines():
    """A prova que faltava nas tres vezes em que isto quebrou."""
    parser = cli.build_parser()
    lidos = _lidos_por_handler()
    globais = _dests(parser)

    culpados = []
    for nome, sp in sorted(_subcomandos(parser).items()):
        fn = sp.get_default("fn")
        if fn is None or fn.__name__ not in lidos:
            continue
        disponiveis = globais | _dests(sp)
        falta = lidos[fn.__name__] - disponiveis
        if falta:
            culpados.append(f"'regente {nome}' -> {fn.__name__} le "
                            f"{sorted(falta)}, que o parser nao define")
    assert not culpados, ("a CLI morre com AttributeError nestes comandos:\n  "
                          + "\n  ".join(culpados))


def test_every_registered_command_has_a_handler():
    for nome, sp in sorted(_subcomandos(cli.build_parser()).items()):
        assert sp.get_default("fn") is not None, f"'{nome}' nao tem handler"


def test_every_handler_is_reachable_from_the_parser():
    """Um handler implementado e nao registrado e trabalho que ninguem alcanca."""
    parser = cli.build_parser()
    registrados = {sp.get_default("fn").__name__
                   for sp in _subcomandos(parser).values()
                   if sp.get_default("fn") is not None}
    implementados = set(_lidos_por_handler())
    orfaos = sorted(implementados - registrados)
    assert not orfaos, f"handlers sem comando: {orfaos}"


@pytest.mark.parametrize("comando", sorted(_subcomandos(cli.build_parser())))
def test_every_command_parses_its_own_help(comando, capsys):
    """`--help` monta o parser inteiro daquele comando. Um `add_argument`
    malformado aparece aqui, e nao no dia em que alguem usa a flag."""
    with pytest.raises(SystemExit) as saida:
        cli.main([comando, "--help"])
    assert saida.value.code == 0


# ---------------------------------------------------------------------------
# 2. Os codigos de saida SAO a interface para quem roteiriza
# ---------------------------------------------------------------------------

def test_the_exit_codes_are_distinct_and_none_is_success():
    codigos = {n: v for n, v in vars(cli).items()
               if n.startswith("EXIT_") and isinstance(v, int)}
    assert codigos["EXIT_OK"] == 0
    falhas = {n: v for n, v in codigos.items() if n != "EXIT_OK"}
    assert 0 not in falhas.values(), "uma falha devolve 0"
    assert len(set(falhas.values())) == len(falhas), \
        "dois motivos diferentes devolvem o mesmo codigo"


def test_an_unknown_refusal_never_becomes_success():
    """Vocabulario novo no motor nao pode virar `0` na CLI por omissao."""
    assert cli._exit_for("UMA_RECUSA_QUE_AINDA_NAO_EXISTE") == cli.EXIT_UNKNOWN
    assert cli._exit_for(None) == cli.EXIT_UNKNOWN
    assert cli._exit_for("") == cli.EXIT_UNKNOWN


def test_every_engine_refusal_has_an_exit_code():
    """Se o motor ganhar uma recusa, este teste pede o codigo dela."""
    from regente.engine.credentials import Refusal

    sem_codigo = [r.value for r in Refusal
                  if r.value not in cli.EXIT_POR_RECUSA]
    assert not sem_codigo, (
        f"recusas do motor sem codigo de saida: {sem_codigo}. "
        f"Sem isto elas caem em UNKNOWN, que e honesto mas inutil")


def test_authority_refusals_do_not_collapse_into_a_generic_error():
    """`nao tem autoridade` e `arquivo errado` mandam procurar em lugares
    diferentes. Um codigo unico obriga quem chama a ler texto e adivinhar."""
    assert cli._exit_for("POLICY_DENIED") != cli._exit_for("INVALID")
    assert cli._exit_for("NOT_FOUND") != cli._exit_for("POLICY_DENIED")
    assert cli._exit_for("REVOKED") != cli._exit_for("NOT_FOUND")
    assert cli._exit_for("SOURCE_UNAVAILABLE") != cli._exit_for("REVOKED"), \
        "nao deu para perguntar nao e a mesma coisa que a credencial nao serve"


# ---------------------------------------------------------------------------
# 3. `init` produz uma pasta que REALMENTE sobe
# ---------------------------------------------------------------------------

def _regente(*args, cwd):
    p = subprocess.run([sys.executable, "-m", "regente", *args], cwd=str(cwd),
                       capture_output=True, encoding="utf-8", errors="replace",
                       timeout=300)
    return p.returncode, ((p.stdout or "") + (p.stderr or ""))


def test_the_module_is_runnable_before_anything_is_on_the_path(tmp_path):
    """`python -m regente` -- a primeira coisa que alguem tenta quando o
    comando 'nao e reconhecido'. Sem `__main__.py`, era uma segunda parede."""
    rc, saida = _regente("--help", cwd=tmp_path)
    assert rc == 0
    assert "usage: regente" in saida


@pytest.mark.slow
def test_init_produces_a_workspace_that_starts(tmp_path):
    """O passo 2 e o passo 3 do tutorial, literalmente.

    Ate o marco 6, `init` escrevia `policies: ../policies/default.yaml` -- um
    caminho que so resolve dentro da arvore do codigo-fonte. Quem seguia o
    tutorial recebia um `ValueError` cru no comando seguinte.
    """
    rc, saida = _regente("init", cwd=tmp_path)
    assert rc == 0, saida
    assert (tmp_path / "regente.yaml").is_file()
    assert (tmp_path / "policies.yaml").is_file(), \
        "init escreveu uma configuracao que aponta para policies inexistente"
    assert (tmp_path / "tasks").is_dir()

    rc, saida = _regente("doctor", cwd=tmp_path)
    assert rc == 0, f"`doctor` nao passa numa pasta recem-criada:\n{saida}"
    assert "tudo pronto" in saida


@pytest.mark.slow
def test_the_shipped_policies_match_the_repositorys_own(tmp_path):
    """Duas copias de uma regra de seguranca divergem, e a que diverge e a que
    o usuario novo recebe."""
    enviado = Path("regente/resources/policies.yaml.example").read_text(
        encoding="utf-8")
    proprio = Path("policies/default.yaml").read_text(encoding="utf-8")
    assert enviado == proprio, \
        "o arquivo de policies enviado a quem instala divergiu do do repositorio"


@pytest.mark.slow
def test_init_refuses_to_overwrite_and_says_so(tmp_path):
    assert _regente("init", cwd=tmp_path)[0] == 0
    rc, saida = _regente("init", cwd=tmp_path)
    assert rc == cli.EXIT_CONFLICT
    assert "ja existe" in saida


# ---------------------------------------------------------------------------
# 4. A CLI nao e autoridade
# ---------------------------------------------------------------------------

def test_the_cli_cannot_be_told_who_is_acting():
    """Identidade digitada nao e identidade.

    Nenhum comando aceita um ator, um workspace ou uma capacidade por
    argumento. Se aceitasse, a auditoria gravaria o texto que a pessoa quisesse
    -- que foi exatamente o `--por` removido no marco 14.
    """
    parser = cli.build_parser()
    # Nomes que designariam QUEM AGE. O ALVO de uma acao e outra coisa:
    # `access conceder <principal>` nomeia quem RECEBE a concessao, e o marco
    # 14 separou as duas colunas de proposito -- "alice concede a bob" e "bob
    # concede a bob" sao fatos diferentes. Um guard que confundisse os dois
    # acusaria o comando por existir.
    proibidos = {"por", "ator", "actor", "usuario", "user", "as_user",
                 "workspace_id", "client_id", "abilities", "granted_by",
                 "identidade", "sujeito", "subject"}
    culpados = []
    for nome, sp in sorted(_subcomandos(parser).items()):
        for dest in _dests(sp):
            if dest in proibidos:
                culpados.append(f"regente {nome} --{dest}")
    assert not culpados, ("a CLI deixa falsificar identidade ou escopo:\n  "
                          + "\n  ".join(culpados))


def test_the_cli_resolves_no_secret_and_builds_no_adapter_by_hand():
    """A CLI chama SERVICO. Se resolvesse referencia ou montasse adapter de
    escrita, seria uma segunda autoridade ao lado do motor."""
    arvore = ast.parse(FONTE.read_text(encoding="utf-8"), filename=str(FONTE))
    culpados = []
    for no in ast.walk(arvore):
        if not isinstance(no, ast.Call):
            continue
        chamada = ast.unparse(no.func)
        if chamada.endswith(".resolve") and "secret" in ast.unparse(no).lower():
            culpados.append(f"linha {no.lineno}: {chamada}")
        if chamada.endswith(".material"):
            culpados.append(f"linha {no.lineno}: pede material de credencial")
        if "registry.create" in chamada:
            culpados.append(f"linha {no.lineno}: monta adapter direto")
    assert not culpados, ("a CLI virou autoridade:\n  " + "\n  ".join(culpados))


def test_the_cli_never_prints_a_whole_environment():
    """Um `print(os.environ)` de depuracao despeja toda credencial da maquina."""
    arvore = ast.parse(FONTE.read_text(encoding="utf-8"), filename=str(FONTE))
    for no in ast.walk(arvore):
        if (isinstance(no, ast.Call) and isinstance(no.func, ast.Name)
                and no.func.id == "print"):
            texto = ast.unparse(no)
            assert "environ" not in texto, f"linha {no.lineno}: {texto[:70]}"
            assert ".env" not in texto or "environment" in texto, \
                f"linha {no.lineno}: {texto[:70]}"


# ===========================================================================
# `regente init`: o comando que precisa RETORNAR
# ===========================================================================

def test_init_never_blocks_waiting_for_an_answer(tmp_path):
    """Sem `--perguntar`, o comando nao le a entrada e nao trava.

    A versao anterior decidia isso olhando `sys.stdin.isatty()`, e essa base se
    mostrou ruim: no Git Bash do Windows, `< /dev/null` responde que E terminal
    e um cano responde que NAO e -- entao ora o comando ficava esperando uma
    tecla que nunca vinha, ora descartava em silencio respostas enviadas.

    O `timeout` do subprocesso e o teste: se ele estourar, o comando bloqueou.
    """
    rc, saida = _regente("init", cwd=tmp_path)
    assert rc == 0, saida
    assert "usando os nomes padrao" in saida, (
        "o comando escolheu nomes por conta propria e nao contou quais")


def test_init_never_leaves_a_server_running(tmp_path):
    """`init` sozinho NAO abre a Mission Control.

    `cmd_ui` sobe um servidor e nao retorna nunca. Abrir a tela por padrao
    travou a suite inteira uma vez, num `regente init` de subprocesso servindo
    HTTP -- sem erro e sem pista. Abrir passou a exigir `--ui`, ou uma resposta
    em `--perguntar`.
    """
    rc, saida = _regente("init", cwd=tmp_path)
    assert rc == 0, saida
    assert "proximo: regente ui" in saida, (
        "o comando deveria dizer o proximo passo em vez de tomar o terminal")


def test_init_writes_the_names_it_was_given(tmp_path):
    """Os tres nomes DERIVAM o id do workspace, e por isso importam.

    Renomear depois cria outro workspace, vazio, sem aviso -- entao escolher no
    `init` e o unico momento barato.
    """
    rc, saida = _regente("init", "--organizacao", "acme", "--cliente", "acme",
                         "--workspace", "api", cwd=tmp_path)
    assert rc == 0, saida
    escrito = (tmp_path / "regente.yaml").read_text(encoding="utf-8")
    assert "organization: acme" in escrito
    assert "client: acme" in escrito
    assert "workspace: api" in escrito


def test_init_leaves_the_workspace_usable_by_both_identities(tmp_path):
    """Uma pessoa que roda `init` nao deveria precisar de mais dois comandos.

    O terminal autentica pela conta do sistema; a Mission Control autentica por
    um token local nomeado pelo cliente. Conceder so a primeira era o que fazia
    a tela abrir autenticada e sem poder fazer nada -- e obrigava a um segundo
    comando que ninguem tinha como adivinhar.

    As duas concessoes passam pelo `AccessService`, com policy e auditoria: o
    que muda e deixarem de ser trabalho manual, e nao a barreira.
    """
    rc, saida = _regente("init", "--organizacao", "acme", "--cliente", "acme",
                         "--workspace", "api", cwd=tmp_path)
    assert rc == 0, saida
    assert "voce e o dono" in saida
    assert "dev-token:acme" in saida, (
        "a identidade da tela ficou sem acesso; a Mission Control abriria "
        "autenticada e impotente")

    rc, listado = _regente("access", "listar", cwd=tmp_path)
    assert rc == 0, listado
    assert "os-account:" in listado and "dev-token:acme" in listado


def test_init_twice_does_not_disturb_the_access_already_granted(tmp_path):
    """Refazer o `init` nao pode tirar acesso de ninguem.

    A porta do `bootstrap` fecha depois da primeira concessao, e o comando
    precisa tratar essa recusa como "ja esta pronto", e nao como falha.
    """
    assert _regente("init", cwd=tmp_path)[0] == 0
    rc, saida = _regente("init", "--force", cwd=tmp_path)
    assert rc == 0, saida
    assert "ja tem dono" in saida

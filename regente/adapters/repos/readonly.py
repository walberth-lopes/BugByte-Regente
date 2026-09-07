# -*- coding: utf-8 -*-
"""O que conta como leitura, por INVOCACAO -- nunca por verbo.

Este modulo existe por causa de um defeito medido em 06/09/2026, e a licao vale
para qualquer adapter futuro que dispare um processo externo:

    Permitir o verbo `repo` deixou passar `repo delete`.
    Permitir o verbo `remote` deixaria passar `remote set-url`.
    Permitir `symbolic-ref` deixaria passar a forma de DOIS argumentos, que grava.

Uma allowlist na granularidade errada e pior do que nenhuma: ela produz confianca
sem produzir garantia. A unica forma honesta e listar **invocacoes inteiras** e
recusar tudo o que nao casar exatamente -- inclusive formas novas do mesmo verbo
que apareçam numa versao futura da ferramenta.

Regra de ouro deste arquivo: na duvida, recusa. Um comando de leitura recusado
custa um `AdapterErro` legivel; um comando de escrita aceito custa um repositorio.
"""

from __future__ import annotations

#: Subcomandos de git cuja forma NENHUMA muta o repositorio.
#: Conferido um a um: nao existe variante de escrita nestes.
GIT_ALWAYS_READ: frozenset[str] = frozenset({
    "rev-parse", "for-each-ref", "log", "ls-tree", "cat-file", "show", "status",
})

#: Flags que gravam, em qualquer subcomando de git.
GIT_WRITE_FLAGS: frozenset[str] = frozenset({
    "-d", "-D", "--delete", "--unset", "--unset-all", "--add", "--replace-all",
    "--edit", "-w", "--write", "--force", "-f", "--set", "--short-hand-set",
})

#: Flags que fazem a CLI de hospedagem enviar POST mesmo sem `--method`.
#: Esta e a armadilha: `api -f chave=valor rota` NAO e uma leitura.
CLI_WRITE_FLAGS: frozenset[str] = frozenset({
    "-f", "-F", "--field", "--raw-field", "--input", "--method", "-X",
})

#: Invocacoes permitidas na CLI de hospedagem, por par (comando, subcomando).
#: `api` entra com subcomando None porque a rota e livre -- e por isso ele leva
#: uma checagem extra de metodo.
CLI_READ_INVOCATIONS: frozenset[tuple[str, str | None]] = frozenset({
    ("repo", "list"), ("repo", "view"),
    ("pr", "list"), ("pr", "view"), ("pr", "diff"), ("pr", "checks"),
    ("auth", "status"),
    ("api", None),
    # Perguntar a versao. Nao contata nada, nao le configuracao e nao precisa
    # de credencial -- e por isso e a checagem certa para `doctor`, que prova
    # que a ferramenta EXISTE E RODA sem transformar saude em autenticacao.
    ("--version", None),
})


def git_is_read(args: list[str] | tuple[str, ...]) -> tuple[bool, str]:
    """Devolve (permitido, motivo da recusa)."""
    if not args:
        return False, "invocacao vazia"
    cmd, resto = args[0], list(args[1:])

    proibidas = [a for a in resto if a in GIT_WRITE_FLAGS]
    if proibidas:
        return False, f"flag de escrita: {', '.join(proibidas)}"

    if cmd in GIT_ALWAYS_READ:
        return True, ""

    if cmd == "remote":
        # `get-url` le; `add`, `remove`, `set-url`, `rename`, `prune` gravam.
        if resto[:1] == ["get-url"]:
            return True, ""
        return False, "so 'remote get-url' e leitura"

    if cmd == "symbolic-ref":
        # Uma ref = leitura. Duas = grava a ref. A diferenca e so a aridade,
        # e e por isso que um allowlist por verbo nao poderia captura-la.
        refs = [a for a in resto if not a.startswith("-")]
        if len(refs) != 1:
            return False, f"symbolic-ref com {len(refs)} refs grava; leitura tem 1"
        return True, ""

    if cmd == "config":
        # `--get` e `--list` leem; qualquer outra forma grava.
        if any(a in ("--get", "--get-all", "--list", "-l") for a in resto):
            return True, ""
        return False, "so 'config --get/--list' e leitura"

    if cmd == "branch":
        if any(a in ("--list", "-l", "--show-current") for a in resto):
            return True, ""
        return False, "so 'branch --list/--show-current' e leitura"

    return False, f"'git {cmd}' nao esta na lista de leitura"


def cli_is_read(args: list[str] | tuple[str, ...]) -> tuple[bool, str]:
    """Devolve (permitido, motivo da recusa)."""
    if not args:
        return False, "invocacao vazia"
    cmd = args[0]
    sub = args[1] if len(args) > 1 and not args[1].startswith("-") else None

    if cmd == "api":
        if ("api", None) not in CLI_READ_INVOCATIONS:
            return False, "'api' nao esta liberado"
        for i, a in enumerate(args):
            base = a.split("=", 1)[0]
            if base not in CLI_WRITE_FLAGS:
                continue
            if base in ("--method", "-X"):
                metodo = (a.split("=", 1)[1] if "=" in a
                          else (args[i + 1] if i + 1 < len(args) else "")).upper()
                if metodo and metodo != "GET":
                    return False, f"metodo {metodo} nao e leitura"
                continue
            # -f/-F/--input fazem a CLI enviar POST mesmo sem --method.
            return False, f"'{base}' faz a chamada virar escrita"
        return True, ""

    if (cmd, sub) in CLI_READ_INVOCATIONS:
        return True, ""
    return False, f"'{(cmd + ' ' + (sub or '')).strip()}' nao esta na lista de leitura"


# ---------------------------------------------------------------------------
# WRITE invocations. A SEPARATE list, never a widening of the read one.
# ---------------------------------------------------------------------------
#
# Kept apart on purpose. If writes were added by relaxing `CLI_READ_INVOCATIONS`,
# then every future read would inherit write reach, and the read gate would stop
# meaning anything. Two lists means a caller must say which door it is asking
# for, and the read door can never accidentally open the write one.
#
# Same granularity rule as the read list, for the same measured reason: allowing
# the verb `repo` once let `repo delete` through.

#: Exactly the write invocations this milestone permits. Nothing else.
CLI_WRITE_INVOCATIONS: frozenset[tuple[str, str | None]] = frozenset({
    ("pr", "create"),
})

#: Write invocations that exist, are understood, and stay forbidden here.
#: Listed rather than merely absent so a reader can see they were considered --
#: an empty absence looks like an oversight, a named refusal looks like a rule.
CLI_FORBIDDEN_INVOCATIONS: frozenset[tuple[str, str]] = frozenset({
    ("pr", "merge"), ("pr", "close"), ("pr", "reopen"), ("pr", "edit"),
    ("pr", "review"), ("pr", "ready"), ("pr", "comment"), ("pr", "lock"),
    ("repo", "create"), ("repo", "delete"), ("repo", "edit"), ("repo", "archive"),
    ("release", "create"), ("workflow", "run"), ("workflow", "enable"),
    ("run", "rerun"), ("run", "cancel"), ("secret", "set"), ("api", "delete"),
})


def cli_is_allowed_write(args: list[str] | tuple[str, ...]) -> tuple[bool, str]:
    """Devolve (permitido, motivo da recusa) para uma invocacao de ESCRITA."""
    if not args:
        return False, "invocacao vazia"
    cmd = args[0]
    sub = args[1] if len(args) > 1 and not args[1].startswith("-") else None

    if (cmd, sub) in CLI_FORBIDDEN_INVOCATIONS:
        return False, f"'{cmd} {sub}' is explicitly forbidden in this milestone"
    if (cmd, sub) not in CLI_WRITE_INVOCATIONS:
        return False, (f"'{(cmd + ' ' + (sub or '')).strip()}' is not among the "
                       f"permitted writes")

    # A permitted write may still carry a forbidden shape. `--body-file` is fine;
    # `--web` opens a browser and takes the operation out of the engine's sight,
    # which means the engine could no longer say what it did.
    for a in args:
        base = a.split("=", 1)[0]
        if base in ("--web", "-w"):
            return False, "'--web' takes the operation outside the engine's view"
        if base in ("--fill", "--fill-first"):
            return False, ("'--fill' derives the body from commits; the engine "
                           "must state the body it is responsible for")
    return True, ""

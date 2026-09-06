# -*- coding: utf-8 -*-
"""Internal identifiers.

The prefix is not decoration: in a log or an error message it makes it obvious
when someone passed a run id where a task id was expected. That kind of mix-up
is silent when everything is an opaque string.
"""

from __future__ import annotations

import uuid

ORG = "org"
CLIENT = "cli"
WORKSPACE = "wks"
PROJECT = "prj"
REPO = "rep"
TASK = "tsk"
RUN = "run"
EVENT = "evt"
APPROVAL = "apv"
ACTION = "act"


def new_id(prefixo: str) -> str:
    return f"{prefixo}_{uuid.uuid4().hex[:12]}"


def prefix_of(identificador: str) -> str:
    return identificador.split("_", 1)[0] if "_" in identificador else ""


def expect_prefix(identificador: str, prefixo_esperado: str) -> str:
    obtido = prefix_of(identificador)
    if obtido != prefixo_esperado:
        raise ValueError(
            f"expected a '{prefixo_esperado}' id and got '{obtido}': {identificador}"
        )
    return identificador

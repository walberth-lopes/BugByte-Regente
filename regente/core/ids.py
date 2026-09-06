# -*- coding: utf-8 -*-
"""Identificadores internos.

O prefixo nao e enfeite: ele deixa obvio, num log ou numa mensagem de error, se
alguem passou um id de run onde se esperava um id de task. Esse tipo de troca e
silencioso quando tudo e string opaca.
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
            f"esperava id de '{prefixo_esperado}' e recebi '{obtido}': {identificador}"
        )
    return identificador

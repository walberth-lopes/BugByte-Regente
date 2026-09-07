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
DELIVERY = "dlv"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def prefix_of(identifier: str) -> str:
    return identifier.split("_", 1)[0] if "_" in identifier else ""


def expect_prefix(identifier: str, expected_prefix: str) -> str:
    got = prefix_of(identifier)
    if got != expected_prefix:
        raise ValueError(
            f"expected a '{expected_prefix}' id and got '{got}': {identifier}"
        )
    return identifier

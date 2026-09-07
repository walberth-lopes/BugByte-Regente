# -*- coding: utf-8 -*-
"""The shadow run, executed rather than inspected.

`shadow.execute()` had no test at all, and the gap cost exactly what an untested
entry point costs: `plan = plan(...)` made the name local to the function, so the
call on the right-hand side raised UnboundLocalError against the `plan` imported
from `core.scheduling`. Every `regente shadow` died on that line.

It survived because the only caller is the CLI, and the CLI was itself dead on an
argument mismatch two hundred lines earlier -- one broken layer hiding the next.
So this test calls the function, rather than reading it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from regente.adapters.tasks.filesystem import FilesystemTasks
from regente.core.scheduling import Limits
from regente.engine import shadow


def _provider(tmp_path: Path) -> FilesystemTasks:
    pasta = tmp_path / "tasks"
    pasta.mkdir()
    body = [
        {"key": "K-1", "title": "first", "status": "TO DO", "description": "does something"},
        {"key": "K-2", "title": "second", "status": "TO DO",
         "depends_on": [{"key": "K-1"}], "description": "waits for the first"},
        {"key": "K-3", "title": "third", "status": "DONE", "description": "already finished"},
    ]
    for d in body:
        (pasta / f"{d['key']}.yaml").write_text(
            yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return FilesystemTasks(pasta)


def test_shadow_run_produces_a_plan(tmp_path):
    """The regression: this call raised UnboundLocalError before reaching a plan."""
    r = shadow.execute(_provider(tmp_path), Limits(max_workers=2))

    # K-3 is DONE and never arrives: `filesystem` drops finished work in
    # `list_tasks`, so the shadow's own `ignored` counter stays at zero here. A
    # provider with a broader query (a loose JQL) is what makes it move.
    assert r.discovered == 2
    assert r.relevant == 2
    assert r.ignored == 0

    assert r.plan is not None
    assert r.plan.dispatch == ("K-1",)
    assert r.ready == 1
    assert r.blocked == 1           # K-2 depends on K-1


def test_shadow_declares_why_the_others_stayed_out(tmp_path):
    """A deferral reason grouped by cause is the point of the report."""
    r = shadow.execute(_provider(tmp_path), Limits(max_workers=2))
    assert any("depends on" in a.reason for a in r.plan.deferred)
    assert r.deferral_reasons


def test_shadow_mutates_nothing(tmp_path):
    """The whole promise of the mode, asserted rather than assumed."""
    provider = _provider(tmp_path)
    before = {p: p.read_bytes() for p in (tmp_path / "tasks").iterdir()}

    r = shadow.execute(provider, Limits(max_workers=2))

    assert r.mutations == 0
    assert {p: p.read_bytes() for p in (tmp_path / "tasks").iterdir()} == before


def test_shadow_report_renders(tmp_path):
    """`render` is what the CLI prints; it must survive a real report."""
    text = shadow.render(shadow.execute(_provider(tmp_path), Limits(max_workers=2)))
    assert "TASK PROVIDER SHADOW REPORT" in text
    assert "Mutations performed          0" in text
    assert "K-1" in text

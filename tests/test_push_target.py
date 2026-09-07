# -*- coding: utf-8 -*-
"""Where a push from an isolated area may go.

This file exists because of a defect found while proposing Milestone 6, and the
defect is a good example of a dangerous default: git sets `origin` to whatever it
cloned from. An area cloned from a local path therefore comes **pre-aimed at the
source checkout** -- so the configuration that looks safest (clone locally, it is
faster) is the one that would write into somebody's working repository.

Three properties are enforced here, and the third is the one that matters most:

  1. With a remote configured, the push target IS that remote.
  2. The local source never survives as a push target.
  3. With NO remote configured, the push target is **absent** -- a push is
     impossible rather than accidentally local. Absence of configuration must
     fail closed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from regente.adapters.workspace.local import GitClone

REMOTE = "https://github.com/acme/thing.git"


def _make_repo(root: Path, name: str = "source") -> Path:
    p = Path(root) / name
    p.mkdir(parents=True)
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=str(p), check=True, capture_output=True)
    (p / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(p), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(p), check=True,
                   capture_output=True)
    return p


def _remotes(path: str) -> str:
    return subprocess.run(["git", "remote", "-v"], cwd=str(path),
                          capture_output=True, encoding="utf-8").stdout


def test_the_push_target_is_the_real_remote_not_the_clone_source(tmp_path):
    source = _make_repo(tmp_path)
    areas = GitClone(root=tmp_path / "areas", sources={"acme/thing": str(source)},
                     remotes={"acme/thing": REMOTE})
    area = areas.prepare("K-1", repo="acme/thing", branch="regente/k-1", base="main")

    assert areas.push_target(area) == REMOTE
    assert str(source) not in _remotes(area.path), \
        "the local source survived as a git remote"


def test_the_source_path_is_never_a_push_target(tmp_path):
    """The exact defect: cloning locally aims `origin` at the source."""
    source = _make_repo(tmp_path)
    areas = GitClone(root=tmp_path / "areas", sources={"acme/thing": str(source)},
                     remotes={"acme/thing": REMOTE})
    area = areas.prepare("K-1", repo="acme/thing", branch="regente/k-1", base="main")

    target = areas.push_target(area)
    assert target is not None
    assert not Path(target).drive, f"push target is a local path: {target}"
    assert target.startswith(("https://", "git@", "ssh://"))


def test_without_a_configured_remote_a_push_is_impossible(tmp_path):
    """Fails closed. Absence of configuration must not mean 'push locally'."""
    source = _make_repo(tmp_path)
    areas = GitClone(root=tmp_path / "areas", sources={"acme/thing": str(source)})
    area = areas.prepare("K-1", repo="acme/thing", branch="regente/k-1", base="main")

    assert areas.push_target(area) is None
    assert _remotes(area.path).strip() == "", "a remote survived with no target"

    # And git itself agrees: the push cannot even be attempted.
    p = subprocess.run(["git", "push", "origin", "regente/k-1"], cwd=area.path,
                       capture_output=True, encoding="utf-8")
    assert p.returncode != 0
    assert "origin" in (p.stderr or "").lower()


def test_the_area_records_its_push_target_for_inspection(tmp_path):
    source = _make_repo(tmp_path)
    areas = GitClone(root=tmp_path / "areas", sources={"acme/thing": str(source)},
                     remotes={"acme/thing": REMOTE})
    area = areas.prepare("K-1", repo="acme/thing", branch="regente/k-1", base="main")
    assert area.data["push_target"] == REMOTE


def test_the_source_repository_is_untouched_by_the_retarget(tmp_path):
    source = _make_repo(tmp_path)
    before = subprocess.run(["git", "rev-parse", "main"], cwd=str(source),
                            capture_output=True, encoding="utf-8").stdout.strip()
    areas = GitClone(root=tmp_path / "areas", sources={"acme/thing": str(source)},
                     remotes={"acme/thing": REMOTE})
    areas.prepare("K-1", repo="acme/thing", branch="regente/k-1", base="main")

    after = subprocess.run(["git", "rev-parse", "main"], cwd=str(source),
                           capture_output=True, encoding="utf-8").stdout.strip()
    assert after == before
    status = subprocess.run(["git", "status", "--porcelain"], cwd=str(source),
                            capture_output=True, encoding="utf-8").stdout.strip()
    assert status == ""
    branches = subprocess.run(["git", "branch", "--format=%(refname:short)"],
                              cwd=str(source), capture_output=True,
                              encoding="utf-8").stdout.split()
    assert branches == ["main"]


def test_a_resumed_area_keeps_its_push_target(tmp_path):
    """Resumption must not silently re-aim the area at the source."""
    source = _make_repo(tmp_path)
    areas = GitClone(root=tmp_path / "areas", sources={"acme/thing": str(source)},
                     remotes={"acme/thing": REMOTE})
    areas.prepare("K-1", repo="acme/thing", branch="regente/k-1", base="main")
    again = areas.prepare("K-1", repo="acme/thing", branch="regente/k-1", base="main")

    assert again.data.get("resumed") is True
    assert areas.push_target(again) == REMOTE

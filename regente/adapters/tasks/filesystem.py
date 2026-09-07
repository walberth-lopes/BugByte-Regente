# -*- coding: utf-8 -*-
"""TaskProvider that reads work from YAML files on disk.

Not a simulation of another provider: it is a real one, useful for anyone who
describes work in a versioned file, and it serves as the second implementation
proving the Core Engine does not know what Jira is. The Jira adapter slots in
alongside this one without a single line of the engine changing -- if it has to
change, the abstraction was wrong.

Real writing: `transition_task` and `add_comment` write into the file itself. An
adapter that accepts a write and does nothing hides exactly the defect it matters
most to find early.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ...ports import AdapterError


def _field(data: dict[str, Any], name: str, legacy: str) -> Any:
    """Reads the en-US key, accepting the old pt-BR one.

    A task file somebody already wrote must not stop being readable because the
    project standardised its vocabulary. The new name wins when both exist.
    """
    value = data.get(name)
    return value if value is not None else data.get(legacy)
from ...ports.tasks import (BLOCKS, PARENT, RELATED, Comment, ExternalTask,
                            ExternalStatus, TaskProvider, TaskRef)


class FilesystemTasks(TaskProvider):
    name = "filesystem"

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    def verify(self) -> None:
        if not self.dir.is_dir():
            raise AdapterError(f"task directory does not exist: {self.dir}")

    # ---- reading ---------------------------------------------------------

    def _files(self) -> list[Path]:
        return sorted([*self.dir.glob("*.yaml"), *self.dir.glob("*.yml")])

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            # A broken file rises as an error. Skipping it silently would make
            # the task vanish from the board without anyone noticing.
            raise AdapterError(f"{path.name} could not be read: {e}") from e
        if not isinstance(data, dict):
            raise AdapterError(f"{path.name} does not contain a mapping")
        return data

    #: This format's vocabulary -> the engine's vocabulary. Whoever writes the
    #: YAML chooses the text; the map is the contract. A status outside the map
    #: becomes UNKNOWN and rises as an anomaly -- it is never coerced into
    #: the neighbouring value. The keys are status text from user files and stay
    #: as they are.
    STATUS_MAP: dict[str, ExternalStatus] = {
        "TO DO": ExternalStatus.NOT_STARTED,
        "TODO": ExternalStatus.NOT_STARTED,
        "BACKLOG": ExternalStatus.NOT_STARTED,
        "ANALISE": ExternalStatus.IN_ANALYSIS,
        "PLANNING": ExternalStatus.IN_ANALYSIS,
        "DOING": ExternalStatus.IN_PROGRESS,
        "CODING": ExternalStatus.IN_PROGRESS,
        "IN PROGRESS": ExternalStatus.IN_PROGRESS,
        "REVIEW": ExternalStatus.IN_REVIEW,
        "REVIEWING": ExternalStatus.IN_REVIEW,
        "QA": ExternalStatus.IN_VALIDATION,
        "DONE": ExternalStatus.COMPLETED,
        "CANCELLED": ExternalStatus.CANCELLED,
        "CANCELADA": ExternalStatus.CANCELLED,
    }

    def _build(self, path: Path, data: dict[str, Any]) -> ExternalTask:
        # `depends_on` is, by definition, blocking -- the only link kind this
        # format expresses. `related` lives in `related`/`relacionadas`, which
        # creates no execution order.
        links = tuple(
            TaskRef(key=str(v["key"]) if isinstance(v, dict) else str(v),
                    kind=str(v.get("tipo", BLOCKS)) if isinstance(v, dict) else BLOCKS)
            for v in (_field(data, "depends_on", "depende_de") or [])
        ) + tuple(
            TaskRef(key=str(v), kind=RELATED)
            for v in (_field(data, "related", "relacionadas") or [])
        ) + tuple(
            # Hierarchy, not order. Without it this format cannot express that
            # two tasks are siblings -- and sibling evidence is one of the few
            # honest signals for locating work whose repository nobody declared.
            TaskRef(key=str(v), kind=PARENT)
            for v in ([data["parent"]] if data.get("parent") else [])
        )
        raw_status = str(_field(data, "status", "estado") or "TO DO")
        return ExternalTask(
            key=str(data.get("key") or path.stem),
            title=str(_field(data, "title", "titulo") or path.stem),
            status=self.STATUS_MAP.get(raw_status.strip().upper(), ExternalStatus.UNKNOWN),
            external_status=raw_status,
            description=str(_field(data, "description", "descricao") or ""),
            url=data.get("url"),
            priority=int(_field(data, "priority", "prioridade") or 100),
            project=str(_field(data, "project", "projeto") or ""),
            assignee=_field(data, "assignee", "responsavel"),
            links=links,
            resources=tuple(str(r) for r in (data.get("resources") or [])),
            labels=tuple(str(r) for r in (data.get("labels") or [])),
            data={"file": str(path)})

    def list_tasks(self, filters: dict[str, Any] | None = None) -> list[ExternalTask]:
        self.verify()
        ready = []
        excluded = set((filters or {}).get("exclude_states", ["DONE", "CANCELLED"]))
        for path in self._files():
            t = self._build(path, self._read(path))
            if t.external_status.upper() not in excluded:
                ready.append(t)
        return ready

    def _path_for(self, key: str) -> Path:
        for path in self._files():
            if str(self._read(path).get("key") or path.stem) == key:
                return path
        raise AdapterError(f"task {key} not found in {self.dir}")

    def get_task(self, key: str) -> ExternalTask:
        path = self._path_for(key)
        return self._build(path, self._read(path))

    def get_comments(self, key: str) -> list[Comment]:
        data = self._read(self._path_for(key))
        return [Comment(author=str(_field(c, "author", "autor") or "?"),
                        text=str(_field(c, "text", "texto") or ""),
                        created_at=str(_field(c, "created_at", "em") or ""), id=str(c.get("id", "")))
                for c in (_field(data, "comments", "comentarios") or [])]

    # ---- writing ---------------------------------------------------------

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        # Write to a temporary file and swap: an interruption halfway must not
        # leave the file half-written, because the file is the provider's state.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
        tmp.replace(path)

    def update_task(self, key: str, fields: dict[str, Any]) -> None:
        path = self._path_for(key)
        data = self._read(path)
        data.update(fields)
        self._write(path, data)

    def transition_task(self, key: str, destination: str) -> None:
        self.update_task(key, {"status": destination})

    def add_comment(self, key: str, text: str) -> None:
        path = self._path_for(key)
        data = self._read(path)
        data.setdefault("comments", []).append({"author": "regente", "text": text})
        self._write(path, data)

    def add_label(self, key: str, label: str) -> None:
        path = self._path_for(key)
        data = self._read(path)
        labels = data.setdefault("labels", [])
        if label not in labels:
            labels.append(label)
        self._write(path, data)

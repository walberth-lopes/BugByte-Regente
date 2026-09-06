# -*- coding: utf-8 -*-
"""TaskProvider que le trabalho de arquivos YAML no disco.

Nao e simulacao de outro provider: e um provedor de verdade, util para quem
descreve trabalho em arquivo versionado, e serve de segunda implementacao para
provar que o Core Engine nao sabe o que e Jira. O adapter de Jira entra ao lado
deste sem que nenhuma linha do motor mude -- se mudar, a abstracao estava errada.

Escrita de verdade: `transition_task` e `add_comment` gravam no proprio arquivo.
Um adapter que aceita a escrita e nao faz nada esconde exatamente o defeito que
mais importa descobrir cedo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ...ports import AdapterError


def _field(data: dict[str, Any], name: str, legacy: str) -> Any:
    """Le a chave en-us, aceitando a antiga em pt-br.

    Arquivo de task ja escrito por alguem nao pode deixar de ser lido porque o
    projeto padronizou o vocabulario. O nome novo vence quando os dois existem.
    """
    value = data.get(name)
    return value if value is not None else data.get(legacy)
from ...ports.tasks import (BLOCKS, RELATED, Comment, ExternalTask,
                            ExternalStatus, TaskProvider, TaskRef)


class FilesystemTasks(TaskProvider):
    name = "filesystem"

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    def verify(self) -> None:
        if not self.dir.is_dir():
            raise AdapterError(f"diretorio de tasks nao existe: {self.dir}")

    # ---- leitura ---------------------------------------------------------

    def _files(self) -> list[Path]:
        return sorted([*self.dir.glob("*.yaml"), *self.dir.glob("*.yml")])

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            # Arquivo quebrado sobe como error. Pular em silencio faria a task
            # sumir do board sem ninguem notar.
            raise AdapterError(f"{path.name} nao pode ser lido: {e}") from e
        if not isinstance(data, dict):
            raise AdapterError(f"{path.name} nao contem um mapeamento")
        return data

    #: Vocabulario deste formato -> vocabulario do motor. Quem escreve o YAML
    #: escolhe o texto; o mapa e o contrato. Status fora do mapa vira
    #: DESCONHECIDA e sobe como anomalia -- nunca e coagido para o vizinho.
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
        # `depends_on` e, por definicao, bloqueio -- e o unico tipo de vinculo
        # que este formato exprime. `relacionado` fica em `relacionadas`, que
        # nao cria ordem de execucao.
        links = tuple(
            TaskRef(key=str(v["key"]) if isinstance(v, dict) else str(v),
                    kind=str(v.get("tipo", BLOCKS)) if isinstance(v, dict) else BLOCKS)
            for v in (_field(data, "depends_on", "depende_de") or [])
        ) + tuple(
            TaskRef(key=str(v), kind=RELATED)
            for v in (_field(data, "related", "relacionadas") or [])
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
            data={"arquivo": str(path)})

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
        raise AdapterError(f"task {key} nao encontrada em {self.dir}")

    def get_task(self, key: str) -> ExternalTask:
        path = self._path_for(key)
        return self._build(path, self._read(path))

    def get_comments(self, key: str) -> list[Comment]:
        data = self._read(self._path_for(key))
        return [Comment(author=str(_field(c, "author", "autor") or "?"),
                        text=str(_field(c, "text", "texto") or ""),
                        criado_em=str(c.get("em", "")), id=str(c.get("id", "")))
                for c in (_field(data, "comments", "comentarios") or [])]

    # ---- escrita ---------------------------------------------------------

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        # Grava em temporario e troca: interrupcao no meio nao pode deixar o
        # arquivo pela metade, porque ele e o estado do provider.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
        tmp.replace(path)

    def update_task(self, key: str, campos: dict[str, Any]) -> None:
        path = self._path_for(key)
        data = self._read(path)
        data.update(campos)
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

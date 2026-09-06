# -*- coding: utf-8 -*-
"""TaskProvider que le trabalho de arquivos YAML no disco.

Nao e simulacao de outro provedor: e um provedor de verdade, util para quem
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

from ...ports import AdapterErro
from ...ports.tasks import Comment, ExternalTask, TaskProvider, TaskRef


class FilesystemTasks(TaskProvider):
    nome = "filesystem"

    def __init__(self, diretorio: str | Path):
        self.dir = Path(diretorio)

    def verifica(self) -> None:
        if not self.dir.is_dir():
            raise AdapterErro(f"diretorio de tasks nao existe: {self.dir}")

    # ---- leitura ---------------------------------------------------------

    def _arquivos(self) -> list[Path]:
        return sorted([*self.dir.glob("*.yaml"), *self.dir.glob("*.yml")])

    def _le(self, caminho: Path) -> dict[str, Any]:
        try:
            dados = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
        except Exception as e:
            # Arquivo quebrado sobe como erro. Pular em silencio faria a task
            # sumir do board sem ninguem notar.
            raise AdapterErro(f"{caminho.name} nao pode ser lido: {e}") from e
        if not isinstance(dados, dict):
            raise AdapterErro(f"{caminho.name} nao contem um mapeamento")
        return dados

    def _monta(self, caminho: Path, dados: dict[str, Any]) -> ExternalTask:
        vinculos = tuple(
            TaskRef(key=str(v["key"]), tipo=str(v.get("tipo", "blocks")))
            if isinstance(v, dict) else TaskRef(key=str(v))
            for v in (dados.get("depende_de") or [])
        )
        return ExternalTask(
            key=str(dados.get("key") or caminho.stem),
            titulo=str(dados.get("titulo") or dados.get("title") or caminho.stem),
            estado_externo=str(dados.get("estado", "TO DO")),
            descricao=str(dados.get("descricao") or dados.get("description") or ""),
            url=dados.get("url"),
            prioridade=int(dados.get("prioridade", 100)),
            projeto=str(dados.get("projeto", "")),
            responsavel=dados.get("responsavel"),
            vinculos=vinculos,
            recursos=tuple(str(r) for r in (dados.get("recursos") or [])),
            dados={"arquivo": str(caminho)})

    def list_tasks(self, filtro: dict[str, Any] | None = None) -> list[ExternalTask]:
        self.verifica()
        prontas = []
        excluir = set((filtro or {}).get("excluir_estados", ["DONE", "CANCELLED"]))
        for caminho in self._arquivos():
            t = self._monta(caminho, self._le(caminho))
            if t.estado_externo.upper() not in excluir:
                prontas.append(t)
        return prontas

    def _caminho_de(self, key: str) -> Path:
        for caminho in self._arquivos():
            if str(self._le(caminho).get("key") or caminho.stem) == key:
                return caminho
        raise AdapterErro(f"task {key} nao encontrada em {self.dir}")

    def get_task(self, key: str) -> ExternalTask:
        caminho = self._caminho_de(key)
        return self._monta(caminho, self._le(caminho))

    def get_comments(self, key: str) -> list[Comment]:
        dados = self._le(self._caminho_de(key))
        return [Comment(autor=str(c.get("autor", "?")), texto=str(c.get("texto", "")),
                        criado_em=str(c.get("em", "")), id=str(c.get("id", "")))
                for c in (dados.get("comentarios") or [])]

    # ---- escrita ---------------------------------------------------------

    def _grava(self, caminho: Path, dados: dict[str, Any]) -> None:
        # Grava em temporario e troca: interrupcao no meio nao pode deixar o
        # arquivo pela metade, porque ele e o estado do provedor.
        tmp = caminho.with_suffix(caminho.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(dados, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
        tmp.replace(caminho)

    def update_task(self, key: str, campos: dict[str, Any]) -> None:
        caminho = self._caminho_de(key)
        dados = self._le(caminho)
        dados.update(campos)
        self._grava(caminho, dados)

    def transition_task(self, key: str, destino: str) -> None:
        self.update_task(key, {"estado": destino})

    def add_comment(self, key: str, texto: str) -> None:
        caminho = self._caminho_de(key)
        dados = self._le(caminho)
        dados.setdefault("comentarios", []).append({"autor": "regente", "texto": texto})
        self._grava(caminho, dados)

    def add_label(self, key: str, label: str) -> None:
        caminho = self._caminho_de(key)
        dados = self._le(caminho)
        labels = dados.setdefault("labels", [])
        if label not in labels:
            labels.append(label)
        self._grava(caminho, dados)

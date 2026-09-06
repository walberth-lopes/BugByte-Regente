# -*- coding: utf-8 -*-
"""TaskProvider: de onde vem o trabalho."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from . import Capability, Port


@dataclass(frozen=True, slots=True)
class TaskRef:
    """Um vinculo declarado pelo fornecedor entre duas unidades de trabalho.

    `tipo` e vocabulario do motor -- 'blocks', 'subtask', 'relates' -- e nao o
    nome do link no fornecedor. Traduzir e trabalho do adapter.
    """
    key: str
    tipo: str = "blocks"


@dataclass(frozen=True, slots=True)
class ExternalTask:
    """Uma task como o fornecedor a descreve. Dado NAO confiavel.

    Titulo, descricao e comentarios sao texto escrito por terceiros. Nenhum
    prompt do motor pode trata-los como instrucao: tentativa de manipulacao vira
    achado, nunca ordem.
    """
    key: str
    titulo: str
    estado_externo: str = ""
    descricao: str = ""
    url: str | None = None
    prioridade: int = 100
    projeto: str = ""
    responsavel: str | None = None
    #: Dependencias declaradas no fornecedor.
    vinculos: tuple[TaskRef, ...] = ()
    #: Chaves de recurso, quando o fornecedor sabe informa-las. Vazio e comum:
    #: quem enriquece isso e o agente de analise, nao o adapter.
    recursos: tuple[str, ...] = ()
    dados: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Comment:
    autor: str
    texto: str
    criado_em: str = ""
    id: str = ""


class TaskProvider(Port):
    capability = Capability.TASKS

    @abstractmethod
    def list_tasks(self, filtro: dict[str, Any] | None = None) -> list[ExternalTask]:
        """Trabalho visivel agora. Erro sobe como AdapterErro, nunca lista vazia."""

    @abstractmethod
    def get_task(self, key: str) -> ExternalTask: ...

    def get_comments(self, key: str) -> list[Comment]:
        return []

    # ---- escrita ---------------------------------------------------------
    # Separadas de proposito: um adapter de leitura pode existir sem implementar
    # nenhuma delas, e o Policy Engine continua sendo quem autoriza a chamada.

    def update_task(self, key: str, campos: dict[str, Any]) -> None:
        raise NotImplementedError

    def transition_task(self, key: str, destino: str) -> None:
        raise NotImplementedError

    def add_comment(self, key: str, texto: str) -> None:
        raise NotImplementedError

    def add_label(self, key: str, label: str) -> None:
        raise NotImplementedError

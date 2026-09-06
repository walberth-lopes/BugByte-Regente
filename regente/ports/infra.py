# -*- coding: utf-8 -*-
"""CloudProvider and DatabaseProvider.

The two most dangerous ports in the engine, and for that reason the two most
asymmetric ones: reading is plentiful, writing is minimal. `DatabaseProvider`
exposes `query_readonly()` and nothing else for querying -- there is no
`execute()` on the port. An adapter that needs to write to the database declares
that as an action of its own, passes through the Policy Engine and carries
autonomy level L4. Leaving a generic `execute()` here would be asking the policy
to guess what a SQL string does.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from . import Capability, Port


@dataclass(frozen=True, slots=True)
class Resource:
    id: str
    kind: str
    name: str
    environment: str = "staging"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LogEntry:
    ts: str
    severidade: str
    text: str
    source: str = ""


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    value: float
    unidade: str = ""
    ts: str = ""


class CloudProvider(Port):
    capability = Capability.CLOUD

    @abstractmethod
    def list_resources(self, kind: str | None = None,
                       environment: str | None = None) -> list[Resource]: ...

    @abstractmethod
    def inspect_resource(self, resource_id: str) -> Resource: ...

    def get_logs(self, resource_id: str, desde: str | None = None,
                 limit: int = 200) -> list[LogEntry]:
        return []

    def get_metrics(self, resource_id: str, nomes: list[str] | None = None) -> list[Metric]:
        return []

    # ---- writing ---------------------------------------------------------

    def deploy(self, resource_id: str, version: str, environment: str) -> str:
        raise NotImplementedError

    def rollback(self, resource_id: str, to_version: str | None = None) -> str:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    kind: str
    nulo: bool = True


@dataclass(frozen=True, slots=True)
class Table:
    name: str
    schema: str = ""
    colunas: tuple[Column, ...] = ()
    linhas_aprox: int | None = None


@dataclass(frozen=True, slots=True)
class QueryResult:
    colunas: tuple[str, ...]
    lines: tuple[tuple[Any, ...], ...]
    truncado: bool = False


class DatabaseProvider(Port):
    capability = Capability.DATABASE

    @abstractmethod
    def list_databases(self) -> list[str]: ...

    @abstractmethod
    def inspect_schema(self, database: str, schema: str | None = None) -> list[Table]: ...

    @abstractmethod
    def query_readonly(self, database: str, sql: str, limit: int = 100) -> QueryResult:
        """A query with no side effect.

        The adapter is responsible for genuinely guaranteeing that -- read-only
        session, a user with no write GRANT, a timeout. A 'SELECT' prefix is no
        guarantee: a CTE with `INSERT ... RETURNING` also starts with WITH.
        """

# -*- coding: utf-8 -*-
"""CloudProvider e DatabaseProvider.

As duas portas mais perigosas do motor, e por isso as duas mais assimetricas:
leitura e farta, escrita e minima. `DatabaseProvider` expoe `query_readonly()` e
nada mais de consulta -- nao existe `execute()` na port. Um adapter que precise
escrever no banco declara isso como acao propria, passa pelo Policy Engine e
carrega o nivel de autonomia L4. Deixar um `execute()` generico aqui seria pedir
que a policy adivinhasse o que uma string SQL faz.
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

    # ---- escrita ---------------------------------------------------------

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
        """Consulta sem efeito colateral.

        O adapter e responsavel por garantir isso de verdade -- sessao somente
        leitura, usuario sem GRANT de escrita, timeout. Prefixo 'SELECT' nao e
        garantia: CTE com `INSERT ... RETURNING` tambem comeca com WITH.
        """

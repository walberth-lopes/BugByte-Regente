# -*- coding: utf-8 -*-
"""O dominio. Nenhum tipo aqui sabe o que e Jira, GitHub, GCloud ou SQLite.

Duas escolhas estruturais que sustentam o resto do motor:

**A espinha de multi-tenancy esta em todo objeto que persiste.** Organization ->
Client -> Workspace -> Project -> Repository nao e hierarquia decorativa: cada
Task, Run e Event carrega `workspace_id`. Enfiar tenancy depois exige migrar
todas as tabelas e revisar toda consulta -- e a consulta esquecida e justamente a
que vaza dado do cliente A para o cliente B.

**`ExternalRef` separa o id do motor do id do fornecedor.** A task existe no
motor mesmo que o fornecedor mude de ferramenta; e a mesma task pode ser vista
por dois provedores diferentes (a issue no Jira, o PR no GitHub) sem duplicar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone
from typing import Any

from .policy import AutonomyLevel
from .risk import RiskLevel
from .states import TaskState


def agora() -> datetime:
    """UTC, sempre. Horario local so aparece na superficie de apresentacao."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Espinha de tenancy
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Organization:
    id: str
    nome: str


@dataclass(frozen=True, slots=True)
class Client:
    id: str
    organization_id: str
    nome: str


@dataclass(frozen=True, slots=True)
class Workspace:
    """A unidade de configuracao: um conjunto de adapters, policies e limites.

    E o workspace -- nao o projeto -- que carrega credencial e autonomia, porque
    e nele que a fronteira entre clientes precisa ser inviolavel.
    """
    id: str
    client_id: str
    nome: str
    autonomia_maxima: AutonomyLevel = AutonomyLevel.L2
    raiz: str | None = None


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    workspace_id: str
    nome: str
    ambiente_padrao: str = "staging"
    autonomia_maxima: AutonomyLevel | None = None  # None = herda do workspace


@dataclass(frozen=True, slots=True)
class Repository:
    id: str
    project_id: str
    nome: str
    branch_base: str = "main"
    url: str | None = None


# --------------------------------------------------------------------------
# Trabalho
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ExternalRef:
    """De onde a task veio, no vocabulario de quem a emitiu."""
    provider: str          # nome do adapter, ex.: "filesystem", "jira"
    key: str               # chave no fornecedor, ex.: "FAXINA-183"
    url: str | None = None


@dataclass(slots=True)
class Task:
    id: str
    workspace_id: str
    project_id: str
    titulo: str
    estado: TaskState = TaskState.DISCOVERED
    externo: ExternalRef | None = None
    descricao: str = ""
    prioridade: int = 100                       # menor roda antes
    risco: RiskLevel | None = None
    #: Estado em que a task estava quando pausou para decisao humana.
    pausada_em: TaskState | None = None
    #: Chaves de recurso que esta task toca em exclusividade. O scheduler usa
    #: isto para NAO paralelizar dois workers sobre a mesma migration ou o mesmo
    #: arquivo. Ex.: "repo:acme/api", "migration:acme/api", "file:src/auth.py".
    recursos: tuple[str, ...] = ()
    tentativas: int = 0
    criada_em: datetime = field(default_factory=agora)
    atualizada_em: datetime = field(default_factory=agora)
    dados: dict[str, Any] = field(default_factory=dict)

    @property
    def chave(self) -> str:
        """Como a task aparece para um humano."""
        return self.externo.key if self.externo else self.id


@dataclass(frozen=True, slots=True)
class Dependency:
    task_id: str
    depende_de: str
    tipo: str = "blocks"     # blocks | subtask | conflito
    motivo: str = ""


class RunState(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"   # worker morreu; lease venceu
    ABORTED = "ABORTED"           # o motor parou de proposito (loop, orcamento)


@dataclass(slots=True)
class Run:
    """Uma tentativa de execucao de uma task por um agente.

    Task tem estado de longo prazo; Run tem estado de tentativa. Separar os dois
    e o que permite retentar sem perder o historico -- e o que faz "o worker
    morreu" ser diferente de "a task falhou".
    """
    id: str
    task_id: str
    workspace_id: str
    agente: str
    estado: RunState = RunState.RUNNING
    worker: str | None = None
    workspace_path: str | None = None
    branch: str | None = None
    iniciado_em: datetime = field(default_factory=agora)
    encerrado_em: datetime | None = None
    motivo: str = ""
    custo_usd: float = 0.0
    tokens: int = 0
    chamadas_tool: int = 0
    iteracoes: int = 0
    dados: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Event:
    """Registro append-only. Fonte de verdade da timeline e da auditoria.

    O motor nunca apaga nem edita evento. Estado e uma projecao conveniente;
    o evento e o que aconteceu.
    """
    id: str
    workspace_id: str
    tipo: str
    ts: datetime = field(default_factory=agora)
    task_id: str | None = None
    run_id: str | None = None
    ator: str = "engine"
    resumo: str = ""
    dados: dict[str, Any] = field(default_factory=dict)


class ApprovalState(str, Enum):
    OPEN = "OPEN"
    DECIDED = "DECIDED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class Option:
    id: str
    rotulo: str
    efeito: str = ""


@dataclass(slots=True)
class Approval:
    """Um item da fila NEEDS ME.

    Os campos sao os do briefing exigido: o que aconteceu, por que importa, o que
    o agente tentou, opcoes, recomendacao, risco. Log gigante nao entra aqui --
    fica nos eventos, sob demanda.
    """
    id: str
    workspace_id: str
    task_id: str
    o_que_aconteceu: str
    por_que_importa: str
    o_que_o_agente_tentou: tuple[str, ...] = ()
    opcoes: tuple[Option, ...] = ()
    recomendacao: str | None = None      # Option.id
    risco: RiskLevel = RiskLevel.MEDIUM
    estado: ApprovalState = ApprovalState.OPEN
    run_id: str | None = None
    criada_em: datetime = field(default_factory=agora)
    decidida_em: datetime | None = None
    decidida_por: str | None = None
    escolha: str | None = None
    nota: str = ""


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """Toda requisicao de tool, com o veredito da policy. Nada fica de fora.

    Registra-se tambem o que foi NEGADO: uma negativa e o registro mais valioso
    que o motor produz, porque e ela que prova que o portao esta vivo.
    """
    id: str
    workspace_id: str
    agente: str
    acao: str
    recurso: str
    efeito: str                # ALLOW | DENY | HUMAN_APPROVAL
    risco: str
    ts: datetime = field(default_factory=agora)
    task_id: str | None = None
    run_id: str | None = None
    regra: str | None = None
    motivo: str = ""
    resultado: str = ""
    duracao_ms: int = 0
    custo_usd: float = 0.0
    tokens: int = 0


@dataclass(frozen=True, slots=True)
class Lease:
    """Trava cooperativa com batimento.

    Nao existe trava sem expiracao neste motor: o worker que morre sem soltar a
    trava e o caso normal, nao o excepcional. Lease vencido e o sinal que a
    recuperacao usa para devolver a task a fila.
    """
    recurso: str
    dono: str
    expira_em: datetime
    workspace_id: str
    renovado_em: datetime = field(default_factory=agora)

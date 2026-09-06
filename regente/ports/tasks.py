# -*- coding: utf-8 -*-
"""TaskProvider: de onde vem o trabalho."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import Capability, Port


class ExternalStatus(str, Enum):
    """Onde o trabalho esta, no entender de quem o emitiu.

    Existe porque `estado_externo` cru e texto livre, e o motor precisa de UMA
    decisao a partir dele: este trabalho esta disponivel, ja tem alguem nele, ou
    acabou? Sem isso o Core teria de conhecer os nomes de status de cada
    fornecedor -- que e exatamente o acoplamento que a port impede.

    O vocabulario e o menor que responde a essa pergunta. Nao e traducao do
    fluxo de nenhuma ferramenta: e a posicao no ciclo de vida, que todo sistema
    de trabalho tem.
    """
    NOT_STARTED = "NAO_INICIADA"
    IN_ANALYSIS = "EM_ANALISE"
    IN_PROGRESS = "EM_EXECUCAO"
    IN_REVIEW = "EM_REVISAO"
    IN_VALIDATION = "EM_VALIDACAO"
    COMPLETED = "CONCLUIDA"
    CANCELLED = "CANCELADA"
    #: Status que o adapter nao soube mapear. **Nunca** e coagido para o vizinho
    #: mais conveniente: um status novo no board significa que alguem mudou o
    #: processo, e o motor precisa dizer isso em vez de adivinhar.
    UNKNOWN = "DESCONHECIDA"

    @property
    def available(self) -> bool:
        """Trabalho que o motor poderia pegar."""
        return self in (ExternalStatus.NOT_STARTED, ExternalStatus.IN_ANALYSIS)

    @property
    def in_progress(self) -> bool:
        """Alguem (humano ou nao) ja esta nisto."""
        return self in (ExternalStatus.IN_PROGRESS, ExternalStatus.IN_REVIEW,
                        ExternalStatus.IN_VALIDATION)

    @property
    def finished(self) -> bool:
        return self in (ExternalStatus.COMPLETED, ExternalStatus.CANCELLED)


#: Tipos de vinculo que o motor entende. Traduzir o nome do fornecedor para um
#: destes e trabalho do adapter.
#:
#: `bloqueia` e o UNICO que vira aresta de dependencia. `pai` e hierarquia --
#: uma subtarefa nao espera a mae terminar, ela e parte do que a mae e; e
#: `relacionado` e contexto, nao ordem. Tratar os tres como iguais trava um
#: board inteiro, porque hierarquia e relacionamento sao muito mais comuns que
#: bloqueio real.
BLOCKS = "blocks"
PARENT = "parent"
CHILD = "child"
RELATED = "related"
DUPLICATES = "duplicates"

#: Os que criam ordem de execucao. Qualquer outro e informacao, nao restricao.
BLOCKING_TYPES: frozenset[str] = frozenset({BLOCKS})


@dataclass(frozen=True, slots=True)
class TaskRef:
    """Um vinculo declarado pelo fornecedor entre duas unidades de trabalho.

    `tipo` e vocabulario do motor -- nunca o nome do link no fornecedor.
    """
    key: str
    kind: str = RELATED

    @property
    def blocking(self) -> bool:
        return self.kind in BLOCKING_TYPES


@dataclass(frozen=True, slots=True)
class ExternalTask:
    """Uma task como o fornecedor a descreve. Dado NAO confiavel.

    Titulo, descricao e comentarios sao texto escrito por terceiros. Nenhum
    prompt do motor pode trata-los como instrucao: tentativa de manipulacao vira
    achado, nunca ordem.
    """
    key: str
    title: str
    status: ExternalStatus = ExternalStatus.UNKNOWN
    #: O status cru, como o fornecedor o escreveu. Preservado para diagnostico:
    #: quando `situacao` vem DESCONHECIDA, e este campo que diz o que apareceu.
    external_status: str = ""
    description: str = ""
    url: str | None = None
    priority: int = 100
    project: str = ""
    assignee: str | None = None
    #: Dependencias e hierarquia declaradas no fornecedor.
    links: tuple[TaskRef, ...] = ()
    #: Chaves de recurso que a task toca em exclusividade. Vazio e comum e
    #: honesto: um provedor de tasks raramente sabe quais arquivos serao
    #: tocados. Quem enriquece isso e a analise, nao o adapter.
    resources: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    #: True quando o registro veio de uma LISTAGEM, com campos enxutos.
    #:
    #: Listar e buscar detalhe sao operacoes de custo muito diferente: um board
    #: real devolve centenas de KB se cada item trouxer descricao completa. Sem
    #: este campo, "descricao vazia" e "descricao nao pedida" ficam
    #: indistinguiveis -- e o motor acusaria o board inteiro de estar mal
    #: escrito quando o incompleto era o proprio pedido.
    partial: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def anomalies(self) -> tuple[str, ...]:
        """O que veio torto do fornecedor. Reportado, nunca corrigido em silencio."""
        findings = []
        if self.status is ExternalStatus.UNKNOWN:
            findings.append(f"status nao mapeado: {self.external_status!r}")
        if not self.title.strip():
            findings.append("sem titulo")
        if not self.partial and not self.description.strip():
            findings.append("sem descricao")
        return tuple(findings)


@dataclass(frozen=True, slots=True)
class Comment:
    author: str
    text: str
    criado_em: str = ""
    id: str = ""


class TaskProvider(Port):
    capability = Capability.TASKS

    @abstractmethod
    def list_tasks(self, filtro: dict[str, Any] | None = None) -> list[ExternalTask]:
        """Trabalho visivel now. Erro sobe como AdapterErro, nunca lista vazia."""

    @abstractmethod
    def get_task(self, key: str) -> ExternalTask: ...

    def get_comments(self, key: str) -> list[Comment]:
        return []

    # ---- escrita ---------------------------------------------------------
    # Separadas de proposito: um adapter de leitura pode existir sem implementar
    # nenhuma delas, e o Policy Engine continua sendo quem autoriza a chamada.

    def update_task(self, key: str, campos: dict[str, Any]) -> None:
        raise NotImplementedError

    def transition_task(self, key: str, destination: str) -> None:
        raise NotImplementedError

    def add_comment(self, key: str, text: str) -> None:
        raise NotImplementedError

    def add_label(self, key: str, label: str) -> None:
        raise NotImplementedError

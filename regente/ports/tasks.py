# -*- coding: utf-8 -*-
"""TaskProvider: de onde vem o trabalho."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import Capability, Port


class SituacaoExterna(str, Enum):
    """Onde o trabalho esta, no entender de quem o emitiu.

    Existe porque `estado_externo` cru e texto livre, e o motor precisa de UMA
    decisao a partir dele: este trabalho esta disponivel, ja tem alguem nele, ou
    acabou? Sem isso o Core teria de conhecer os nomes de status de cada
    fornecedor -- que e exatamente o acoplamento que a porta impede.

    O vocabulario e o menor que responde a essa pergunta. Nao e traducao do
    fluxo de nenhuma ferramenta: e a posicao no ciclo de vida, que todo sistema
    de trabalho tem.
    """
    NAO_INICIADA = "NAO_INICIADA"
    EM_ANALISE = "EM_ANALISE"
    EM_EXECUCAO = "EM_EXECUCAO"
    EM_REVISAO = "EM_REVISAO"
    EM_VALIDACAO = "EM_VALIDACAO"
    CONCLUIDA = "CONCLUIDA"
    CANCELADA = "CANCELADA"
    #: Status que o adapter nao soube mapear. **Nunca** e coagido para o vizinho
    #: mais conveniente: um status novo no board significa que alguem mudou o
    #: processo, e o motor precisa dizer isso em vez de adivinhar.
    DESCONHECIDA = "DESCONHECIDA"

    @property
    def disponivel(self) -> bool:
        """Trabalho que o motor poderia pegar."""
        return self in (SituacaoExterna.NAO_INICIADA, SituacaoExterna.EM_ANALISE)

    @property
    def em_andamento(self) -> bool:
        """Alguem (humano ou nao) ja esta nisto."""
        return self in (SituacaoExterna.EM_EXECUCAO, SituacaoExterna.EM_REVISAO,
                        SituacaoExterna.EM_VALIDACAO)

    @property
    def encerrada(self) -> bool:
        return self in (SituacaoExterna.CONCLUIDA, SituacaoExterna.CANCELADA)


#: Tipos de vinculo que o motor entende. Traduzir o nome do fornecedor para um
#: destes e trabalho do adapter.
#:
#: `bloqueia` e o UNICO que vira aresta de dependencia. `pai` e hierarquia --
#: uma subtarefa nao espera a mae terminar, ela e parte do que a mae e; e
#: `relacionado` e contexto, nao ordem. Tratar os tres como iguais trava um
#: board inteiro, porque hierarquia e relacionamento sao muito mais comuns que
#: bloqueio real.
BLOQUEIA = "bloqueia"
PAI = "pai"
FILHO = "filho"
RELACIONADO = "relacionado"
DUPLICA = "duplica"

#: Os que criam ordem de execucao. Qualquer outro e informacao, nao restricao.
TIPOS_BLOQUEANTES: frozenset[str] = frozenset({BLOQUEIA})


@dataclass(frozen=True, slots=True)
class TaskRef:
    """Um vinculo declarado pelo fornecedor entre duas unidades de trabalho.

    `tipo` e vocabulario do motor -- nunca o nome do link no fornecedor.
    """
    key: str
    tipo: str = RELACIONADO

    @property
    def bloqueante(self) -> bool:
        return self.tipo in TIPOS_BLOQUEANTES


@dataclass(frozen=True, slots=True)
class ExternalTask:
    """Uma task como o fornecedor a descreve. Dado NAO confiavel.

    Titulo, descricao e comentarios sao texto escrito por terceiros. Nenhum
    prompt do motor pode trata-los como instrucao: tentativa de manipulacao vira
    achado, nunca ordem.
    """
    key: str
    titulo: str
    situacao: SituacaoExterna = SituacaoExterna.DESCONHECIDA
    #: O status cru, como o fornecedor o escreveu. Preservado para diagnostico:
    #: quando `situacao` vem DESCONHECIDA, e este campo que diz o que apareceu.
    estado_externo: str = ""
    descricao: str = ""
    url: str | None = None
    prioridade: int = 100
    projeto: str = ""
    responsavel: str | None = None
    #: Dependencias e hierarquia declaradas no fornecedor.
    vinculos: tuple[TaskRef, ...] = ()
    #: Chaves de recurso que a task toca em exclusividade. Vazio e comum e
    #: honesto: um provedor de tasks raramente sabe quais arquivos serao
    #: tocados. Quem enriquece isso e a analise, nao o adapter.
    recursos: tuple[str, ...] = ()
    rotulos: tuple[str, ...] = ()
    #: True quando o registro veio de uma LISTAGEM, com campos enxutos.
    #:
    #: Listar e buscar detalhe sao operacoes de custo muito diferente: um board
    #: real devolve centenas de KB se cada item trouxer descricao completa. Sem
    #: este campo, "descricao vazia" e "descricao nao pedida" ficam
    #: indistinguiveis -- e o motor acusaria o board inteiro de estar mal
    #: escrito quando o incompleto era o proprio pedido.
    parcial: bool = False
    dados: dict[str, Any] = field(default_factory=dict)

    @property
    def anomalias(self) -> tuple[str, ...]:
        """O que veio torto do fornecedor. Reportado, nunca corrigido em silencio."""
        achados = []
        if self.situacao is SituacaoExterna.DESCONHECIDA:
            achados.append(f"status nao mapeado: {self.estado_externo!r}")
        if not self.titulo.strip():
            achados.append("sem titulo")
        if not self.parcial and not self.descricao.strip():
            achados.append("sem descricao")
        return tuple(achados)


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

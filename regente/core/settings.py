# -*- coding: utf-8 -*-
"""Configuracao que a TELA edita, sobreposta ao arquivo. Puro.

`regente.yaml` continua sendo a base: versionavel, com comentarios, com dono
humano. Uma pagina web nao reescreve um arquivo desses -- comentarios se perdem,
edicoes simultaneas se atropelam, e um erro de escrita deixa o motor sem subir.

Entao a tela grava uma **sobreposicao**, e a configuracao efetiva e derivada das
duas:

    arquivo  ->  base
    banco    ->  sobreposicao (tela E terminal, pelo mesmo servico)
    efetiva  ->  base + sobreposicao

**A procedencia e obrigatoria, e nao um enfeite.** Sem ela, alguem edita o YAML,
nada muda, e a conclusao razoavel e "o Regente esta quebrado". Por isso `Effective`
carrega, campo a campo, de onde cada valor veio -- e a tela e o terminal mostram
isso, com o caminho para remover a sobreposicao.

E a mesma forma do marco anterior: la a intencao mora no banco e a fase e
derivada; aqui a sobreposicao mora no banco e a configuracao e derivada. Nenhuma
segunda arquitetura.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Source(str, Enum):
    """De onde um valor veio. Aparece na tela e no terminal, sempre."""
    FILE = "arquivo"
    OVERLAY = "tela"
    #: Nem o arquivo nem a sobreposicao disseram nada.
    ABSENT = "ausente"


#: O que a tela pode sobrepor. Lista FECHADA.
#:
#: Fechada porque uma sobreposicao que aceitasse qualquer chave viraria um
#: segundo formato de configuracao, sem validacao e sem revisao -- e o primeiro
#: uso seria alguem sobrepondo `policies` pela tela, que e exatamente a
#: autoridade que a tela nao tem.
OVERRIDABLE: tuple[str, ...] = ("providers", "status_map", "selection")


@dataclass(frozen=True, slots=True)
class Field:
    """Um valor efetivo, e de onde ele veio."""
    value: Any
    source: Source = Source.ABSENT
    #: Preenchido quando ha sobreposicao E o arquivo tambem diz algo. E o caso
    #: perigoso: a pessoa le o arquivo e ve outra coisa.
    shadowed: Any = None

    @property
    def overridden(self) -> bool:
        return self.source is Source.OVERLAY

    @property
    def conflicts(self) -> bool:
        """Ha sobreposicao E o arquivo diz coisa diferente."""
        return self.overridden and self.shadowed is not None


@dataclass(frozen=True, slots=True)
class Overlay:
    """O que a tela gravou para um workspace. Vazio e o estado normal."""
    workspace_id: str = ""
    values: dict[str, Any] = field(default_factory=dict)
    changed_by: str = ""
    changed_at: Any = None

    def get(self, key: str) -> Any:
        return self.values.get(key)

    def has(self, key: str) -> bool:
        return key in self.values


def effective(key: str, from_file: Any, overlay: Overlay | None) -> Field:
    """O valor que vale, e a procedencia. Uma funcao, usada por todos.

    Duas leituras da mesma pergunta divergem, e a que diverge e sempre a que
    esquece de olhar a sobreposicao.
    """
    if key not in OVERRIDABLE:
        raise ValueError(
            f"'{key}' nao e sobreponivel. Sobreponiveis: "
            f"{', '.join(OVERRIDABLE)}")

    if overlay is not None and overlay.has(key):
        return Field(value=overlay.get(key), source=Source.OVERLAY,
                     shadowed=from_file if from_file else None)
    if from_file:
        return Field(value=from_file, source=Source.FILE)
    return Field(value=None, source=Source.ABSENT)


def describe(campo: Field, key: str) -> str:
    """Uma frase que diz de onde veio e o que fazer. Para tela e terminal.

    A frase mais importante e a do conflito: e o unico caso em que uma pessoa
    olha o arquivo, ve uma coisa, e o motor faz outra.
    """
    if campo.source is Source.ABSENT:
        return f"'{key}' nao esta configurado nem no arquivo nem na tela"
    if campo.source is Source.FILE:
        return f"'{key}' vem do regente.yaml"
    if campo.conflicts:
        return (f"'{key}' foi definido PELA TELA e substitui o que esta no "
                f"regente.yaml. Editar o arquivo nao muda nada enquanto esta "
                f"sobreposicao existir -- remova-a para o arquivo voltar a valer")
    return f"'{key}' foi definido pela tela; o regente.yaml nao diz nada sobre ele"

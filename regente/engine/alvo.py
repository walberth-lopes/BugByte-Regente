# -*- coding: utf-8 -*-
"""Onde esta task pode ser executada? -- resolucao de repositorio alvo.

**O achado que define este modulo:** medido contra 100 tasks e 12 repositorios
reais em 06/09/2026, o campo natural para responder isso (`componentes`) esta
vazio em 100 de 100. O sinal mais forte disponivel -- ja existir uma branch
citando a chave da task -- cobre 14 de 100. Os demais sao ambiguos por natureza:
o rotulo `scamchecker` aparece em 72 tasks e e ao mesmo tempo o nome de UM
repositorio e o nome do produto inteiro, que tem doze.

Ou seja: **a informacao esta faltando, nao escondida.** Nenhuma esperteza de
casamento de texto resolve isso -- ela so troca "nao sei" por "errei com
confianca", que e infinitamente pior num motor que vai escrever codigo.

Entao este modulo coleta evidencia e declara o que sabe. Ele nunca escolhe no
empate e nunca inventa no vazio. Ambiguidade e ausencia sao desfechos legitimos,
e viram pergunta ao humano -- que e exatamente o tipo de coisa que a fila NEEDS
ME existe para receber.

CONTRATO FUTURO ENTRE TaskProvider E RepositoryProvider
-------------------------------------------------------
O que falta nao e codigo, e **dado declarado**. Em ordem de preferencia:

1. O provedor de tasks passa a emitir o alvo (campo proprio, componente,
   convencao de rotulo). E a unica fonte que nao envelhece, porque quem escreve
   a task sabe onde ela roda.
2. Enquanto isso nao existe, um mapa na configuracao do workspace
   (`rotulo -> repo`, `projeto -> repo`) cobre o caso comum com zero adivinhacao.
3. Um agente de analise le o codigo e propoe o alvo com evidencia -- caro, e por
   isso ultimo, mas e o unico que resolve task nova em repositorio novo.

Nenhum dos tres exige mudar o Core: `ExternalTask.recursos` e `dados` ja
carregam o resultado, venha ele de onde vier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from ..ports.repository import Branch, RepoInfo
from ..ports.tasks import ExternalTask


class Confianca(str, Enum):
    """Quanto o motor sabe sobre onde a task roda.

    Nao existe grau intermediario inventado. Ou a evidencia e declarada por
    alguem, ou e observada no mundo, ou nao existe.
    """
    #: Alguem declarou explicitamente. Nao ha o que interpretar.
    DECLARADA = "DECLARADA"
    #: O mundo mostra trabalho ja comecado num repositorio (branch com a chave).
    OBSERVADA = "OBSERVADA"
    #: Mais de um candidato com a mesma forca. O motor NAO desempata.
    AMBIGUA = "AMBIGUA"
    #: Nenhuma evidencia. O motor nao chuta.
    AUSENTE = "AUSENTE"

    @property
    def acionavel(self) -> bool:
        return self in (Confianca.DECLARADA, Confianca.OBSERVADA)


@dataclass(frozen=True, slots=True)
class Evidencia:
    """Por que este repositorio e candidato. Sem isto, nada e auditavel."""
    fonte: str
    detalhe: str


@dataclass(frozen=True, slots=True)
class Candidato:
    repo: RepoInfo
    evidencias: tuple[Evidencia, ...]
    #: Peso da evidencia mais forte que sustenta este candidato.
    forca: int = 0


@dataclass(frozen=True, slots=True)
class Alvo:
    """O resultado da resolucao. Pode legitimamente nao ter repositorio."""
    task_key: str
    confianca: Confianca
    candidatos: tuple[Candidato, ...] = ()
    motivo: str = ""

    @property
    def repo(self) -> RepoInfo | None:
        """O alvo, quando ha exatamente um e a evidencia sustenta."""
        if self.confianca.acionavel and len(self.candidatos) == 1:
            return self.candidatos[0].repo
        return None

    @property
    def branch_base(self) -> str:
        r = self.repo
        return r.branch_base if r else ""


#: Pesos. `DECLARADA` supera `OBSERVADA` porque uma branch pode ser restos de uma
#: tentativa abandonada, enquanto um mapa e uma afirmacao de quem sabe.
PESO_MAPA = 100
PESO_BRANCH = 50


@dataclass(slots=True)
class ResolvedorDeAlvo:
    """Junta evidencia declarada e observada. Nao interpreta texto livre."""

    #: `rotulo -> chave de repo`, vindo da configuracao do workspace.
    por_rotulo: dict[str, str] = field(default_factory=dict)
    #: `projeto -> chave de repo`.
    por_projeto: dict[str, str] = field(default_factory=dict)
    #: `chave de task -> chave de repo`, para o caso pontual que nao cabe em regra.
    por_task: dict[str, str] = field(default_factory=dict)

    def resolve(self, task: ExternalTask, repos: list[RepoInfo],
                branches: dict[str, list[Branch]] | None = None) -> Alvo:
        por_chave = {r.ref.key: r for r in repos}
        # Nome curto tambem resolve, para que a configuracao possa dizer
        # `dashboard-api` em vez da chave inteira. Nome AMBIGUO entre dois
        # repositorios nao entra no indice: seria reintroduzir o chute.
        curtos: dict[str, list[RepoInfo]] = {}
        for r in repos:
            curtos.setdefault(r.nome.lower(), []).append(r)
        por_nome = {n: v[0] for n, v in curtos.items() if len(v) == 1}

        def acha(chave: str) -> RepoInfo | None:
            return por_chave.get(chave) or por_nome.get(chave.lower())

        achados: dict[str, list[Evidencia]] = {}
        forcas: dict[str, int] = {}

        def marca(repo: RepoInfo | None, ev: Evidencia, peso: int) -> None:
            if repo is None:
                return
            achados.setdefault(repo.ref.key, []).append(ev)
            forcas[repo.ref.key] = max(forcas.get(repo.ref.key, 0), peso)

        # --- 1. declarado ------------------------------------------------
        if task.key in self.por_task:
            marca(acha(self.por_task[task.key]),
                  Evidencia("mapa:task", f"{task.key} -> {self.por_task[task.key]}"),
                  PESO_MAPA)
        for rot in task.rotulos:
            if rot in self.por_rotulo:
                marca(acha(self.por_rotulo[rot]),
                      Evidencia("mapa:rotulo", f"rotulo '{rot}' -> {self.por_rotulo[rot]}"),
                      PESO_MAPA)
        if task.projeto in self.por_projeto:
            marca(acha(self.por_projeto[task.projeto]),
                  Evidencia("mapa:projeto",
                            f"projeto '{task.projeto}' -> {self.por_projeto[task.projeto]}"),
                  PESO_MAPA)

        # --- 2. observado no mundo ---------------------------------------
        # Casamento por palavra inteira: `SG-11` nao pode casar com `SG-110`.
        alvo_re = re.compile(rf"\b{re.escape(task.key.upper())}\b")
        for chave, lista in (branches or {}).items():
            repo = por_chave.get(chave)
            if repo is None:
                continue
            for b in lista:
                if alvo_re.search(b.nome.upper()):
                    marca(repo, Evidencia("branch", f"'{b.nome}' cita {task.key}"),
                          PESO_BRANCH)
                    break

        if not achados:
            return Alvo(task_key=task.key, confianca=Confianca.AUSENTE,
                        motivo="nenhuma evidencia declarada nem observada liga esta "
                               "task a um repositorio")

        melhor = max(forcas.values())
        vencedores = [k for k, f in forcas.items() if f == melhor]
        candidatos = tuple(
            Candidato(repo=por_chave[k], evidencias=tuple(achados[k]), forca=forcas[k])
            for k in sorted(vencedores))

        if len(vencedores) > 1:
            return Alvo(task_key=task.key, confianca=Confianca.AMBIGUA,
                        candidatos=candidatos,
                        motivo=f"{len(vencedores)} repositorios com evidencia de mesma "
                               f"forca: {', '.join(vencedores)}")

        return Alvo(
            task_key=task.key,
            confianca=Confianca.DECLARADA if melhor >= PESO_MAPA else Confianca.OBSERVADA,
            candidatos=candidatos,
            motivo="; ".join(e.detalhe for e in candidatos[0].evidencias))

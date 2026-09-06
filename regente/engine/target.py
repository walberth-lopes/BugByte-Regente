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


class Confidence(str, Enum):
    """Quanto o motor sabe sobre onde a task roda.

    Nao existe grau intermediario inventado. Ou a evidencia e declarada por
    alguem, ou e observada no mundo, ou nao existe.
    """
    #: Alguem declarou explicitamente. Nao ha o que interpretar.
    DECLARED = "DECLARADA"
    #: O mundo mostra trabalho ja comecado num repositorio (branch com a chave).
    OBSERVED = "OBSERVADA"
    #: Mais de um candidato com a mesma forca. O motor NAO desempata.
    AMBIGUOUS = "AMBIGUA"
    #: Nenhuma evidencia. O motor nao chuta.
    ABSENT = "AUSENTE"

    @property
    def actionable(self) -> bool:
        return self in (Confidence.DECLARED, Confidence.OBSERVED)


@dataclass(frozen=True, slots=True)
class Evidence:
    """Por que este repositorio e candidato. Sem isto, nada e auditavel."""
    fonte: str
    detalhe: str


@dataclass(frozen=True, slots=True)
class Candidate:
    repo: RepoInfo
    evidence: tuple[Evidence, ...]
    #: Peso da evidencia mais forte que sustenta este candidato.
    strength: int = 0


@dataclass(frozen=True, slots=True)
class Target:
    """O resultado da resolucao. Pode legitimamente nao ter repositorio."""
    task_key: str
    confidence: Confidence
    candidatos: tuple[Candidate, ...] = ()
    reason: str = ""

    @property
    def repo(self) -> RepoInfo | None:
        """O alvo, quando ha exatamente um e a evidencia sustenta."""
        if self.confidence.actionable and len(self.candidatos) == 1:
            return self.candidatos[0].repo
        return None

    @property
    def base_branch(self) -> str:
        r = self.repo
        return r.base_branch if r else ""


#: Pesos. `DECLARADA` supera `OBSERVADA` porque uma branch pode ser restos de uma
#: tentativa abandonada, enquanto um mapa e uma afirmacao de quem sabe.
WEIGHT_DECLARED = 100
WEIGHT_BRANCH = 50


@dataclass(slots=True)
class TargetResolver:
    """Junta evidencia declarada e observada. Nao interpreta texto livre."""

    #: `rotulo -> chave de repo`, vindo da configuracao do workspace.
    by_label: dict[str, str] = field(default_factory=dict)
    #: `projeto -> chave de repo`.
    by_project: dict[str, str] = field(default_factory=dict)
    #: `chave de task -> chave de repo`, para o caso pontual que nao cabe em regra.
    by_task: dict[str, str] = field(default_factory=dict)

    def resolve(self, task: ExternalTask, repos: list[RepoInfo],
                branches: dict[str, list[Branch]] | None = None) -> Target:
        by_key = {r.ref.key: r for r in repos}
        # Nome curto tambem resolve, para que a configuracao possa dizer
        # `dashboard-api` em vez da chave inteira. Nome AMBIGUO entre dois
        # repositorios nao entra no indice: seria reintroduzir o chute.
        short_names: dict[str, list[RepoInfo]] = {}
        for r in repos:
            short_names.setdefault(r.name.lower(), []).append(r)
        by_name = {n: v[0] for n, v in short_names.items() if len(v) == 1}

        def find_repo(key: str) -> RepoInfo | None:
            return by_key.get(key) or by_name.get(key.lower())

        findings: dict[str, list[Evidence]] = {}
        strengths: dict[str, int] = {}

        def mark(repo: RepoInfo | None, ev: Evidence, peso: int) -> None:
            if repo is None:
                return
            findings.setdefault(repo.ref.key, []).append(ev)
            strengths[repo.ref.key] = max(strengths.get(repo.ref.key, 0), peso)

        # --- 1. declarado ------------------------------------------------
        if task.key in self.by_task:
            mark(find_repo(self.by_task[task.key]),
                  Evidence("mapa:task", f"{task.key} -> {self.by_task[task.key]}"),
                  WEIGHT_DECLARED)
        for label in task.labels:
            if label in self.by_label:
                mark(find_repo(self.by_label[label]),
                      Evidence("mapa:rotulo", f"rotulo '{label}' -> {self.by_label[label]}"),
                      WEIGHT_DECLARED)
        if task.project in self.by_project:
            mark(find_repo(self.by_project[task.project]),
                  Evidence("mapa:projeto",
                            f"projeto '{task.project}' -> {self.by_project[task.project]}"),
                  WEIGHT_DECLARED)

        # --- 2. observado no mundo ---------------------------------------
        # Casamento por palavra inteira: `SG-11` nao pode casar com `SG-110`.
        alvo_re = re.compile(rf"\b{re.escape(task.key.upper())}\b")
        for key, lista in (branches or {}).items():
            repo = by_key.get(key)
            if repo is None:
                continue
            for b in lista:
                if alvo_re.search(b.name.upper()):
                    mark(repo, Evidence("branch", f"'{b.name}' cita {task.key}"),
                          WEIGHT_BRANCH)
                    break

        if not findings:
            return Target(task_key=task.key, confidence=Confidence.ABSENT,
                        reason="nenhuma evidencia declarada nem observada liga esta "
                               "task a um repositorio")

        best = max(strengths.values())
        winners = [k for k, f in strengths.items() if f == best]
        candidatos = tuple(
            Candidate(repo=by_key[k], evidence=tuple(findings[k]), strength=strengths[k])
            for k in sorted(winners))

        if len(winners) > 1:
            return Target(task_key=task.key, confidence=Confidence.AMBIGUOUS,
                        candidatos=candidatos,
                        reason=f"{len(winners)} repositorios com evidencia de mesma "
                               f"forca: {', '.join(winners)}")

        return Target(
            task_key=task.key,
            confidence=Confidence.DECLARED if best >= WEIGHT_DECLARED else Confidence.OBSERVED,
            candidatos=candidatos,
            reason="; ".join(e.detalhe for e in candidatos[0].evidence))

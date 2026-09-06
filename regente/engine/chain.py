# -*- coding: utf-8 -*-
"""A cadeia de execucao, em sombra: da task real ao candidato a execucao.

    task -> repositorio candidato -> contexto -> branch base
         -> recursos/isolamento -> risco/policy -> candidato a execucao

Cada elo pode reprovar, e reprovar e um desfecho normal -- nao uma falha. O valor
deste modulo esta em dizer **em qual elo** o trabalho parou: "nao ha o que fazer"
e "nao sei onde fazer" e "nao posso fazer" exigem acoes completamente diferentes
do dono, e um numero unico as esconderia.

Nada aqui muta o mundo. Nada aqui grava estado. A cadeia e recalculavel a partir
do zero a qualquer momento, e e isso que a torna segura de rodar a vontade.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from ..core.policy import Action, AutonomyLevel, Decision, Effect, PolicyContext, PolicyEngine
from ..core.risk import RiskAssessment, RiskEngine
from ..ports.repository import Branch, RepoCapability, RepoInfo
from ..ports.tasks import ExternalTask
from .target import Target, Confidence, TargetResolver


class Stage(str, Enum):
    """Onde a cadeia parou. Vocabulario fechado: cada valor pede uma acao."""
    SEM_TRABALHO = "SEM_TRABALHO"          # a origem diz que nao esta disponivel
    SEM_ALVO = "SEM_ALVO"                  # ninguem sabe em que repositorio roda
    ALVO_AMBIGUO = "ALVO_AMBIGUO"          # mais de um candidato empatado
    REPO_INUTILIZAVEL = "REPO_INUTILIZAVEL"  # arquivado, ou sem branch base
    SEM_CAPACIDADE = "SEM_CAPACIDADE"      # o adapter nao faz o que seria preciso
    BARRADO_POR_POLICY = "BARRADO_POR_POLICY"
    PRECISA_HUMANO = "PRECISA_HUMANO"
    CANDIDATO = "CANDIDATO"                # passou por tudo

    @property
    def executable(self) -> bool:
        return self is Stage.CANDIDATO


@dataclass(frozen=True, slots=True)
class Step:
    task: ExternalTask
    elo: Stage
    reason: str
    target: Target | None = None
    repo: RepoInfo | None = None
    base_branch: str = ""
    work_branch: str = ""
    resources: tuple[str, ...] = ()
    risk: RiskAssessment | None = None
    decision: Decision | None = None

    @property
    def summary(self) -> str:
        where = self.repo.ref.key if self.repo else "-"
        return f"{self.task.key:<10} {self.elo.value:<20} {where}"


@dataclass(slots=True)
class ChainReport:
    workspace: str
    tasks: int = 0
    repos: int = 0
    by_stage: Counter = field(default_factory=Counter)
    by_confidence: Counter = field(default_factory=Counter)
    steps: tuple[Step, ...] = ()
    mutations: int = 0

    @property
    def candidates(self) -> tuple[Step, ...]:
        return tuple(p for p in self.steps if p.elo.executable)


#: A acao que o motor precisaria executar para trabalhar numa task. Declarada
#: aqui para que policy e risco sejam avaliados sobre a MESMA acao que seria
#: pedida de verdade -- avaliar uma acao generica daria um veredito que nao
#: corresponde a nada.
WORK_ACTION = "repo.branch"

#: Capacidades sem as quais nao ha como comecar trabalho de codigo.
REQUIRED_CAPS = (RepoCapability.LER_ARQUIVOS, RepoCapability.CLONAR)


def build(
    workspace_nome: str,
    workspace_id: str,
    tasks: list[ExternalTask],
    repos: list[RepoInfo],
    resolvedor: TargetResolver,
    policy: PolicyEngine,
    risk: RiskEngine,
    autonomy: AutonomyLevel,
    branches: dict[str, list[Branch]] | None = None,
    environment: str = "staging",
    organization: str = "*",
    client: str = "*",
) -> ChainReport:
    rel = ChainReport(workspace=workspace_nome, tasks=len(tasks), repos=len(repos))
    steps: list[Step] = []

    for t in tasks:
        # --- elo 1: ha trabalho? ---------------------------------------
        if not t.status.available:
            steps.append(Step(t, Stage.SEM_TRABALHO,
                                f"a origem diz {t.external_status or t.status.value}"))
            continue

        # --- elo 2: onde? ----------------------------------------------
        target = resolvedor.resolve(t, repos, branches)
        rel.by_confidence[target.confidence.value] += 1
        if target.confidence is Confidence.ABSENT:
            steps.append(Step(t, Stage.SEM_ALVO, target.reason, target=target))
            continue
        if target.confidence is Confidence.AMBIGUOUS:
            steps.append(Step(t, Stage.ALVO_AMBIGUO, target.reason, target=target))
            continue

        repo = target.repo
        assert repo is not None   # garantido por Confianca.acionavel + 1 candidato

        # --- elo 3: da para trabalhar nele? -----------------------------
        if not repo.usable:
            steps.append(Step(t, Stage.REPO_INUTILIZAVEL,
                                "; ".join(repo.anomalies) or "sem branch base",
                                target=target, repo=repo))
            continue
        faltando = [c.value for c in REQUIRED_CAPS if not repo.can(c)]
        if faltando:
            # Descobrir isto agora poupa um ciclo inteiro -- e poupa uma
            # escalonada ao humano por um motivo que o motor ja sabia.
            steps.append(Step(t, Stage.SEM_CAPACIDADE,
                                f"o provedor nao oferece: {', '.join(faltando)}",
                                target=target, repo=repo))
            continue

        # --- elo 4: isolamento ------------------------------------------
        # O recurso e escopado pelo workspace. Repositorio homonimo em outro
        # cliente e outro recurso, e nao pode disputar a mesma trava.
        resources = (repo.ref.resource(workspace_id),)
        branch = f"regente/{t.key.lower()}"

        # --- elo 5: risco e policy --------------------------------------
        assessment = risk.assess({
            "action": WORK_ACTION,
            "environment": environment,
            "category": "repo",
            "paths": [t.title, *t.labels],
        })
        decision = policy.decide(PolicyContext(
            action=Action(kind=WORK_ACTION, resource=repo.ref.key,
                          environment=environment),
            organization=organization, client=client, workspace=workspace_nome,
            project=t.project or "*", agent="coder",
            risk=assessment.level.name, autonomy=autonomy))

        comum = dict(target=target, repo=repo, base_branch=repo.base_branch,
                     work_branch=branch, resources=resources,
                     risk=assessment, decision=decision)
        if decision.effect == Effect.DENY:
            steps.append(Step(t, Stage.BARRADO_POR_POLICY, decision.reason, **comum))
            continue
        if decision.effect == Effect.HUMAN_APPROVAL:
            steps.append(Step(t, Stage.PRECISA_HUMANO, decision.reason, **comum))
            continue

        steps.append(Step(t, Stage.CANDIDATO,
                            f"risco {assessment.level.name}; base {repo.base_branch}",
                            **comum))

    rel.steps = tuple(steps)
    rel.by_stage = Counter(p.elo.value for p in steps)
    return rel


def render(rel: ChainReport, limit: int = 10) -> str:
    lines = [
        "CADEIA DE EXECUCAO (sombra)",
        "",
        f"  Workspace                  {rel.workspace}",
        f"  Tasks                      {rel.tasks}",
        f"  Repositorios               {rel.repos}",
        f"  Mutacoes                   {rel.mutations}   <- tem de ser 0",
        "",
        "  ONDE A CADEIA PAROU",
    ]
    for elo, n in rel.by_stage.most_common():
        lines.append(f"    {elo:<22} {n}")

    if rel.by_confidence:
        lines += ["", "  CONFIANCA NO ALVO (das que tinham trabalho)"]
        for c, n in rel.by_confidence.most_common():
            lines.append(f"    {c:<22} {n}")

    candidates = rel.candidates
    lines += ["", f"  CANDIDATOS A EXECUCAO ({len(candidates)})"]
    for p in candidates[:limit]:
        lines.append(f"    {p.task.key:<10} -> {p.repo.ref.key}")
        lines.append(f"                  base={p.base_branch}  branch={p.work_branch}")
        lines.append(f"                  recurso={p.resources[0]}")
        lines.append(f"                  risco={p.risk.level.name}  policy={p.decision.effect}"
                      f" ({p.decision.rule})")
        lines.append(f"                  evidencia: {p.target.reason[:70]}")
    if len(candidates) > limit:
        lines.append(f"    ... mais {len(candidates) - limit}")

    ambiguos = [p for p in rel.steps if p.elo is Stage.ALVO_AMBIGUO]
    if ambiguos:
        lines += ["", f"  AMBIGUOS -- o motor NAO desempata ({len(ambiguos)})"]
        for p in ambiguos[:5]:
            lines.append(f"    {p.task.key:<10} {p.reason[:80]}")
    return "\n".join(lines)

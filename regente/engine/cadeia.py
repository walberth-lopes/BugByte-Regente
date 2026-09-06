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
from ..ports.repository import Branch, CapacidadeRepo, RepoInfo
from ..ports.tasks import ExternalTask
from .alvo import Alvo, Confianca, ResolvedorDeAlvo


class Elo(str, Enum):
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
    def executavel(self) -> bool:
        return self is Elo.CANDIDATO


@dataclass(frozen=True, slots=True)
class Passo:
    task: ExternalTask
    elo: Elo
    motivo: str
    alvo: Alvo | None = None
    repo: RepoInfo | None = None
    branch_base: str = ""
    branch_de_trabalho: str = ""
    recursos: tuple[str, ...] = ()
    risco: RiskAssessment | None = None
    decisao: Decision | None = None

    @property
    def resumo(self) -> str:
        onde = self.repo.ref.key if self.repo else "-"
        return f"{self.task.key:<10} {self.elo.value:<20} {onde}"


@dataclass(slots=True)
class RelatorioCadeia:
    workspace: str
    tasks: int = 0
    repos: int = 0
    por_elo: Counter = field(default_factory=Counter)
    por_confianca: Counter = field(default_factory=Counter)
    passos: tuple[Passo, ...] = ()
    mutacoes: int = 0

    @property
    def candidatos(self) -> tuple[Passo, ...]:
        return tuple(p for p in self.passos if p.elo.executavel)


#: A acao que o motor precisaria executar para trabalhar numa task. Declarada
#: aqui para que policy e risco sejam avaliados sobre a MESMA acao que seria
#: pedida de verdade -- avaliar uma acao generica daria um veredito que nao
#: corresponde a nada.
ACAO_DE_TRABALHO = "repo.branch"

#: Capacidades sem as quais nao ha como comecar trabalho de codigo.
EXIGIDAS = (CapacidadeRepo.LER_ARQUIVOS, CapacidadeRepo.CLONAR)


def monta(
    workspace_nome: str,
    workspace_id: str,
    tasks: list[ExternalTask],
    repos: list[RepoInfo],
    resolvedor: ResolvedorDeAlvo,
    policy: PolicyEngine,
    risco: RiskEngine,
    autonomia: AutonomyLevel,
    branches: dict[str, list[Branch]] | None = None,
    ambiente: str = "staging",
    organizacao: str = "*",
    cliente: str = "*",
) -> RelatorioCadeia:
    rel = RelatorioCadeia(workspace=workspace_nome, tasks=len(tasks), repos=len(repos))
    passos: list[Passo] = []

    for t in tasks:
        # --- elo 1: ha trabalho? ---------------------------------------
        if not t.situacao.disponivel:
            passos.append(Passo(t, Elo.SEM_TRABALHO,
                                f"a origem diz {t.estado_externo or t.situacao.value}"))
            continue

        # --- elo 2: onde? ----------------------------------------------
        alvo = resolvedor.resolve(t, repos, branches)
        rel.por_confianca[alvo.confianca.value] += 1
        if alvo.confianca is Confianca.AUSENTE:
            passos.append(Passo(t, Elo.SEM_ALVO, alvo.motivo, alvo=alvo))
            continue
        if alvo.confianca is Confianca.AMBIGUA:
            passos.append(Passo(t, Elo.ALVO_AMBIGUO, alvo.motivo, alvo=alvo))
            continue

        repo = alvo.repo
        assert repo is not None   # garantido por Confianca.acionavel + 1 candidato

        # --- elo 3: da para trabalhar nele? -----------------------------
        if not repo.utilizavel:
            passos.append(Passo(t, Elo.REPO_INUTILIZAVEL,
                                "; ".join(repo.anomalias) or "sem branch base",
                                alvo=alvo, repo=repo))
            continue
        faltando = [c.value for c in EXIGIDAS if not repo.pode(c)]
        if faltando:
            # Descobrir isto agora poupa um ciclo inteiro -- e poupa uma
            # escalonada ao humano por um motivo que o motor ja sabia.
            passos.append(Passo(t, Elo.SEM_CAPACIDADE,
                                f"o provedor nao oferece: {', '.join(faltando)}",
                                alvo=alvo, repo=repo))
            continue

        # --- elo 4: isolamento ------------------------------------------
        # O recurso e escopado pelo workspace. Repositorio homonimo em outro
        # cliente e outro recurso, e nao pode disputar a mesma trava.
        recursos = (repo.ref.recurso(workspace_id),)
        branch = f"regente/{t.key.lower()}"

        # --- elo 5: risco e policy --------------------------------------
        avaliacao = risco.avalia({
            "acao": ACAO_DE_TRABALHO,
            "ambiente": ambiente,
            "categoria": "repo",
            "caminhos": [t.titulo, *t.rotulos],
        })
        decisao = policy.decide(PolicyContext(
            action=Action(kind=ACAO_DE_TRABALHO, resource=repo.ref.key,
                          environment=ambiente),
            organization=organizacao, client=cliente, workspace=workspace_nome,
            project=t.projeto or "*", agent="coder",
            risk=avaliacao.nivel.name, autonomy=autonomia))

        comum = dict(alvo=alvo, repo=repo, branch_base=repo.branch_base,
                     branch_de_trabalho=branch, recursos=recursos,
                     risco=avaliacao, decisao=decisao)
        if decisao.efeito == Effect.DENY:
            passos.append(Passo(t, Elo.BARRADO_POR_POLICY, decisao.motivo, **comum))
            continue
        if decisao.efeito == Effect.HUMAN_APPROVAL:
            passos.append(Passo(t, Elo.PRECISA_HUMANO, decisao.motivo, **comum))
            continue

        passos.append(Passo(t, Elo.CANDIDATO,
                            f"risco {avaliacao.nivel.name}; base {repo.branch_base}",
                            **comum))

    rel.passos = tuple(passos)
    rel.por_elo = Counter(p.elo.value for p in passos)
    return rel


def texto(rel: RelatorioCadeia, limite: int = 10) -> str:
    linhas = [
        "CADEIA DE EXECUCAO (sombra)",
        "",
        f"  Workspace                  {rel.workspace}",
        f"  Tasks                      {rel.tasks}",
        f"  Repositorios               {rel.repos}",
        f"  Mutacoes                   {rel.mutacoes}   <- tem de ser 0",
        "",
        "  ONDE A CADEIA PAROU",
    ]
    for elo, n in rel.por_elo.most_common():
        linhas.append(f"    {elo:<22} {n}")

    if rel.por_confianca:
        linhas += ["", "  CONFIANCA NO ALVO (das que tinham trabalho)"]
        for c, n in rel.por_confianca.most_common():
            linhas.append(f"    {c:<22} {n}")

    candidatos = rel.candidatos
    linhas += ["", f"  CANDIDATOS A EXECUCAO ({len(candidatos)})"]
    for p in candidatos[:limite]:
        linhas.append(f"    {p.task.key:<10} -> {p.repo.ref.key}")
        linhas.append(f"                  base={p.branch_base}  branch={p.branch_de_trabalho}")
        linhas.append(f"                  recurso={p.recursos[0]}")
        linhas.append(f"                  risco={p.risco.nivel.name}  policy={p.decisao.efeito}"
                      f" ({p.decisao.regra})")
        linhas.append(f"                  evidencia: {p.alvo.motivo[:70]}")
    if len(candidatos) > limite:
        linhas.append(f"    ... mais {len(candidatos) - limite}")

    ambiguos = [p for p in rel.passos if p.elo is Elo.ALVO_AMBIGUO]
    if ambiguos:
        linhas += ["", f"  AMBIGUOS -- o motor NAO desempata ({len(ambiguos)})"]
        for p in ambiguos[:5]:
            linhas.append(f"    {p.task.key:<10} {p.motivo[:80]}")
    return "\n".join(linhas)

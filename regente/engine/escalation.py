# -*- coding: utf-8 -*-
"""A fila NEEDS ME.

O criterio de entrada e estreito de proposito. Um item so aparece aqui quando a
resposta **nao existe dentro do sistema**:

- a policy exige autoridade humana (merge em producao, por exemplo);
- o contrato do trabalho e ambiguo e nenhuma leitura extra resolve;
- a escada de recuperacao acabou;
- o backlog se autobloqueia (ciclo de dependencias).

Nao entram aqui: risco alto (isso compra segunda passada, nao espera), teste
vermelho (isso e trabalho), error transitorio (isso e retentativa). Encher esta
fila com o que o motor poderia resolver e o unico jeito garantido de fazer o dono
parar de le-la.

O card carrega decisao, nao diagnostico. Log fica no evento, sob demanda.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core import ids
from ..core.model import Approval, Option, Task
from ..core.risk import RiskLevel

#: Opcoes padrao. Toda escalonada oferece pelo menos: seguir a recomendacao,
#: mandar investigar mais, ou parar. "Parar" precisa estar sempre disponivel --
#: sem ela, a unica saida do dono seria mexer no banco.
FOLLOW = Option("seguir", "Aprovar a recomendacao", "o motor executa o caminho recomendado")
INVESTIGATE = Option("investigar", "Pedir mais investigacao", "devolve a task para analise")
BLOCK = Option("bloquear", "Bloquear a task", "sai da fila de trabalho ate alguem destravar")
CANCEL = Option("cancelar", "Cancelar a task", "encerra o trabalho")


@dataclass(frozen=True, slots=True)
class Briefing:
    """A projecao do Approval para a superficie. Sem vocabulario interno."""
    id: str
    key: str
    title: str
    what_happened: str
    why_it_matters: str
    what_was_tried: tuple[str, ...]
    options: tuple[dict[str, str], ...]
    recommendation: str | None
    risk: str


def build(
    task: Task,
    what_happened: str,
    why_it_matters: str,
    attempts: tuple[str, ...] = (),
    options: tuple[Option, ...] = (),
    recommendation: str | None = FOLLOW.id,
    risk: RiskLevel = RiskLevel.MEDIUM,
    run_id: str | None = None,
) -> Approval:
    """Cria o item da fila. `por_que_importa` e obrigatorio e nao pode ser vazio.

    Um card que descreve o que aconteceu sem dizer por que importa devolve ao
    dono o trabalho de descobrir se aquilo merece atencao -- que e exatamente o
    trabalho que a fila deveria ter poupado.
    """
    if not why_it_matters.strip():
        raise ValueError("escalonada sem 'por que importa' nao entra na fila")
    return Approval(
        id=ids.new_id(ids.APPROVAL),
        workspace_id=task.workspace_id,
        task_id=task.id,
        run_id=run_id,
        what_happened=what_happened,
        why_it_matters=why_it_matters,
        what_was_tried=attempts,
        options=options or (FOLLOW, INVESTIGATE, BLOCK, CANCEL),
        recommendation=recommendation,
        risk=risk,
    )


def briefing(a: Approval, task: Task) -> Briefing:
    return Briefing(
        id=a.id,
        key=task.key,
        title=task.title,
        what_happened=a.what_happened,
        why_it_matters=a.why_it_matters,
        what_was_tried=a.what_was_tried,
        options=tuple({"id": o.id, "label": o.label, "effect": o.effect} for o in a.options),
        recommendation=a.recommendation,
        risk=a.risk.name,
    )


def render(b: Briefing) -> str:
    """Render de terminal. Mesmo conteudo que a UI mostra."""
    lines = [
        f"{b.key}  [{b.risk}]",
        f"  {b.title}",
        "",
        f"  O QUE ACONTECEU   {b.what_happened}",
        f"  POR QUE IMPORTA   {b.why_it_matters}",
    ]
    if b.what_was_tried:
        lines.append("  O QUE JA TENTEI   " + b.what_was_tried[0])
        lines += ["                    " + t for t in b.what_was_tried[1:]]
    lines.append("")
    for o in b.options:
        mark = "->" if o["id"] == b.recommendation else "  "
        lines.append(f"  {mark} [{o['id']}] {o['label']}")
    return "\n".join(lines)

# -*- coding: utf-8 -*-
"""A fila NEEDS ME.

O criterio de entrada e estreito de proposito. Um item so aparece aqui quando a
resposta **nao existe dentro do sistema**:

- a policy exige autoridade humana (merge em producao, por exemplo);
- o contrato do trabalho e ambiguo e nenhuma leitura extra resolve;
- a escada de recuperacao acabou;
- o backlog se autobloqueia (ciclo de dependencias).

Nao entram aqui: risco alto (isso compra segunda passada, nao espera), teste
vermelho (isso e trabalho), erro transitorio (isso e retentativa). Encher esta
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
SEGUIR = Option("seguir", "Aprovar a recomendacao", "o motor executa o caminho recomendado")
INVESTIGAR = Option("investigar", "Pedir mais investigacao", "devolve a task para analise")
BLOQUEAR = Option("bloquear", "Bloquear a task", "sai da fila de trabalho ate alguem destravar")
CANCELAR = Option("cancelar", "Cancelar a task", "encerra o trabalho")


@dataclass(frozen=True, slots=True)
class Briefing:
    """A projecao do Approval para a superficie. Sem vocabulario interno."""
    id: str
    chave: str
    titulo: str
    o_que_aconteceu: str
    por_que_importa: str
    o_que_o_agente_tentou: tuple[str, ...]
    opcoes: tuple[dict[str, str], ...]
    recomendacao: str | None
    risco: str


def monta(
    task: Task,
    o_que_aconteceu: str,
    por_que_importa: str,
    tentativas: tuple[str, ...] = (),
    opcoes: tuple[Option, ...] = (),
    recomendacao: str | None = SEGUIR.id,
    risco: RiskLevel = RiskLevel.MEDIUM,
    run_id: str | None = None,
) -> Approval:
    """Cria o item da fila. `por_que_importa` e obrigatorio e nao pode ser vazio.

    Um card que descreve o que aconteceu sem dizer por que importa devolve ao
    dono o trabalho de descobrir se aquilo merece atencao -- que e exatamente o
    trabalho que a fila deveria ter poupado.
    """
    if not por_que_importa.strip():
        raise ValueError("escalonada sem 'por que importa' nao entra na fila")
    return Approval(
        id=ids.novo(ids.APPROVAL),
        workspace_id=task.workspace_id,
        task_id=task.id,
        run_id=run_id,
        o_que_aconteceu=o_que_aconteceu,
        por_que_importa=por_que_importa,
        o_que_o_agente_tentou=tentativas,
        opcoes=opcoes or (SEGUIR, INVESTIGAR, BLOQUEAR, CANCELAR),
        recomendacao=recomendacao,
        risco=risco,
    )


def briefing(a: Approval, task: Task) -> Briefing:
    return Briefing(
        id=a.id,
        chave=task.chave,
        titulo=task.titulo,
        o_que_aconteceu=a.o_que_aconteceu,
        por_que_importa=a.por_que_importa,
        o_que_o_agente_tentou=a.o_que_o_agente_tentou,
        opcoes=tuple({"id": o.id, "rotulo": o.rotulo, "efeito": o.efeito} for o in a.opcoes),
        recomendacao=a.recomendacao,
        risco=a.risco.name,
    )


def texto(b: Briefing) -> str:
    """Render de terminal. Mesmo conteudo que a UI mostra."""
    linhas = [
        f"{b.chave}  [{b.risco}]",
        f"  {b.titulo}",
        "",
        f"  O QUE ACONTECEU   {b.o_que_aconteceu}",
        f"  POR QUE IMPORTA   {b.por_que_importa}",
    ]
    if b.o_que_o_agente_tentou:
        linhas.append("  O QUE JA TENTEI   " + b.o_que_o_agente_tentou[0])
        linhas += ["                    " + t for t in b.o_que_o_agente_tentou[1:]]
    linhas.append("")
    for o in b.opcoes:
        marca = "->" if o["id"] == b.recomendacao else "  "
        linhas.append(f"  {marca} [{o['id']}] {o['rotulo']}")
    return "\n".join(linhas)

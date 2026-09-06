# -*- coding: utf-8 -*-
"""Runners que nao dependem de LLM.

`ScriptedRunner` executa um roteiro declarado. Ele existe para duas coisas
legitimas -- exercitar o motor em teste e rodar a **fase de sombra**, em que o
dono confere as decisoes do orquestrador antes de qualquer worker real tocar
codigo. Ele nao finge ser um agente: seu desfecho vem do roteiro, e por isso
nenhum defeito de arquitetura consegue se esconder atras dele.

`ComandoRunner` executa um processo externo. E o caminho real: qualquer harness
agentico headless, um script proprio ou um laco sobre LLMProvider entram por
aqui sem que o Orchestrator saiba a diferenca.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...ports.workspace import AgentRunner, RunRequest, RunResult


@dataclass(slots=True)
class ScriptedRunner(AgentRunner):
    nome: str = "roteiro"
    #: chave da task -> desfecho declarado
    roteiro: dict[str, dict[str, Any]] = field(default_factory=dict)
    padrao: dict[str, Any] = field(default_factory=lambda: {"ok": True, "resumo": "sem alteracao"})

    def run(self, pedido: RunRequest) -> RunResult:
        chave = pedido.contexto.get("chave", pedido.task_id)
        d = self.roteiro.get(chave, self.padrao)
        # A area existe e e do worker: escrever nela prova que o isolamento
        # funcionou, e deixa rastro para inspecao depois do tick.
        Path(pedido.area.caminho).mkdir(parents=True, exist_ok=True)
        (Path(pedido.area.caminho) / "run.json").write_text(
            json.dumps({"run": pedido.run_id, "objetivo": pedido.objetivo, "desfecho": d},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return RunResult(
            ok=bool(d.get("ok", True)),
            resumo=str(d.get("resumo", "")),
            desfecho=str(d.get("desfecho", "concluido" if d.get("ok", True) else "erro")),
            custo_usd=float(d.get("custo_usd", 0.0)),
            tokens=int(d.get("tokens", 0)),
            chamadas_tool=int(d.get("chamadas_tool", 0)),
            iteracoes=int(d.get("iteracoes", 1)),
            pergunta=d.get("pergunta"))


@dataclass(slots=True)
class ComandoRunner(AgentRunner):
    """Executa um comando externo na area do worker.

    Contrato com o processo: ele recebe o pedido como JSON no stdin e deve
    imprimir um JSON de resultado no stdout. Saida ilegivel e tratada como falha
    -- e nao como sucesso silencioso -- porque um worker que nao consegue
    relatar o que fez nao pode ser considerado bem-sucedido.
    """
    comando: list[str] = field(default_factory=list)
    nome: str = "comando"
    timeout_folga: int = 120

    def run(self, pedido: RunRequest) -> RunResult:
        entrada = json.dumps({
            "run_id": pedido.run_id, "task_id": pedido.task_id, "agente": pedido.agente,
            "objetivo": pedido.objetivo, "area": pedido.area.caminho,
            "branch": pedido.area.branch, "contexto": pedido.contexto,
            "limites": {"iteracoes": pedido.limite_iteracoes,
                        "tool_calls": pedido.limite_tool_calls,
                        "custo_usd": pedido.limite_custo_usd,
                        "segundos": pedido.limite_segundos},
        }, ensure_ascii=False)
        try:
            p = subprocess.run(
                self.comando, input=entrada, cwd=pedido.area.caminho,
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=pedido.limite_segundos + self.timeout_folga)
        except subprocess.TimeoutExpired:
            return RunResult(ok=False, resumo=f"estourou {pedido.limite_segundos}s",
                             desfecho="timebox")
        if p.returncode != 0:
            return RunResult(ok=False, desfecho="erro",
                             resumo=f"rc={p.returncode}: {(p.stderr or '').strip()[:400]}")
        try:
            d = json.loads((p.stdout or "").strip() or "{}")
        except ValueError:
            return RunResult(ok=False, desfecho="erro",
                             resumo=f"saida nao e JSON: {(p.stdout or '')[:200]}")
        return RunResult(
            ok=bool(d.get("ok", False)), resumo=str(d.get("resumo", "")),
            desfecho=str(d.get("desfecho", "concluido" if d.get("ok") else "erro")),
            artefatos=d.get("artefatos") or {}, custo_usd=float(d.get("custo_usd", 0.0)),
            tokens=int(d.get("tokens", 0)), chamadas_tool=int(d.get("chamadas_tool", 0)),
            iteracoes=int(d.get("iteracoes", 0)), pergunta=d.get("pergunta"))

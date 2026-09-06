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
    name: str = "roteiro"
    #: chave da task -> desfecho declarado
    script: dict[str, dict[str, Any]] = field(default_factory=dict)
    default_value: dict[str, Any] = field(default_factory=lambda: {"ok": True, "resumo": "sem alteracao"})

    def run(self, request: RunRequest) -> RunResult:
        key = request.contexto.get("chave", request.task_id)
        d = self.script.get(key, self.default_value)
        # A area existe e e do worker: escrever nela prova que o isolamento
        # funcionou, e deixa rastro para inspecao depois do tick.
        Path(request.area.path).mkdir(parents=True, exist_ok=True)
        (Path(request.area.path) / "run.json").write_text(
            json.dumps({"run": request.run_id, "objetivo": request.goal, "desfecho": d},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return RunResult(
            ok=bool(d.get("ok", True)),
            summary=str(d.get("resumo", "")),
            outcome=str(d.get("desfecho", "concluido" if d.get("ok", True) else "error")),
            cost_usd=float(d.get("custo_usd", 0.0)),
            tokens=int(d.get("tokens", 0)),
            tool_calls=int(d.get("chamadas_tool", 0)),
            iterations=int(d.get("iteracoes", 1)),
            question=d.get("pergunta"))


@dataclass(slots=True)
class CommandRunner(AgentRunner):
    """Executa um comando externo na area do worker.

    Contrato com o processo: ele recebe o pedido como JSON no stdin e deve
    imprimir um JSON de resultado no stdout. Saida ilegivel e tratada como falha
    -- e nao como success silencioso -- porque um worker que nao consegue
    relatar o que fez nao pode ser considerado bem-sucedido.
    """
    command: list[str] = field(default_factory=list)
    name: str = "comando"
    timeout_slack: int = 120

    def run(self, request: RunRequest) -> RunResult:
        payload = json.dumps({
            "run_id": request.run_id, "task_id": request.task_id, "agente": request.agent,
            "objetivo": request.goal, "area": request.area.path,
            "branch": request.area.branch, "contexto": request.contexto,
            "limites": {"iteracoes": request.limit_iterations,
                        "tool_calls": request.limit_tool_calls,
                        "custo_usd": request.limit_cost_usd,
                        "segundos": request.limit_seconds},
        }, ensure_ascii=False)
        try:
            p = subprocess.run(
                self.command, input=payload, cwd=request.area.path,
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=request.limit_seconds + self.timeout_slack)
        except subprocess.TimeoutExpired:
            return RunResult(ok=False, summary=f"estourou {request.limit_seconds}s",
                             outcome="timebox")
        if p.returncode != 0:
            return RunResult(ok=False, outcome="error",
                             summary=f"rc={p.returncode}: {(p.stderr or '').strip()[:400]}")
        try:
            d = json.loads((p.stdout or "").strip() or "{}")
        except ValueError:
            return RunResult(ok=False, outcome="error",
                             summary=f"saida nao e JSON: {(p.stdout or '')[:200]}")
        return RunResult(
            ok=bool(d.get("ok", False)), summary=str(d.get("resumo", "")),
            outcome=str(d.get("desfecho", "concluido" if d.get("ok") else "error")),
            artifacts=d.get("artefatos") or {}, cost_usd=float(d.get("custo_usd", 0.0)),
            tokens=int(d.get("tokens", 0)), tool_calls=int(d.get("chamadas_tool", 0)),
            iterations=int(d.get("iteracoes", 0)), question=d.get("pergunta"))

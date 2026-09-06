# -*- coding: utf-8 -*-
"""Registro de adapters: nome na configuracao -> fabrica.

Este e o **unico** modulo do motor que importa adapters, e ele fica fora do Core
e do Engine de proposito. Adicionar um provedor novo e adicionar uma entrada
aqui; se algum dia for preciso mexer em `core/` ou `engine/` para isso, a
abstracao falhou -- e o teste de fronteira acusa.

O import e tardio (dentro da fabrica) para que um adapter com dependencia pesada
nao seja exigido de quem nao o usa: o motor precisa subir num ambiente sem SDK
de nuvem nenhum.
"""

from __future__ import annotations

from typing import Any, Callable

from ..ports import Capability, Port

Fabrica = Callable[[dict[str, Any]], Port]
_REGISTRO: dict[tuple[Capability, str], Fabrica] = {}


def registra(cap: Capability, nome: str, fabrica: Fabrica) -> None:
    _REGISTRO[(cap, nome)] = fabrica


def cria(cap: Capability, nome: str, opcoes: dict[str, Any] | None = None) -> Port:
    chave = (cap, nome)
    if chave not in _REGISTRO:
        disponiveis = sorted(n for (c, n) in _REGISTRO if c == cap)
        raise KeyError(
            f"nao existe adapter '{nome}' para {cap.value}. "
            f"Disponiveis: {', '.join(disponiveis) or 'nenhum'}")
    return _REGISTRO[chave](opcoes or {})


def disponiveis(cap: Capability | None = None) -> dict[str, list[str]]:
    saida: dict[str, list[str]] = {}
    for (c, n) in sorted(_REGISTRO, key=lambda k: (k[0].value, k[1])):
        if cap is None or c == cap:
            saida.setdefault(c.value, []).append(n)
    return saida


# ---- fabricas embutidas -------------------------------------------------

def _tasks_filesystem(o: dict[str, Any]) -> Port:
    from .tasks.filesystem import FilesystemTasks
    return FilesystemTasks(o["diretorio"])


def _workspace_diretorio(o: dict[str, Any]) -> Port:
    from .workspace.local import DiretorioIsolado
    return DiretorioIsolado(o["raiz"])


def _workspace_worktree(o: dict[str, Any]) -> Port:
    from .workspace.local import GitWorktree
    return GitWorktree(clones=o.get("clones", {}), raiz=o["raiz"])


def _notify_console(o: dict[str, Any]) -> Port:
    from .notify.console import Console
    return Console(jornal=o.get("jornal"))


def _runner_roteiro(o: dict[str, Any]) -> Port:
    from .runner.scripted import ScriptedRunner
    return ScriptedRunner(roteiro=o.get("roteiro", {}), padrao=o.get("padrao", {"ok": True, "resumo": "sem alteracao"}))


def _runner_comando(o: dict[str, Any]) -> Port:
    from .runner.scripted import ComandoRunner
    return ComandoRunner(comando=list(o["comando"]))


registra(Capability.TASKS, "filesystem", _tasks_filesystem)
registra(Capability.WORKSPACE, "diretorio", _workspace_diretorio)
registra(Capability.WORKSPACE, "worktree", _workspace_worktree)
registra(Capability.NOTIFICATION, "console", _notify_console)
registra(Capability.RUNNER, "roteiro", _runner_roteiro)
registra(Capability.RUNNER, "comando", _runner_comando)

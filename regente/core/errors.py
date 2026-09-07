# -*- coding: utf-8 -*-
"""Erros do dominio. Nenhum deles carrega detalhe de fornecedor."""


class RegenteError(Exception):
    """Raiz. Quem captura Regente captura tudo do motor."""


class InvalidTransition(RegenteError):
    """Tentativa de mover uma unidade de trabalho para um estado inalcancavel."""


class GraphCycle(RegenteError):
    """Dependencias formam ciclo -- nada pode comecar."""


class PolicyDenied(RegenteError):
    """A acao foi barrada pelo Policy Engine. Nao e falha: e o motor funcionando."""

    def __init__(self, reason: str, rule: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.rule = rule


class HumanApprovalRequired(RegenteError):
    """A acao exige decisao humana. Interrompe o agente, nao o motor."""

    def __init__(self, reason: str, rule: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.rule = rule


class CapabilityMissing(RegenteError):
    """Pediram uma capacidade que o cliente nao configurou.

    Erro explicito de proposito: ausencia de adapter nunca pode virar
    'a operacao nao encontrou nada'.
    """


class CorruptedState(RegenteError):
    """O estado persistido nao bate com o que o motor espera."""


class AlreadyExists(RegenteError):
    """Outro processo criou esta entidade primeiro.

    Perder essa corrida nao e erro: a restricao de unicidade fez exatamente o
    que existe para fazer. O motor precisa poder distinguir "colidi com um
    concorrente" de "o banco esta corrompido", porque a resposta certa para o
    primeiro e reler e seguir, e para o segundo e parar.
    """

# -*- coding: utf-8 -*-
"""Erros do dominio. Nenhum deles carrega detalhe de fornecedor."""


class RegenteError(Exception):
    """Raiz. Quem captura Regente captura tudo do motor."""


class TransicaoInvalida(RegenteError):
    """Tentativa de mover uma unidade de trabalho para um estado inalcancavel."""


class CicloNoGrafo(RegenteError):
    """Dependencias formam ciclo -- nada pode comecar."""


class PolicyNegou(RegenteError):
    """A acao foi barrada pelo Policy Engine. Nao e falha: e o motor funcionando."""

    def __init__(self, motivo: str, regra: str | None = None):
        super().__init__(motivo)
        self.motivo = motivo
        self.regra = regra


class PrecisaDeHumano(RegenteError):
    """A acao exige decisao humana. Interrompe o agente, nao o motor."""

    def __init__(self, motivo: str, regra: str | None = None):
        super().__init__(motivo)
        self.motivo = motivo
        self.regra = regra


class CapacidadeAusente(RegenteError):
    """Pediram uma capacidade que o cliente nao configurou.

    Erro explicito de proposito: ausencia de adapter nunca pode virar
    'a operacao nao encontrou nada'.
    """


class EstadoCorrompido(RegenteError):
    """O estado persistido nao bate com o que o motor espera."""

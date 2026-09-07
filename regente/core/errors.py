# -*- coding: utf-8 -*-
"""Domain errors. None of them carries provider-specific detail."""


class RegenteError(Exception):
    """Root. Catching RegenteError catches everything the engine raises."""


class InvalidTransition(RegenteError):
    """Attempt to move a work unit into a state it cannot reach."""


class GraphCycle(RegenteError):
    """Dependencies form a cycle -- nothing can start."""


class PolicyDenied(RegenteError):
    """The action was blocked by the Policy Engine. Not a failure: the engine working."""

    def __init__(self, reason: str, rule: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.rule = rule


class HumanApprovalRequired(RegenteError):
    """The action needs a human decision. It stops the agent, not the engine."""

    def __init__(self, reason: str, rule: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.rule = rule


class CapabilityMissing(RegenteError):
    """Someone asked for a capability the client did not configure.

    Explicit on purpose: a missing adapter must never degrade into
    'the operation found nothing'.
    """


class CorruptedState(RegenteError):
    """The persisted state does not match what the engine expects."""

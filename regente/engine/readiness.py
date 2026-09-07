# -*- coding: utf-8 -*-
"""Turning an adapter's four axes into the engine's six.

The adapter says whether it can run. The engine says whether it may, and whether
there is anything left to spend. Those are three different authorities and this
module is where they meet -- without ever merging.

    adapter -> executable, protocol, authentication, agent
    policy  -> may this workspace run an agent here at all
    budget  -> is there anything left today

An adapter that filled in its own `policy` would be a vendor granting itself
permission, so `AgentAvailability` leaves those two `UNKNOWN` and only this
module supplies them.

The diagnosis is deliberately mechanism-blind. It reports
`BLOCKED_AUTHENTICATION`, never "a named variable is missing" -- because the
second sentence is advice, and it is wrong advice for every client whose agent
authenticates some other way. What is missing is stated by the adapter, in the
adapter's own terms, in the `detail` of the axis that failed.
"""

from __future__ import annotations

from ..core.policy import (Action, AutonomyLevel, Effect, PolicyContext,
                           PolicyEngine)
from ..ports.agent import AgentAvailability, AgentRunner, Check, Readiness

#: The action a workspace needs before an agent may be started at all. Distinct
#: from `repo.commit` and the remote actions: running an agent costs money and
#: touches an isolated area, which is a smaller authority than writing history
#: and a real one nonetheless.
RUN_ACTION = "agent.run"


def diagnose(
    agent: AgentRunner,
    *,
    policy: PolicyEngine | None = None,
    autonomy: AutonomyLevel = AutonomyLevel.L2,
    organization: str = "*",
    client: str = "*",
    workspace: str = "*",
    project: str = "*",
    environment: str = "staging",
    resource: str = "*",
    risk: str = "LOW",
    spent_usd: float = 0.0,
    ceiling_usd: float | None = None,
    dispatches_today: int = 0,
    max_dispatches: int | None = None,
) -> AgentAvailability:
    """Ask the adapter, then add the two axes only the engine may fill."""
    try:
        state = agent.availability()
    except Exception as e:  # noqa: BLE001 - a broken adapter is a diagnosis
        return AgentAvailability(
            adapter=getattr(agent, "name", "?"),
            executable=Check.no(f"the adapter raised while reporting its own "
                                f"availability: {type(e).__name__}: {e}"[:200]))

    return state.with_engine_verdict(
        policy=_policy_check(policy, autonomy, organization, client, workspace,
                             project, environment, resource, risk,
                             getattr(agent, "name", "agent")),
        budget=_budget_check(spent_usd, ceiling_usd, dispatches_today,
                             max_dispatches))


def _policy_check(policy, autonomy, organization, client, workspace, project,
                  environment, resource, risk, agent_name) -> Check:
    if policy is None:
        # No policy configured is not permission. The engine's default is DENY
        # everywhere else and it would be strange -- and dangerous -- for the
        # one place that decides whether to start spending money to be laxer.
        return Check.no("no policy engine is configured for this workspace; "
                        "absence of a rule is not permission")
    decision = policy.decide(PolicyContext(
        action=Action(kind=RUN_ACTION, resource=resource,
                      environment=environment),
        organization=organization, client=client, workspace=workspace,
        project=project, agent=agent_name, risk=risk, autonomy=autonomy))
    if decision.effect == Effect.ALLOW:
        return Check.yes(f"policy ALLOW: {decision.reason}")
    return Check.no(f"policy {decision.effect}: {decision.reason}")


def _budget_check(spent_usd, ceiling_usd, dispatches_today,
                  max_dispatches) -> Check:
    if ceiling_usd is not None and spent_usd >= ceiling_usd:
        return Check.no(f"US$ {spent_usd:.2f} of {ceiling_usd:.2f} spent")
    if max_dispatches is not None and dispatches_today >= max_dispatches:
        return Check.no(f"{dispatches_today} dispatch(es) of {max_dispatches} "
                        f"today")
    parts = []
    if ceiling_usd is not None:
        parts.append(f"US$ {spent_usd:.2f} of {ceiling_usd:.2f}")
    if max_dispatches is not None:
        parts.append(f"{dispatches_today} of {max_dispatches} dispatches")
    return Check.yes(", ".join(parts) or "no ceiling configured")

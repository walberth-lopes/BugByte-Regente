# -*- coding: utf-8 -*-
"""CICDProvider and DeploymentProvider.

The engine reads CI. It does not run it. `run()` stays on the port because the
capability is real and will be needed one day, and stays unimplemented because
triggering a pipeline is a mutation -- and this milestone observes.

The vocabulary here is deliberately thin. The Core knows a check has a name, a
state and a conclusion. It does not know what a workflow, a job, a matrix leg or
a required status check is: those are provider concepts, and letting them in
would mean the Core could only ever speak to one provider.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from . import Capability, Port


@dataclass(frozen=True, slots=True)
class Check:
    """One verification the provider ran, or is running."""
    name: str
    conclusion: str = ""   # SUCCESS | FAILURE | CANCELLED | TIMED_OUT | ...
    state: str = ""        # QUEUED | IN_PROGRESS | COMPLETED
    url: str = ""

    @property
    def running(self) -> bool:
        return self.state in {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED"}

    @property
    def green(self) -> bool:
        """Passed, or passed by not applying.

        `SKIPPED` and `NEUTRAL` count as green because a check that decided it
        had nothing to do has not failed. Counting them red would make every
        conditional job look like a defect.
        """
        return self.conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}

    @property
    def red(self) -> bool:
        return bool(self.conclusion) and not self.green and not self.running


@dataclass(frozen=True, slots=True)
class PipelineStatus:
    """What the provider says about the checks for one reference."""
    reference: str
    checks: tuple[Check, ...] = ()
    url: str = ""
    #: True only when the provider CONFIRMED there is no check at all.
    #:
    #: An empty list caused by a failed read is an `AdapterError`, never this.
    #: The distinction is the whole point: "there is nothing to verify" and "I
    #: could not find out" lead to opposite decisions, and conflating them lets
    #: a network blip read as a clean bill of health.
    confirmed_no_checks: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def running(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.checks if c.running)

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.checks if c.red)

    @property
    def finished(self) -> bool:
        return bool(self.checks) and not self.running

    @property
    def all_green(self) -> bool:
        return bool(self.checks) and all(c.green for c in self.checks)


class CICDProvider(Port):
    capability = Capability.CICD

    @abstractmethod
    def get_status(self, repo: str, reference: str) -> PipelineStatus:
        """Checks for a SHA or a pull request. Never invents an empty result.

        A provider that cannot answer raises. Returning "no checks" because the
        request failed is the one bug this port exists to make impossible.
        """

    def get_logs(self, repo: str, check: str, limit: int = 200) -> list[str]:
        return []

    # ---- mutation: declared, not implemented ----------------------------

    def run(self, repo: str, pipeline: str,
            parameters: dict[str, Any] | None = None) -> str:
        """Trigger a pipeline. Out of scope while the engine only observes."""
        raise NotImplementedError

    def cancel(self, repo: str, run_id: str) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Deployment:
    id: str
    environment: str
    version: str
    state: str = "IN_PROGRESS"
    url: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class DeploymentProvider(Port):
    capability = Capability.DEPLOYMENT

    def get_deployment(self, deployment_id: str) -> Deployment:
        raise NotImplementedError

    def deploy_staging(self, project: str, version: str) -> Deployment:
        raise NotImplementedError

    def deploy_production(self, project: str, version: str) -> Deployment:
        """Separate from staging on the port, on purpose.

        A single `deploy(environment)` would make the difference between staging
        and production the value of a string carried in context -- exactly the
        kind of field an injected prompt can reach. They are distinct methods,
        with distinct policy actions and distinct autonomy levels.
        """
        raise NotImplementedError

    def rollback(self, project: str, environment: str,
                 to_version: str | None = None) -> Deployment:
        raise NotImplementedError

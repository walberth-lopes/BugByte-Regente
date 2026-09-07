# -*- coding: utf-8 -*-
"""SecretProvider: resolves secret REFERENCES, never stores values.

The configuration says `token: env:JIRA_API_TOKEN`. What travels through the
YAML, the database, the events and the prompts is the *reference* -- the value
only exists at the moment of use, inside the adapter that needs it.

**The reference is scoped by tenancy.** A workspace only reaches the references
its own configuration declares, and the resolver refuses any other. Without
that, a misconfigured adapter belonging to client B would read client A's
credential -- and the worst part is that it would work, silently, until the day
it turned up in a log.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..ports import AdapterError
from ..ports.support import SecretProvider


class SecretMissing(AdapterError):
    """The reference exists in the configuration but resolves to nothing."""


class SecretOutOfScope(AdapterError):
    """Something asked for a reference this workspace did not declare. Never a
    benign mistake: it is the boundary between clients being probed."""


@dataclass(slots=True)
class ScopedSecrets(SecretProvider):
    """Resolves `env:NAME` and `file:PATH`.

    There is deliberately no `literal:` form. If there were, the first production
    secret would show up in a versioned YAML inside a week.

    """
    name: str = "scoped"
    #: References THIS workspace may resolve. Empty = none.
    allowed_from: frozenset[str] = field(default_factory=frozenset)
    workspace: str = "?"

    def resolve(self, reference: str) -> str:
        if reference not in self.allowed_from:
            raise SecretOutOfScope(
                f"workspace '{self.workspace}' did not declare the reference "
                f"{reference!r}; declared: {sorted(self.allowed_from) or 'none'}")

        scheme, _, resto = reference.partition(":")
        if scheme == "env":
            value = os.environ.get(resto, "")
            if not value:
                raise SecretMissing(
                    f"environment variable {resto} is not set or is empty")
            return value
        if scheme == "file":
            path = Path(resto).expanduser()
            if not path.is_file():
                raise SecretMissing(f"secret file does not exist: {path}")
            value = path.read_text(encoding="utf-8").strip()
            if not value:
                raise SecretMissing(f"secret file is empty: {path}")
            return value
        raise SecretMissing(
            f"unknown reference scheme: {scheme!r}. Use env: or file:")

    def available(self, reference: str) -> bool:
        try:
            self.resolve(reference)
            return True
        except AdapterError:
            return False

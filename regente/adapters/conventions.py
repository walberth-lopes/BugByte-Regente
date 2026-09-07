# -*- coding: utf-8 -*-
"""Vendor-named facts about repositories, kept out of the engine.

Three lists, one idea: the engine knows the *concept* -- a file that confers
authority, a file where a team writes down how its repository works -- and
receives the concrete names from here. Every entry is somebody's product
convention, and a rule that grows a line each time a client picks a different CI
provider or a different coding agent does not belong above the adapter line.

Both lists arrived here the same way: the boundary test caught them sitting in
`engine/`. The second time it did not catch them, which was the more useful
finding -- see `FORNECEDORES` in the boundary test, which had no coding-agent
vendor in it because coding agents were not a vendor category until this
milestone.
"""

from __future__ import annotations

#: Paths that decide what runs on a server. Editing one is an escalation
#: attempt whatever the task said.
CI_AUTHORITY_PATHS: tuple[str, ...] = (
    ".github/workflows",
    ".gitlab-ci.yml",
    ".circleci",
    "Jenkinsfile",
    "azure-pipelines.yml",
    "bitbucket-pipelines.yml",
    ".buildkite",
    "cloudbuild.yaml",
)

#: Paths that decide where code and credentials go.
DEPLOYMENT_AUTHORITY_PATHS: tuple[str, ...] = (
    "Dockerfile", "docker-compose.yml", "skaffold.yaml", "app.yaml",
    "serverless.yml", "terraform", "helm", ".npmrc", ".pypirc",
)

#: Files where a team writes down how its repository works, best first.
#: Several are one coding agent's convention rather than a general one, which is
#: exactly why the list lives here and not in the Context Engine.
INSTRUCTION_FILES: tuple[str, ...] = (
    "AGENTS.md", "CLAUDE.md", "CONVENTIONS.md", "CONTRIBUTING.md",
    "ARCHITECTURE.md", "README.md",
)


def default_authority_paths() -> tuple[str, ...]:
    """Everything guarded unless a workspace says otherwise.

    Deployment paths are included: a task that legitimately needs to change one
    is a task that legitimately needs a person to look, and the cost of that is
    one review. The cost of the opposite default is an agent that rewrites a
    Dockerfile and nobody notices until it ships.
    """
    return CI_AUTHORITY_PATHS + DEPLOYMENT_AUTHORITY_PATHS

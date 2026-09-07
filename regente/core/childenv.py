# -*- coding: utf-8 -*-
"""Building the environment a child process is allowed to see.

Pure: the caller passes the source mapping in, so nothing here touches the real
environment and `core/` stays free of I/O.

It lives in `core/` because two very different callers need the identical rule
and must not drift apart -- the agent's process, and the engine's own test run.
The second one is the interesting case, and it was found by an adversarial audit
rather than by design.

**The verification step is a way to run code.** The agent is given no tool that
runs commands, which closes push, pull-request creation and deploy in one move.
But the agent can write files, tests are files, and the engine runs the test
suite itself to reach a verdict. So an agent that cannot execute anything can
still write `test_anything.py` and have the engine execute it -- with whatever
environment the engine happened to be holding.

Writing tests is work we want from an agent, so refusing to run agent-authored
tests would gut the loop. What we can refuse is to hand that code a wallet: the
suite runs with a composed environment like everything else, and a repository
whose tests genuinely need a credential declares it, per workspace, the way this
project declares every other secret. Absence of configuration makes a thing
impossible; it never makes it accidental.
"""

from __future__ import annotations

from typing import Mapping

#: Variables a child may see. Enough to start a process and find its tools;
#: nothing that identifies anybody to anything.
BASE_ALLOWLIST: tuple[str, ...] = (
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "HOME",
    "USERPROFILE", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "PATHEXT",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
)

#: Additional variables a test runner needs to behave like the developer's own.
#: Deliberately separate from `BASE_ALLOWLIST`: they are a concession to one
#: caller, and a reader should see which caller asked for them.
TEST_RUNNER_ALLOWLIST: tuple[str, ...] = (
    "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "PYTHONIOENCODING",
    "PYTHONUTF8", "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX",
    "CI", "TERM", "COLUMNS", "NODE_PATH", "GOPATH", "GOCACHE", "JAVA_HOME",
    "GRADLE_USER_HOME", "M2_HOME",
)

#: What a tool that talks to the network needs to keep working behind a
#: corporate perimeter. Separate from `BASE_ALLOWLIST` for the same reason as
#: the test-runner list: a reader should see which caller asked for them.
#:
#: A proxy URL can itself carry userinfo. That is the operator's own network
#: credential, not the provider's, and withholding it would break every client
#: behind a proxy -- so it passes, and this comment is where that is admitted
#: rather than discovered.
#:
#: `SSL_CERT_FILE` and `SSL_CERT_DIR` are deliberately NOT here: "CERT" is a
#: credential mark, so `compose` would drop them anyway and the entries would be
#: decoration. A workspace that needs a custom CA bundle states it as a fixed
#: variable of its adapter, where the choice is visible.
NETWORK_ALLOWLIST: tuple[str, ...] = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
)

#: Substrings that mark a name as credential-shaped. A second layer under the
#: allowlist, because an allowlist is edited by people and this catches the edit
#: that was made in a hurry.
CREDENTIAL_MARKS: tuple[str, ...] = (
    "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "API_KEY", "APIKEY",
    "PRIVATE", "SESSION", "COOKIE", "AUTH", "LICENSE", "SIGNING", "CERT",
)


def looks_credential(name: str) -> bool:
    upper = name.upper()
    return any(mark in upper for mark in CREDENTIAL_MARKS)


def compose(source: Mapping[str, str],
            allow: tuple[str, ...] = BASE_ALLOWLIST,
            extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Start from nothing and add only what is named.

    Starting from `{}` rather than from a copy of `source` is the whole point.
    An allowlist applied to a copy is one forgotten deletion away from leaking;
    an environment built up from empty cannot leak what was never put in it.

    `extra` is not filtered: it is what the caller deliberately resolved and
    chose to pass -- a credential the engine resolved for this workspace, for
    instance. Filtering it would make the resolved path impossible.
    """
    env: dict[str, str] = {}
    for name in allow:
        if looks_credential(name):
            continue
        value = source.get(name)
        if value is not None:
            env[name] = value
    for name, value in (extra or {}).items():
        env[str(name)] = str(value)
    return env

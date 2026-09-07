# -*- coding: utf-8 -*-
"""What counts as a read, per INVOCATION -- never per verb.

This module exists because of a defect measured on 06/09/2026, and the lesson
holds for any future adapter that spawns an external process:

    Allowing the verb `repo` let `repo delete` through.
    Allowing the verb `remote` would let `remote set-url` through.
    Allowing `symbolic-ref` would let the TWO-argument form, which writes, through.

An allowlist at the wrong granularity is worse than none: it produces confidence
without producing a guarantee. The only honest form is to list **whole
invocations** and refuse everything that does not match exactly -- including new
forms of the same verb that may appear in a future version of the tool.

The golden rule of this file: when in doubt, refuse. A refused read command costs
a readable `AdapterError`; an accepted write command costs a repository.
"""

from __future__ import annotations

#: Git subcommands NO form of which mutates the repository.
#: Checked one by one: there is no write variant among these.
GIT_ALWAYS_READ: frozenset[str] = frozenset({
    "rev-parse", "for-each-ref", "log", "ls-tree", "cat-file", "show", "status",
})

#: Flags that write, in any git subcommand.
GIT_WRITE_FLAGS: frozenset[str] = frozenset({
    "-d", "-D", "--delete", "--unset", "--unset-all", "--add", "--replace-all",
    "--edit", "-w", "--write", "--force", "-f", "--set", "--short-hand-set",
})

#: Flags that make the hosting CLI send a POST even without `--method`.
#: This is the trap: `api -f key=value route` is NOT a read.
CLI_WRITE_FLAGS: frozenset[str] = frozenset({
    "-f", "-F", "--field", "--raw-field", "--input", "--method", "-X",
})

#: Invocations allowed on the hosting CLI, by (command, subcommand) pair.
#: `api` enters with subcommand None because the route is free-form -- which is
#: why it gets an extra method check.
CLI_READ_INVOCATIONS: frozenset[tuple[str, str | None]] = frozenset({
    ("repo", "list"), ("repo", "view"),
    ("pr", "list"), ("pr", "view"), ("pr", "diff"), ("pr", "checks"),
    ("auth", "status"),
    ("api", None),
})


def git_is_read(args: list[str] | tuple[str, ...]) -> tuple[bool, str]:
    """Returns (allowed, reason for refusal)."""
    if not args:
        return False, "empty invocation"
    cmd, resto = args[0], list(args[1:])

    forbidden = [a for a in resto if a in GIT_WRITE_FLAGS]
    if forbidden:
        return False, f"write flag: {', '.join(forbidden)}"

    if cmd in GIT_ALWAYS_READ:
        return True, ""

    if cmd == "remote":
        # `get-url` reads; `add`, `remove`, `set-url`, `rename`, `prune` write.
        if resto[:1] == ["get-url"]:
            return True, ""
        return False, "only 'remote get-url' is a read"

    if cmd == "symbolic-ref":
        # One ref = a read. Two = it writes the ref. The difference is only the
        # arity, which is why an allowlist by verb could not have caught it.
        refs = [a for a in resto if not a.startswith("-")]
        if len(refs) != 1:
            return False, f"symbolic-ref with {len(refs)} refs writes; a read has 1"
        return True, ""

    if cmd == "config":
        # `--get` and `--list` read; any other form writes.
        if any(a in ("--get", "--get-all", "--list", "-l") for a in resto):
            return True, ""
        return False, "only 'config --get/--list' is a read"

    if cmd == "branch":
        if any(a in ("--list", "-l", "--show-current") for a in resto):
            return True, ""
        return False, "only 'branch --list/--show-current' is a read"

    return False, f"'git {cmd}' is not on the read list"


def cli_is_read(args: list[str] | tuple[str, ...]) -> tuple[bool, str]:
    """Returns (allowed, reason for refusal)."""
    if not args:
        return False, "empty invocation"
    cmd = args[0]
    sub = args[1] if len(args) > 1 and not args[1].startswith("-") else None

    if cmd == "api":
        if ("api", None) not in CLI_READ_INVOCATIONS:
            return False, "'api' is not allowed"
        for i, a in enumerate(args):
            base = a.split("=", 1)[0]
            if base not in CLI_WRITE_FLAGS:
                continue
            if base in ("--method", "-X"):
                method = (a.split("=", 1)[1] if "=" in a
                          else (args[i + 1] if i + 1 < len(args) else "")).upper()
                if method and method != "GET":
                    return False, f"method {method} is not a read"
                continue
            # -f/-F/--input make the CLI send a POST even without --method.
            return False, f"'{base}' turns the call into a write"
        return True, ""

    if (cmd, sub) in CLI_READ_INVOCATIONS:
        return True, ""
    return False, f"'{(cmd + ' ' + (sub or '')).strip()}' is not on the read list"

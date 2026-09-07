# -*- coding: utf-8 -*-
"""TaskProvider for Jira Cloud. READ ONLY.

This file is the only one in the engine that knows what an `issuelink`, a
`parent`, a `statusCategory` or an ADF is. None of that crosses the port.

**Operational authority: none.** The port's write methods raise
`ReadOnlyRefused`. And the real guarantee is not here but in the transport, which
only has `get` -- this adapter could not mutate Jira even if the code tried.

Three facts about the real Jira the design has to respect, all measured on
06/09/2026 against the site in use:

1. **A response blows the context.** A search of 100 issues with the necessary
   fields returned 726 KB. That is why the field list is always trimmed and
   explicit: asking for `*all` is like asking for the whole database.
2. **Pagination is by cursor**, `nextPageToken`, not `startAt`. Paginating wrong
   returns the first page for ever -- and the board looks like it has 100 items.
3. **Hierarchy dominates the board.** 95 of 100 issues had a `parent` and there
   were 118 `Relates` links against 28 `Blocks`. Treating hierarchy or
   relatedness as a dependency would lock everything up; only `Blocks` becomes
   execution order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ...ports import AdapterError, ReadOnlyRefused
from ...ports.tasks import (BLOCKS, DUPLICATES, PARENT, RELATED, Comment, ExternalTask,
                            ExternalStatus, TaskProvider, TaskRef)
from .transport import Transport

# ---------------------------------------------------------------------------
# MAPPING -- external -> internal. Every deviation is declared here.
# ---------------------------------------------------------------------------

#: EXTERNAL STATUS -> INTERNAL STATUS.
#:
#: The names were read from the real board, not assumed. A status outside this
#: map is NOT coerced into the neighbouring one: it becomes UNKNOWN and
#: rises as an anomaly. A new status means somebody changed the process, and the
#: engine has to say so instead of pretending it understood. Keys are Jira status
#: names and stay as they are.
STATUS_MAP: dict[str, ExternalStatus] = {
    "TO DO": ExternalStatus.NOT_STARTED,
    "BACKLOG": ExternalStatus.NOT_STARTED,
    "PLANNING": ExternalStatus.IN_ANALYSIS,
    "CODING": ExternalStatus.IN_PROGRESS,
    "IN PROGRESS": ExternalStatus.IN_PROGRESS,
    "REVIEWING": ExternalStatus.IN_REVIEW,
    "IN REVIEW": ExternalStatus.IN_REVIEW,
    "QA STAGING": ExternalStatus.IN_VALIDATION,
    "QA PRODUCTION": ExternalStatus.IN_VALIDATION,
    "DONE": ExternalStatus.COMPLETED,
    "CANCELLED": ExternalStatus.CANCELLED,
    "WON'T DO": ExternalStatus.CANCELLED,
}

#: Safety net by status category. Jira classifies every status as
#: `new` / `indeterminate` / `done`, and that classification exists even for
#: statuses nobody mapped. Using it for `done` avoids the worst possible error --
#: dispatching work that is already finished -- without faking precision on the
#: rest: `indeterminate` stays UNKNOWN, because "it is in the middle" does
#: not say whether that is code, review or validation, and guessing is worse than
#: admitting the ignorance.
CATEGORY_MAP: dict[str, ExternalStatus] = {
    "new": ExternalStatus.NOT_STARTED,
    "done": ExternalStatus.COMPLETED,
}

#: EXTERNAL PRIORITY -> INTERNAL PRIORITY (lower runs first).
#: Spaced 20 apart on purpose: it leaves room for a planner to adjust without
#: colliding with the value coming from the source.
PRIORITY_MAP: dict[str, int] = {
    "HIGHEST": 10, "BLOCKER": 10, "CRITICAL": 10,
    "HIGH": 30, "MAJOR": 30,
    "MEDIUM": 50, "NORMAL": 50,
    "LOW": 70, "MINOR": 70,
    "LOWEST": 90, "TRIVIAL": 90,
}
DEFAULT_PRIORITY = 50

#: EXTERNAL LINK TYPE -> INTERNAL TYPE.
#:
#: The direction matters and is easy to invert. In Jira the link lives on issue A
#: with one end: `inwardIssue` means "A <inward> B". For `Blocks`, inward is "is
#: blocked by" -- that is, **A depends on B**. Outward is "blocks": B depends on
#: A, and the correct record belongs on the other issue, which will have its own
#: inward. Recording both sides as blocking would invert half the graph.
LINK_MAP: dict[str, str] = {
    "Blocks": BLOCKS,
    "Duplicate": DUPLICATES,
    "Relates": RELATED,
    "Cloners": RELATED,
    "Problem/Incident": RELATED,
}

#: Fields for the LISTING. Trimmed out of necessity: see fact 1 at the top of the
#: file. `description` is left out on purpose -- it is an ADF tree, and a hundred
#: of them turn a routine search into a transfer of megabytes.
LIST_FIELDS = ("summary", "status", "issuetype", "priority", "labels", "parent",
                "issuelinks", "assignee", "project", "updated")

#: Fields for the DETAIL. Paid for one issue at a time, when somebody is actually
#: going to work on it. That is the only moment the full description is worth the
#: cost.
DETAIL_FIELDS = LIST_FIELDS + ("description",)


def _text_from(content: Any, limit: int = 4000) -> str:
    """A Jira description may arrive as text or as ADF (a JSON tree).

    Extracts readable text from both cases. It does not try to reconstruct
    formatting: what the engine needs is the content, and ADF rendered by regex
    turns into noise.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:limit]
    pedacos: list[str] = []

    def anda(no: Any) -> None:
        if len(" ".join(pedacos)) > limit:
            return
        if isinstance(no, dict):
            if no.get("type") == "text" and isinstance(no.get("text"), str):
                pedacos.append(no["text"])
            for filho in (no.get("content") or []):
                anda(filho)
        elif isinstance(no, list):
            for filho in no:
                anda(filho)

    anda(content)
    return " ".join(pedacos)[:limit]


@dataclass(slots=True)
class JiraTasks(TaskProvider):
    """Reads work from a Jira Cloud site. Never writes."""

    transport: Transport
    #: The JQL defining what this workspace considers its own work. It comes from
    #: the configuration: the only place where the notion of "relevant" is declared.
    jql: str = "statusCategory != Done ORDER BY updated DESC"
    #: Where the resource key used by the scheduler comes from. See `_resources_of`.
    #: Its values are "parent", "project" and "none".
    resources_by: str = "parent"
    #: Page cap. It exists so that a loose JQL does not turn into a sweep of an
    #: entire board in one tick.
    max_pages: int = 10
    per_page: int = 100
    site: str = ""
    name: str = "jira"

    def describe(self) -> dict[str, str]:
        return {"capability": self.capability.value, "adapter": self.name,
                "mode": "read-only", "site": self.site}

    def verify(self) -> None:
        """Proves the credential and the reach with the cheapest call there is."""
        try:
            self.transport.get("/rest/api/3/myself", {"expand": ""})
        except AdapterError:
            raise
        except Exception as e:  # noqa: BLE001
            raise AdapterError(f"transport failed: {type(e).__name__}: {e}") from e

    # ---- reading ---------------------------------------------------------

    def list_tasks(self, filters: dict[str, Any] | None = None) -> list[ExternalTask]:
        f = filters or {}
        jql = f.get("jql") or self.jql
        if f.get("mine_only"):
            jql = f"assignee = currentUser() AND ({jql})"

        items: list[ExternalTask] = []
        cursor: str | None = None
        for _ in range(self.max_pages):
            body = self.transport.get("/rest/api/3/search/jql", {
                "jql": jql,
                "fields": ",".join(LIST_FIELDS),
                "maxResults": self.per_page,
                "nextPageToken": cursor,
            })
            if not isinstance(body, dict):
                raise AdapterError(f"search returned {type(body).__name__}, expected an object")
            for raw in (body.get("issues") or []):
                items.append(self._normalize(raw, partial=True))
            cursor = body.get("nextPageToken")
            # `isLast` does not always arrive; the absence of a cursor is the reliable signal.
            if not cursor:
                break
        return items

    def get_task(self, key: str) -> ExternalTask:
        body = self.transport.get(f"/rest/api/3/issue/{key}",
                                    {"fields": ",".join(DETAIL_FIELDS)})
        if not isinstance(body, dict) or "fields" not in body:
            raise AdapterError(f"issue {key} arrived without 'fields'")
        return self._normalize(body)

    def get_comments(self, key: str) -> list[Comment]:
        body = self.transport.get(f"/rest/api/3/issue/{key}/comment",
                                    {"maxResults": 50, "orderBy": "created"})
        output = []
        for c in (body.get("comments") or []):
            output.append(Comment(
                author=((c.get("author") or {}).get("displayName") or "?"),
                text=_text_from(c.get("body")),
                created_at=str(c.get("created") or ""),
                id=str(c.get("id") or "")))
        return output

    # ---- writing: refused ------------------------------------------------
    # These are not `NotImplementedError`. They are explicit refusals, so that a
    # caller who tries to write gets the reason -- and so that the intent is
    # readable in this file, and not only in the policy file.

    def _refuse(self, operation: str) -> None:
        raise ReadOnlyRefused(
            f"{self.name} is mounted read-only; '{operation}' is not executable "
            f"in this milestone. No mutation of the external system.")

    def update_task(self, key: str, fields: dict[str, Any]) -> None:
        self._refuse("update_task")

    def transition_task(self, key: str, destination: str) -> None:
        self._refuse("transition_task")

    def add_comment(self, key: str, text: str) -> None:
        self._refuse("add_comment")

    def add_label(self, key: str, label: str) -> None:
        self._refuse("add_label")

    # ---- normalisation ---------------------------------------------------

    def _normalize(self, raw: dict[str, Any], partial: bool = False) -> ExternalTask:
        fields = raw.get("fields") or {}
        key = str(raw.get("key") or "")
        if not key:
            raise AdapterError("issue without a 'key' -- impossible to give it an identity")

        status = fields.get("status") or {}
        status_name = str(status.get("name") or "")
        category = str((status.get("statusCategory") or {}).get("key") or "")
        status = STATUS_MAP.get(status_name.strip().upper())
        if status is None:
            status = CATEGORY_MAP.get(category, ExternalStatus.UNKNOWN)

        priority_name = str((fields.get("priority") or {}).get("name") or "")
        priority = PRIORITY_MAP.get(priority_name.strip().upper(), DEFAULT_PRIORITY)

        links = self._links_of(fields)
        labels = tuple(str(x) for x in (fields.get("labels") or []))

        return ExternalTask(
            key=key,
            title=str(fields.get("summary") or ""),
            status=status,
            external_status=status_name,
            description=_text_from(fields.get("description")),
            url=f"{self.site.rstrip('/')}/browse/{key}" if self.site else None,
            priority=priority,
            project=str((fields.get("project") or {}).get("key") or ""),
            assignee=((fields.get("assignee") or {}).get("displayName") or None),
            links=links,
            resources=self._resources_of(key, fields),
            labels=labels,
            partial=partial,
            data={
                "tipo": str((fields.get("issuetype") or {}).get("name") or ""),
                "subtarefa": bool((fields.get("issuetype") or {}).get("subtask")),
                "categoria_status": category,
                "atualizada_em": str(fields.get("updated") or ""),
                "prioridade_externa": priority_name,
            })

    def _links_of(self, fields: dict[str, Any]) -> tuple[TaskRef, ...]:
        output: list[TaskRef] = []

        pai = fields.get("parent") or {}
        if pai.get("key"):
            # Hierarchy, not order: the subtask does NOT wait for its parent to finish.
            output.append(TaskRef(key=str(pai["key"]), kind=PARENT))

        for link in (fields.get("issuelinks") or []):
            external_kind = str((link.get("type") or {}).get("name") or "")
            kind = LINK_MAP.get(external_kind, RELATED)
            inward, outward = link.get("inwardIssue"), link.get("outwardIssue")
            if inward and inward.get("key"):
                # "this issue <inward> that one". For Blocks: "is blocked by".
                output.append(TaskRef(key=str(inward["key"]), kind=kind))
            elif outward and outward.get("key"):
                # "this issue <outward> that one". For Blocks: "blocks" -- the one
                # that depends is the OTHER issue, and it will record its own
                # inward. Recording it as blocking here would invert the edge.
                output.append(TaskRef(key=str(outward["key"]),
                                     kind=RELATED if kind == BLOCKS else kind))
        return tuple(output)

    def _resources_of(self, key: str, fields: dict[str, Any]) -> tuple[str, ...]:
        """Mutual-exclusion key for the scheduler.

        A task provider does not know which files will be touched -- inventing
        that would be exactly the kind of patch that corrupts the engine. What it
        does know is hierarchy, and hierarchy is a real signal: two subtasks of
        the same parent almost always touch the same code.

        `parent` (default): serialises siblings, parallelises different parents.
        `project`: serialises the whole project -- conservative, almost no gain.
        `none`:    declares no conflict; only for those who enrich this later.

        Real precision comes from an analysis agent reading the code. Until then
        this is a declared heuristic -- and it says so in writing.
        """
        if self.resources_by == "none":
            return ()
        if self.resources_by == "project":
            return (f"project:{(fields.get('project') or {}).get('key') or '?'}",)
        pai = (fields.get("parent") or {}).get("key")
        return (f"parent:{pai}",) if pai else (f"issue:{key}",)

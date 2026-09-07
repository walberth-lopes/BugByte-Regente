# -*- coding: utf-8 -*-
"""Notification to the terminal and to the logbook file.

The file matters more than it looks: a terminal notification is lost when nobody
is watching, and the NEEDS ME queue exists precisely for the moments when the
owner is not watching. The journal is what survives.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from ...ports.support import NotificationProvider


class Console(NotificationProvider):
    name = "console"

    def __init__(self, journal: str | Path | None = None):
        self.journal = Path(journal) if journal else None

    def notify(self, title: str, body: str, urgency: str = "normal",
               link: str | None = None) -> None:
        # The urgency keys stay in Portuguese: they are the port's vocabulary.
        mark = {"alta": "!!", "normal": " *", "baixa": "  "}.get(urgency, " *")
        line = f"{mark} {title} -- {body}"
        # `errors='replace'` because the Windows console is not UTF-8 by default
        # and a title with an accent must not bring the tick down.
        try:
            print(line, file=sys.stderr)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"), file=sys.stderr)
        if self.journal:
            carimbo = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with self.journal.open("a", encoding="utf-8") as f:
                f.write(f"{carimbo} | {urgency} | {title} | {body}"
                        + (f" | {link}" if link else "") + "\n")

# -*- coding: utf-8 -*-
"""Notificacao no terminal e no arquivo de bordo.

O arquivo importa mais do que parece: notificacao de terminal se perde quando
ninguem esta olhando, e a fila NEEDS ME existe justamente para os momentos em que
o dono nao esta olhando. O jornal e o que sobrevive.
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
        mark = {"alta": "!!", "normal": " *", "baixa": "  "}.get(urgency, " *")
        linha = f"{mark} {title} -- {body}"
        # `errors='replace'` porque o console do Windows nao e UTF-8 por padrao
        # e um titulo com acento nao pode derrubar o tick.
        try:
            print(linha, file=sys.stderr)
        except UnicodeEncodeError:
            print(linha.encode("ascii", "replace").decode("ascii"), file=sys.stderr)
        if self.journal:
            carimbo = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with self.journal.open("a", encoding="utf-8") as f:
                f.write(f"{carimbo} | {urgency} | {title} | {body}"
                        + (f" | {link}" if link else "") + "\n")

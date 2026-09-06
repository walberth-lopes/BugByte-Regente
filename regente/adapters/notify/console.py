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
    nome = "console"

    def __init__(self, jornal: str | Path | None = None):
        self.jornal = Path(jornal) if jornal else None

    def notify(self, titulo: str, corpo: str, urgencia: str = "normal",
               link: str | None = None) -> None:
        marca = {"alta": "!!", "normal": " *", "baixa": "  "}.get(urgencia, " *")
        linha = f"{marca} {titulo} -- {corpo}"
        # `errors='replace'` porque o console do Windows nao e UTF-8 por padrao
        # e um titulo com acento nao pode derrubar o tick.
        try:
            print(linha, file=sys.stderr)
        except UnicodeEncodeError:
            print(linha.encode("ascii", "replace").decode("ascii"), file=sys.stderr)
        if self.jornal:
            carimbo = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.jornal.parent.mkdir(parents=True, exist_ok=True)
            with self.jornal.open("a", encoding="utf-8") as f:
                f.write(f"{carimbo} | {urgencia} | {titulo} | {corpo}"
                        + (f" | {link}" if link else "") + "\n")

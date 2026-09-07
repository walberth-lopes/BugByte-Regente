# -*- coding: utf-8 -*-
"""Um processo que tenta decidir uma escalada. Usado aos pares.

Existe como arquivo separado porque a prova precisa de processos de verdade: a
guarda que impede duas decisoes esta dentro da transacao do SQLite, e uma prova
feita com threads poderia passar por um detalhe do interpretador em vez de pela
transacao -- ficando verde pelo motivo errado.

    python tests/decision_race.py <raiz> <approval_id> <escolha>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from regente.core.policy import PolicyEngine          # noqa: E402
from regente.core.principal import Principal          # noqa: E402
from regente.engine.decision import DECIDE_ACTION, DecisionService  # noqa: E402
from regente.engine.store_sqlite import SqliteStore   # noqa: E402


def main() -> int:
    root, approval_id, choice = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    store = SqliteStore(root / "race.db")
    store.migrate()
    try:
        service = DecisionService(
            store=store,
            policy=PolicyEngine.from_config([
                {"name": "decidir", "effect": "ALLOW",
                 "match": {"action": DECIDE_ACTION}}]),
            organization="org", client="Acme", workspace_name="main")
        who = Principal(subject=f"operador-{choice}", method="dev-token",
                        workspaces=frozenset({"wks_a"}),
                        decides=frozenset({"wks_a"}))
        outcome = service.decide(who, "wks_a", approval_id, choice)
        print(json.dumps({
            "accepted": outcome.accepted,
            "denial": outcome.denial.value if outcome.denial else None,
            "choice": outcome.choice,
            "reason": outcome.reason,
        }), flush=True)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

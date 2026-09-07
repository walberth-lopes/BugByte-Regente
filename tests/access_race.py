# -*- coding: utf-8 -*-
"""Um processo que tenta conceder acesso. Usado aos pares.

Arquivo separado porque a prova precisa de processos de verdade: a trava que
impede duas concessoes vivas para a mesma pessoa e o indice unico dentro da
transacao do SQLite. Uma prova feita com threads poderia passar por um detalhe
do interpretador em vez de pelo banco -- ficando verde pelo motivo errado.

    python tests/access_race.py <raiz> <sujeito-do-ator> <papel>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from regente.core.access import PrincipalRef                  # noqa: E402
from regente.core.policy import PolicyEngine                  # noqa: E402
from regente.core.principal import Principal                  # noqa: E402
from regente.engine.access import AccessService               # noqa: E402
from regente.engine.store_sqlite import SqliteStore           # noqa: E402

ALVO = PrincipalRef("os-account", "S-1-5-21-2")


def main() -> int:
    root, sujeito, papel = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    store = SqliteStore(root / "race.db")
    store.migrate()
    try:
        service = AccessService(
            store=store,
            policy=PolicyEngine.from_config([
                {"name": "acesso", "effect": "ALLOW",
                 "match": {"action": "workspace.access.grant"}}]),
            organization="org", client="Acme", workspace_name="main")

        ator = service.authorize(Principal(
            subject=sujeito, display=sujeito, method="os-account",
            provider="os-account", issuer="maquina"))
        saida = service.grant(ator, "wks_a", ALVO, papel)

        print(json.dumps({
            "accepted": saida.accepted,
            "refusal": saida.refusal.value if saida.refusal else None,
            "actor": saida.actor,
            "target": saida.target,
        }), flush=True)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

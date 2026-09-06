# -*- coding: utf-8 -*-
"""Store em SQLite. Unico modulo do motor que fala SQL.

Escolhas que nao sao detalhe:

- **WAL + `BEGIN IMMEDIATE`.** Um tick pode rodar enquanto a UI le. WAL permite
  isso; `IMMEDIATE` na escrita garante que duas transicoes concorrentes da mesma
  task nao se sobreponham -- e a transicao e onde um despacho duplicado nasce.
- **Datas em texto ISO-8601 UTC.** SQLite nao tem kind de data. Guardar epoch
  economiza nada e torna o banco ilegivel num momento em que ler o banco a mao e
  exatamente o que se precisa fazer.
- **`estado` guardado como texto do Enum.** Migrar um Enum e trivial; migrar um
  inteiro cujo significado mudou e um day perdido.
- **Evento nunca e apagado nem editado.** Estado e projecao; evento e o que
  aconteceu. `recupera()` reconstroi a partir dos dois.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from ..core import ids
from ..core.errors import CorruptedState
from ..core.model import (ActionRecord, Approval, ApprovalState, Dependency, Event,
                          ExternalRef, Lease, Option, Project, Repository, Run,
                          RunState, Task, Workspace, agora)
from ..core.policy import AutonomyLevel
from ..core.risk import RiskLevel
from ..core.states import TaskState, require
from ..ports.store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY, client_id TEXT NOT NULL, name TEXT NOT NULL,
  autonomy INTEGER NOT NULL DEFAULT 2, root TEXT);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, name TEXT NOT NULL,
  default_environment TEXT NOT NULL DEFAULT 'staging', autonomy INTEGER);

CREATE TABLE IF NOT EXISTS repositories (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
  base_branch TEXT NOT NULL DEFAULT 'main', url TEXT);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, project_id TEXT NOT NULL,
  title TEXT NOT NULL, state TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  provider TEXT, external_key TEXT, url TEXT,
  priority INTEGER NOT NULL DEFAULT 100, risk TEXT, paused_at TEXT,
  resources TEXT NOT NULL DEFAULT '[]', attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '{}');
-- Identidade externa e unica por workspace: o mesmo FAXINA-183 em dois clientes
-- sao duas tasks distintas, e nunca podem colidir.
CREATE UNIQUE INDEX IF NOT EXISTS ix_tasks_externa
  ON tasks(workspace_id, provider, external_key)
  WHERE provider IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_tasks_estado ON tasks(workspace_id, state);

CREATE TABLE IF NOT EXISTS deps (
  task_id TEXT NOT NULL, depends_on TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'blocks', reason TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (task_id, depends_on));

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
  agent TEXT NOT NULL, state TEXT NOT NULL, worker TEXT, workspace_path TEXT,
  branch TEXT, started_at TEXT NOT NULL, ended_at TEXT,
  reason TEXT NOT NULL DEFAULT '', cost_usd REAL NOT NULL DEFAULT 0,
  tokens INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
  iterations INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ix_runs_ativos ON runs(workspace_id, state);
CREATE INDEX IF NOT EXISTS ix_runs_task ON runs(task_id);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, ts TEXT NOT NULL,
  kind TEXT NOT NULL, task_id TEXT, run_id TEXT,
  actor TEXT NOT NULL DEFAULT 'engine', summary TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ix_events_task ON events(task_id, ts);
CREATE INDEX IF NOT EXISTS ix_events_ws ON events(workspace_id, ts);

CREATE TABLE IF NOT EXISTS actions (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, ts TEXT NOT NULL,
  agent TEXT NOT NULL, action TEXT NOT NULL, resource TEXT NOT NULL,
  effect TEXT NOT NULL, risk TEXT NOT NULL, task_id TEXT, run_id TEXT,
  rule TEXT, reason TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
  duration_ms INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,
  tokens INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_actions_ws ON actions(workspace_id, ts);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, task_id TEXT NOT NULL,
  run_id TEXT, state TEXT NOT NULL, risk TEXT NOT NULL,
  what_happened TEXT NOT NULL, why_it_matters TEXT NOT NULL,
  attempts TEXT NOT NULL DEFAULT '[]', options TEXT NOT NULL DEFAULT '[]',
  recommendation TEXT, created_at TEXT NOT NULL, decided_at TEXT,
  decided_by TEXT, choice TEXT, note TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_approvals_abertos ON approvals(workspace_id, state);

-- A trava e (workspace, resource), nunca so o recurso.
--
-- Dois clientes com um repositorio de MESMO NOME sao dois repositorios. Com o
-- resource nu como key, um cliente atrasaria o outro: conservador o bastante
-- para nunca corromper nada, e errado o bastante para ninguem descobrir por que
-- o motor do cliente B fica parado quando o cliente A trabalha.
CREATE TABLE IF NOT EXISTS leases (
  workspace_id TEXT NOT NULL, resource TEXT NOT NULL, owner TEXT NOT NULL,
  expires_at TEXT NOT NULL, renewed_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, resource));

CREATE TABLE IF NOT EXISTS targets (
  workspace_id TEXT NOT NULL, task_key TEXT NOT NULL,
  repo_provider TEXT NOT NULL, repo_key TEXT NOT NULL,
  source TEXT NOT NULL, confidence TEXT NOT NULL, strength INTEGER NOT NULL DEFAULT 0,
  evidence TEXT NOT NULL DEFAULT '[]', alternatives TEXT NOT NULL DEFAULT '[]',
  discovered_at TEXT NOT NULL, confirmed_at TEXT,
  confirmations INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (workspace_id, task_key, repo_provider, repo_key));

CREATE TABLE IF NOT EXISTS counters (
  workspace_id TEXT NOT NULL, day TEXT NOT NULL, name TEXT NOT NULL,
  value INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (workspace_id, day, name));
"""

SCHEMA_VERSION = "4"


def _v1_to_v2(c: sqlite3.Connection) -> None:
    """Lease deixa de ser global por key e passa a ser (workspace, resource).

    A tabela e recriada em vez de alterada: `ALTER TABLE` do SQLite nao muda
    key primaria, e um lease e state EFEMERO por definicao -- ele expira
    sozinho. O pior caso desta migracao e um worker vivo perder a trava, e esse
    caso ja tem tratamento: o lease vence, a recuperacao devolve a task a fila e
    o proximo tick retoma. Preservar linhas aqui daria trabalho para salvar dado
    que o motor foi desenhado para descartar.
    """
    # `execute`, nunca `executescript`: este ultimo faz COMMIT implicito e
    # mataria a transacao da migracao no meio, deixando o banco entre duas
    # versoes -- o unico state que a escada existe para impedir.
    c.execute("DROP TABLE IF EXISTS leases")
    c.execute("""CREATE TABLE leases (
                   workspace_id TEXT NOT NULL, resource TEXT NOT NULL,
                   owner TEXT NOT NULL, expires_at TEXT NOT NULL,
                   renewed_at TEXT NOT NULL,
                   PRIMARY KEY (workspace_id, resource))""")


#: Escada de migracao: versao de source_state -> como chegar na proxima.
#:
#: Existe porque a alternativa e pedir ao owner que apague o banco -- e o banco e
#: exatamente onde vive o state que o motor promete nao perder. Um bump de
#: esquema sem migracao transforma a promessa em pegadinha.
def _v2_to_v3(c: sqlite3.Connection) -> None:
    """Column and table names go to en-US, project-wide.

    `ALTER TABLE ... RENAME` keeps the rows. Recreating empty would have been one
    line shorter and would have thrown away exactly the state this engine
    promises not to lose -- which is the whole reason the ladder exists.
    """
    def has_table(name: str) -> bool:
        return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                         (name,)).fetchone() is not None

    def columns(table: str) -> set[str]:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}

    # `meta` primeiro: e nele que a propria versao do esquema esta guardada, e
    # o resto da migracao precisa consegui-la ler.
    if has_table("meta") and "chave" in columns("meta"):
        c.execute('ALTER TABLE meta RENAME COLUMN "chave" TO "key"')
    if has_table("meta") and "valor" in columns("meta"):
        c.execute('ALTER TABLE meta RENAME COLUMN "valor" TO "value"')

    for old_table, new_table in (("eventos", "events"), ("acoes", "actions"),
                                 ("contadores", "counters")):
        if has_table(old_table) and not has_table(new_table):
            c.execute(f"ALTER TABLE {old_table} RENAME TO {new_table}")

    renames = {
        "meta": {"chave": "key", "valor": "value"},
        "workspaces": {"nome": "name", "autonomia": "autonomy", "raiz": "root"},
        "projects": {"nome": "name", "ambiente_padrao": "default_environment",
                     "autonomia": "autonomy"},
        "repositories": {"nome": "name", "branch_base": "base_branch"},
        "tasks": {"titulo": "title", "estado": "state", "descricao": "description",
                  "chave_externa": "external_key", "prioridade": "priority",
                  "risco": "risk", "pausada_em": "paused_at", "recursos": "resources",
                  "tentativas": "attempts", "criada_em": "created_at",
                  "atualizada_em": "updated_at", "dados": "data"},
        "deps": {"depende_de": "depends_on", "tipo": "kind", "motivo": "reason"},
        "runs": {"agente": "agent", "estado": "state", "iniciado_em": "started_at",
                 "encerrado_em": "ended_at", "motivo": "reason",
                 "custo_usd": "cost_usd", "chamadas_tool": "tool_calls",
                 "iteracoes": "iterations", "dados": "data"},
        "events": {"tipo": "kind", "ator": "actor", "resumo": "summary",
                   "dados": "data"},
        "actions": {"agente": "agent", "acao": "action", "recurso": "resource",
                    "efeito": "effect", "risco": "risk", "regra": "rule",
                    "motivo": "reason", "resultado": "result",
                    "duracao_ms": "duration_ms", "custo_usd": "cost_usd"},
        "approvals": {"estado": "state", "risco": "risk",
                      "o_que_aconteceu": "what_happened",
                      "por_que_importa": "why_it_matters", "tentativas": "attempts",
                      "opcoes": "options", "recomendacao": "recommendation",
                      "criada_em": "created_at", "decidida_em": "decided_at",
                      "decidida_por": "decided_by", "escolha": "choice",
                      "nota": "note"},
        "leases": {"recurso": "resource", "dono": "owner",
                   "expira_em": "expires_at", "renovado_em": "renewed_at"},
        "counters": {"dia": "day", "nome": "name", "valor": "value"},
    }
    for table, mapa in renames.items():
        if not has_table(table):
            continue
        atuais = columns(table)
        for old, new in mapa.items():
            if old in atuais and new not in atuais:
                c.execute(f'ALTER TABLE {table} RENAME COLUMN "{old}" TO "{new}"')


def _v3_to_v4(c: sqlite3.Connection) -> None:
    """Targets: where each task runs, with the evidence behind the claim."""
    c.execute("""CREATE TABLE IF NOT EXISTS targets (
                   workspace_id TEXT NOT NULL, task_key TEXT NOT NULL,
                   repo_provider TEXT NOT NULL, repo_key TEXT NOT NULL,
                   source TEXT NOT NULL, confidence TEXT NOT NULL,
                   strength INTEGER NOT NULL DEFAULT 0,
                   evidence TEXT NOT NULL DEFAULT '[]',
                   alternatives TEXT NOT NULL DEFAULT '[]',
                   discovered_at TEXT NOT NULL, confirmed_at TEXT,
                   confirmations INTEGER NOT NULL DEFAULT 1,
                   PRIMARY KEY (workspace_id, task_key, repo_provider, repo_key))""")


MIGRATIONS: dict[str, tuple[str, Any]] = {
    "1": ("2", _v1_to_v2),
    "2": ("3", _v2_to_v3),
    "3": ("4", _v3_to_v4),
}


def _iso(d: datetime | None) -> str | None:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ") if d else None


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


class SqliteStore(Store):
    name = "sqlite"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self.path), isolation_level=None,
                                    check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.execute("PRAGMA foreign_keys=ON")
        self._con.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        self._con.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._con.execute("BEGIN IMMEDIATE")
        try:
            yield self._con
        except Exception:
            self._con.execute("ROLLBACK")
            raise
        self._con.execute("COMMIT")

    # ---- esquema ---------------------------------------------------------

    def migrate(self) -> None:
        """Create the schema if needed, then walk the ladder to the current version.

        `executescript` issues an implicit COMMIT before running, so it can NOT
        live inside `_tx`: the transaction would die halfway and the final COMMIT
        would fail. The DDL is idempotent (`IF NOT EXISTS`), which makes running
        it loose safe; the version record comes after, inside a transaction.
        """
        self._con.executescript(SCHEMA)
        with self._tx() as c:
            # `meta` may predate the vocabulary standardisation, in which case
            # `CREATE TABLE IF NOT EXISTS` left the old column names in place and
            # the new ones simply do not exist. Probing beats assuming: reading
            # the wrong column raises, and a raise here would look like a corrupt
            # database rather than an old one.
            cols = {r[1] for r in c.execute("PRAGMA table_info(meta)")}
            if "value" in cols:
                row = c.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
                found = row["value"] if row else None
            elif "valor" in cols:
                row = c.execute("SELECT valor FROM meta WHERE chave='esquema'").fetchone()
                found = row[0] if row else None
            else:
                found = None

            if found is None:
                c.execute("INSERT INTO meta(key, value) VALUES('schema', ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (SCHEMA_VERSION,))
            elif found != SCHEMA_VERSION:
                self._upgrade(c, found)

    def _upgrade(self, c: sqlite3.Connection, current: str) -> None:
        """Apply the ladder, one rung at a time, inside the open transaction."""
        seen = {current}
        while current != SCHEMA_VERSION:
            if current not in MIGRATIONS:
                raise CorruptedState(
                    f"database is at version {current} and there is no path to "
                    f"{SCHEMA_VERSION}. Either it came from a newer engine, or a "
                    f"migration step does not exist yet.")
            nxt, apply_step = MIGRATIONS[current]
            apply_step(c)
            current = nxt
            if current in seen:
                raise CorruptedState(f"cycle in the migration ladder at {current}")
            seen.add(current)
        c.execute("INSERT INTO meta(key, value) VALUES('schema', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (SCHEMA_VERSION,))

    def verify(self) -> None:
        linha = self._con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if linha is None:
            raise CorruptedState("banco sem versao de esquema: rode `regente init`")

    # ---- tenancy ---------------------------------------------------------

    def save_workspace(self, w: Workspace) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO workspaces(id, client_id, name, autonomy, root)
                         VALUES(?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET
                           client_id=excluded.client_id, name=excluded.name,
                           autonomy=excluded.autonomy, root=excluded.root""",
                      (w.id, w.client_id, w.name, int(w.max_autonomy), w.root))

    def _workspace_row(self, r: sqlite3.Row) -> Workspace:
        return Workspace(id=r["id"], client_id=r["client_id"], name=r["name"],
                         max_autonomy=AutonomyLevel(r["autonomy"]), root=r["root"])

    def workspace(self, workspace_id: str) -> Workspace | None:
        r = self._con.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        return self._workspace_row(r) if r else None

    def workspaces(self) -> list[Workspace]:
        return [self._workspace_row(r) for r in
                self._con.execute("SELECT * FROM workspaces ORDER BY name")]

    def save_project(self, p: Project) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO projects(id, workspace_id, name, default_environment, autonomy)
                         VALUES(?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                           default_environment=excluded.default_environment, autonomy=excluded.autonomy""",
                      (p.id, p.workspace_id, p.name, p.default_environment,
                       int(p.max_autonomy) if p.max_autonomy is not None else None))

    def projects(self, workspace_id: str) -> list[Project]:
        return [Project(id=r["id"], workspace_id=r["workspace_id"], name=r["name"],
                        default_environment=r["default_environment"],
                        max_autonomy=(AutonomyLevel(r["autonomy"])
                                          if r["autonomy"] is not None else None))
                for r in self._con.execute(
                    "SELECT * FROM projects WHERE workspace_id=? ORDER BY name", (workspace_id,))]

    def save_repository(self, r: Repository) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO repositories(id, project_id, name, base_branch, url)
                         VALUES(?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                           base_branch=excluded.base_branch, url=excluded.url""",
                      (r.id, r.project_id, r.name, r.base_branch, r.url))

    # ---- tasks -----------------------------------------------------------

    def _task_row(self, r: sqlite3.Row) -> Task:
        externo = (ExternalRef(provider=r["provider"], key=r["external_key"], url=r["url"])
                   if r["provider"] else None)
        return Task(
            id=r["id"], workspace_id=r["workspace_id"], project_id=r["project_id"],
            title=r["title"], state=TaskState(r["state"]), externo=externo,
            description=r["description"], priority=r["priority"],
            risk=RiskLevel[r["risk"]] if r["risk"] else None,
            paused_at=TaskState(r["paused_at"]) if r["paused_at"] else None,
            resources=tuple(json.loads(r["resources"])), attempts=r["attempts"],
            created_at=_dt(r["created_at"]), updated_at=_dt(r["updated_at"]),
            data=json.loads(r["data"]))

    def save_task(self, t: Task) -> None:
        with self._tx() as c:
            self._save_task_row(c, t)

    def _save_task_row(self, c: sqlite3.Connection, t: Task) -> None:
        c.execute("""INSERT INTO tasks(id, workspace_id, project_id, title, state, description,
                       provider, external_key, url, priority, risk, paused_at, resources,
                       attempts, created_at, updated_at, data)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(id) DO UPDATE SET
                       title=excluded.title, state=excluded.state,
                       description=excluded.description, priority=excluded.priority,
                       risk=excluded.risk, paused_at=excluded.paused_at,
                       resources=excluded.resources, attempts=excluded.attempts,
                       updated_at=excluded.updated_at, data=excluded.data""",
                  (t.id, t.workspace_id, t.project_id, t.title, t.state.value, t.description,
                   t.externo.provider if t.externo else None,
                   t.externo.key if t.externo else None,
                   t.externo.url if t.externo else None,
                   t.priority, t.risk.name if t.risk else None,
                   t.paused_at.value if t.paused_at else None,
                   _j(list(t.resources)), t.attempts,
                   _iso(t.created_at), _iso(t.updated_at), _j(t.data)))

    def task(self, task_id: str) -> Task | None:
        r = self._con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task_row(r) if r else None

    def task_by_key(self, workspace_id: str, provider: str, key: str) -> Task | None:
        r = self._con.execute(
            "SELECT * FROM tasks WHERE workspace_id=? AND provider=? AND external_key=?",
            (workspace_id, provider, key)).fetchone()
        return self._task_row(r) if r else None

    def tasks(self, workspace_id: str, estados: list[TaskState] | None = None) -> list[Task]:
        if estados:
            marks = ",".join("?" * len(estados))
            q = f"SELECT * FROM tasks WHERE workspace_id=? AND state IN ({marks})"
            args = [workspace_id] + [e.value for e in estados]
        else:
            q, args = "SELECT * FROM tasks WHERE workspace_id=?", [workspace_id]
        q += " ORDER BY priority, external_key, id"
        return [self._task_row(r) for r in self._con.execute(q, args)]

    def transition(self, task_id: str, destination: TaskState, actor: str,
                    reason: str = "", data: dict | None = None) -> Task:
        """Le, valida, grava e anota -- numa transacao so.

        Ler dentro da transacao (e nao antes) e o que impede dois ticks
        concorrentes de partirem do mesmo state e ambos despacharem.
        """
        with self._tx() as c:
            r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if r is None:
                raise CorruptedState(f"task {task_id} nao existe")
            t = self._task_row(r)
            source = t.state
            require(source, destination, t.paused_at)

            t.paused_at = source if destination is TaskState.WAITING_HUMAN else None
            t.state = destination
            t.updated_at = agora()
            self._save_task_row(c, t)

            c.execute("""INSERT INTO events(id, workspace_id, ts, kind, task_id, actor, summary, data)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (ids.new_id(ids.EVENT), t.workspace_id, _iso(agora()), "transicao",
                       t.id, actor, f"{source.value} -> {destination.value}",
                       _j({"from": source.value, "to": destination.value,
                           "reason": reason, **(data or {})})))
        return t

    def link_dependency(self, d: Dependency) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO deps(task_id, depends_on, kind, reason) VALUES(?,?,?,?)
                         ON CONFLICT(task_id, depends_on) DO UPDATE SET
                           kind=excluded.kind, reason=excluded.reason""",
                      (d.task_id, d.depends_on, d.kind, d.reason))

    def dependencies(self, workspace_id: str) -> list[Dependency]:
        return [Dependency(task_id=r["task_id"], depends_on=r["depends_on"],
                           kind=r["kind"], reason=r["reason"])
                for r in self._con.execute(
                    """SELECT d.* FROM deps d JOIN tasks t ON t.id = d.task_id
                       WHERE t.workspace_id=?""", (workspace_id,))]

    # ---- runs ------------------------------------------------------------

    def _run_row(self, r: sqlite3.Row) -> Run:
        return Run(id=r["id"], task_id=r["task_id"], workspace_id=r["workspace_id"],
                   agent=r["agent"], state=RunState(r["state"]), worker=r["worker"],
                   workspace_path=r["workspace_path"], branch=r["branch"],
                   started_at=_dt(r["started_at"]), ended_at=_dt(r["ended_at"]),
                   reason=r["reason"], cost_usd=r["cost_usd"], tokens=r["tokens"],
                   tool_calls=r["tool_calls"], iterations=r["iterations"],
                   data=json.loads(r["data"]))

    def save_run(self, r: Run) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO runs(id, task_id, workspace_id, agent, state, worker,
                           workspace_path, branch, started_at, ended_at, reason,
                           cost_usd, tokens, tool_calls, iterations, data)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET state=excluded.state,
                           worker=excluded.worker, workspace_path=excluded.workspace_path,
                           branch=excluded.branch, ended_at=excluded.ended_at,
                           reason=excluded.reason, cost_usd=excluded.cost_usd,
                           tokens=excluded.tokens, tool_calls=excluded.tool_calls,
                           iterations=excluded.iterations, data=excluded.data""",
                      (r.id, r.task_id, r.workspace_id, r.agent, r.state.value, r.worker,
                       r.workspace_path, r.branch, _iso(r.started_at), _iso(r.ended_at),
                       r.reason, r.cost_usd, r.tokens, r.tool_calls, r.iterations,
                       _j(r.data)))

    def run(self, run_id: str) -> Run | None:
        r = self._con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._run_row(r) if r else None

    def active_runs(self, workspace_id: str) -> list[Run]:
        return [self._run_row(r) for r in self._con.execute(
            "SELECT * FROM runs WHERE workspace_id=? AND state=? ORDER BY started_at",
            (workspace_id, RunState.RUNNING.value))]

    def task_runs(self, task_id: str) -> list[Run]:
        return [self._run_row(r) for r in self._con.execute(
            "SELECT * FROM runs WHERE task_id=? ORDER BY started_at", (task_id,))]

    # ---- trilha ----------------------------------------------------------

    def record_event(self, e: Event) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO events(id, workspace_id, ts, kind, task_id, run_id,
                           actor, summary, data) VALUES(?,?,?,?,?,?,?,?,?)""",
                      (e.id, e.workspace_id, _iso(e.ts), e.kind, e.task_id, e.run_id,
                       e.actor, e.summary, _j(e.data)))

    def events(self, workspace_id: str, task_id: str | None = None,
                limit: int = 100) -> list[Event]:
        if task_id:
            q = "SELECT * FROM events WHERE workspace_id=? AND task_id=? ORDER BY ts DESC LIMIT ?"
            args: list[Any] = [workspace_id, task_id, limit]
        else:
            q = "SELECT * FROM events WHERE workspace_id=? ORDER BY ts DESC LIMIT ?"
            args = [workspace_id, limit]
        return [Event(id=r["id"], workspace_id=r["workspace_id"], kind=r["kind"],
                      ts=_dt(r["ts"]), task_id=r["task_id"], run_id=r["run_id"],
                      actor=r["actor"], summary=r["summary"], data=json.loads(r["data"]))
                for r in self._con.execute(q, args)]

    def record_action(self, a: ActionRecord) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO actions(id, workspace_id, ts, agent, action, resource, effect,
                           risk, task_id, run_id, rule, reason, result, duration_ms,
                           cost_usd, tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (a.id, a.workspace_id, _iso(a.ts), a.agent, a.action, a.resource,
                       a.effect, a.risk, a.task_id, a.run_id, a.rule, a.reason,
                       a.result, a.duration_ms, a.cost_usd, a.tokens))

    def actions(self, workspace_id: str, limit: int = 100) -> list[ActionRecord]:
        return [ActionRecord(id=r["id"], workspace_id=r["workspace_id"], agent=r["agent"],
                             action=r["action"], resource=r["resource"], effect=r["effect"],
                             risk=r["risk"], ts=_dt(r["ts"]), task_id=r["task_id"],
                             run_id=r["run_id"], rule=r["rule"], reason=r["reason"],
                             result=r["result"], duration_ms=r["duration_ms"],
                             cost_usd=r["cost_usd"], tokens=r["tokens"])
                for r in self._con.execute(
                    "SELECT * FROM actions WHERE workspace_id=? ORDER BY ts DESC LIMIT ?",
                    (workspace_id, limit))]

    # ---- approvals -------------------------------------------------------

    def _approval_row(self, r: sqlite3.Row) -> Approval:
        return Approval(
            id=r["id"], workspace_id=r["workspace_id"], task_id=r["task_id"],
            what_happened=r["what_happened"], why_it_matters=r["why_it_matters"],
            what_was_tried=tuple(json.loads(r["attempts"])),
            options=tuple(Option(**o) for o in json.loads(r["options"])),
            recommendation=r["recommendation"], risk=RiskLevel[r["risk"]],
            state=ApprovalState(r["state"]), run_id=r["run_id"],
            created_at=_dt(r["created_at"]), decided_at=_dt(r["decided_at"]),
            decided_by=r["decided_by"], choice=r["choice"], note=r["note"])

    def open_approval(self, a: Approval) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO approvals(id, workspace_id, task_id, run_id, state, risk,
                           what_happened, why_it_matters, attempts, options, recommendation,
                           created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (a.id, a.workspace_id, a.task_id, a.run_id, a.state.value, a.risk.name,
                       a.what_happened, a.why_it_matters, _j(list(a.what_was_tried)),
                       _j([{"id": o.id, "label": o.label, "effect": o.effect} for o in a.options]),
                       a.recommendation, _iso(a.created_at)))
            c.execute("""INSERT INTO events(id, workspace_id, ts, kind, task_id, run_id,
                           actor, summary, data) VALUES(?,?,?,?,?,?,?,?,?)""",
                      (ids.new_id(ids.EVENT), a.workspace_id, _iso(agora()), "escalou",
                       a.task_id, a.run_id, "engine", a.what_happened,
                       _j({"approval_id": a.id, "risk": a.risk.name})))

    def open_approvals(self, workspace_id: str) -> list[Approval]:
        return [self._approval_row(r) for r in self._con.execute(
            "SELECT * FROM approvals WHERE workspace_id=? AND state=? ORDER BY risk DESC, created_at",
            (workspace_id, ApprovalState.OPEN.value))]

    def approval(self, approval_id: str) -> Approval | None:
        r = self._con.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return self._approval_row(r) if r else None

    def decide_approval(self, approval_id: str, choice: str, por: str, note: str = "") -> Approval:
        with self._tx() as c:
            r = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if r is None:
                raise CorruptedState(f"approval {approval_id} nao existe")
            a = self._approval_row(r)
            if a.state is not ApprovalState.OPEN:
                raise CorruptedState(f"approval {approval_id} ja foi decidido")
            valid = {o.id for o in a.options}
            if valid and choice not in valid:
                raise CorruptedState(
                    f"choice '{choice}' is not among the options: {', '.join(sorted(valid))}")
            a.state, a.choice, a.decided_by = ApprovalState.DECIDED, choice, por
            a.decided_at, a.note = agora(), note
            c.execute("""UPDATE approvals SET state=?, choice=?, decided_by=?,
                           decided_at=?, note=? WHERE id=?""",
                      (a.state.value, choice, por, _iso(a.decided_at), note, approval_id))
            c.execute("""INSERT INTO events(id, workspace_id, ts, kind, task_id, run_id,
                           actor, summary, data) VALUES(?,?,?,?,?,?,?,?,?)""",
                      (ids.new_id(ids.EVENT), a.workspace_id, _iso(agora()), "decisao_humana",
                       a.task_id, a.run_id, por, f"escolheu '{choice}'",
                       _j({"approval_id": approval_id, "note": note})))
        return a

    # ---- leases ----------------------------------------------------------

    def acquire_lease(self, resource: str, owner: str, workspace_id: str,
                      segundos: int) -> Lease | None:
        """Concede se livre, vencido, ou ja do mesmo owner (renovacao)."""
        ts = agora()
        expira = ts + timedelta(seconds=segundos)
        with self._tx() as c:
            r = c.execute("SELECT * FROM leases WHERE workspace_id=? AND resource=?",
                          (workspace_id, resource)).fetchone()
            if r is not None:
                vivo = _dt(r["expires_at"]) > ts
                if vivo and r["owner"] != owner:
                    return None
            c.execute("""INSERT INTO leases(workspace_id, resource, owner, expires_at, renewed_at)
                         VALUES(?,?,?,?,?)
                         ON CONFLICT(workspace_id, resource) DO UPDATE SET
                           owner=excluded.owner, expires_at=excluded.expires_at,
                           renewed_at=excluded.renewed_at""",
                      (workspace_id, resource, owner, _iso(expira), _iso(ts)))
        return Lease(resource=resource, owner=owner, expires_at=expira,
                     workspace_id=workspace_id, renewed_at=ts)

    def renew_lease(self, resource: str, owner: str, segundos: int,
                     workspace_id: str | None = None) -> bool:
        ts = agora()
        with self._tx() as c:
            if workspace_id:
                cur = c.execute("""UPDATE leases SET expires_at=?, renewed_at=?
                                   WHERE workspace_id=? AND resource=? AND owner=?""",
                                (_iso(ts + timedelta(seconds=segundos)), _iso(ts),
                                 workspace_id, resource, owner))
            else:
                # Sem workspace, o owner do lease e o filtro. `dono` e um id de run,
                # que ja e unico -- entao isto continua seguro, so menos explicito.
                cur = c.execute("""UPDATE leases SET expires_at=?, renewed_at=?
                                   WHERE resource=? AND owner=?""",
                                (_iso(ts + timedelta(seconds=segundos)), _iso(ts),
                                 resource, owner))
            return cur.rowcount > 0

    def release_lease(self, resource: str, owner: str, workspace_id: str | None = None) -> None:
        with self._tx() as c:
            if workspace_id:
                c.execute("DELETE FROM leases WHERE workspace_id=? AND resource=? AND owner=?",
                          (workspace_id, resource, owner))
            else:
                c.execute("DELETE FROM leases WHERE resource=? AND owner=?", (resource, owner))

    def expired_leases(self, workspace_id: str, when: datetime | None = None) -> list[Lease]:
        ts = when or agora()
        return [Lease(resource=r["resource"], owner=r["owner"], expires_at=_dt(r["expires_at"]),
                      workspace_id=r["workspace_id"], renewed_at=_dt(r["renewed_at"]))
                for r in self._con.execute(
                    "SELECT * FROM leases WHERE workspace_id=? AND expires_at < ?",
                    (workspace_id, _iso(ts)))]

    # ---- counters ------------------------------------------------------

    def dispatch_count(self, workspace_id: str, day: str) -> int:
        r = self._con.execute(
            "SELECT value FROM counters WHERE workspace_id=? AND day=? AND name='despachos'",
            (workspace_id, day)).fetchone()
        return r["value"] if r else 0

    def mark_dispatch(self, workspace_id: str, day: str) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO counters(workspace_id, day, name, value) VALUES(?,?,'dispatches',1)
                         ON CONFLICT(workspace_id, day, name) DO UPDATE SET value = value + 1""",
                      (workspace_id, day))

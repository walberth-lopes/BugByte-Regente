# -*- coding: utf-8 -*-
"""Store em SQLite. Unico modulo do motor que fala SQL.

Escolhas que nao sao detalhe:

- **WAL + `BEGIN IMMEDIATE`.** Um tick pode rodar enquanto a UI le. WAL permite
  isso; `IMMEDIATE` na escrita garante que duas transicoes concorrentes da mesma
  task nao se sobreponham -- e a transicao e onde um despacho duplicado nasce.
- **Datas em texto ISO-8601 UTC.** SQLite nao tem tipo de data. Guardar epoch
  economiza nada e torna o banco ilegivel num momento em que ler o banco a mao e
  exatamente o que se precisa fazer.
- **`estado` guardado como texto do Enum.** Migrar um Enum e trivial; migrar um
  inteiro cujo significado mudou e um dia perdido.
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
from ..core.errors import EstadoCorrompido
from ..core.model import (ActionRecord, Approval, ApprovalState, Dependency, Event,
                          ExternalRef, Lease, Option, Project, Repository, Run,
                          RunState, Task, Workspace, agora)
from ..core.policy import AutonomyLevel
from ..core.risk import RiskLevel
from ..core.states import TaskState, exige
from ..ports.store import Store

ESQUEMA = """
CREATE TABLE IF NOT EXISTS meta (chave TEXT PRIMARY KEY, valor TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY, client_id TEXT NOT NULL, nome TEXT NOT NULL,
  autonomia INTEGER NOT NULL DEFAULT 2, raiz TEXT);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, nome TEXT NOT NULL,
  ambiente_padrao TEXT NOT NULL DEFAULT 'staging', autonomia INTEGER);

CREATE TABLE IF NOT EXISTS repositories (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, nome TEXT NOT NULL,
  branch_base TEXT NOT NULL DEFAULT 'main', url TEXT);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, project_id TEXT NOT NULL,
  titulo TEXT NOT NULL, estado TEXT NOT NULL, descricao TEXT NOT NULL DEFAULT '',
  provider TEXT, chave_externa TEXT, url TEXT,
  prioridade INTEGER NOT NULL DEFAULT 100, risco TEXT, pausada_em TEXT,
  recursos TEXT NOT NULL DEFAULT '[]', tentativas INTEGER NOT NULL DEFAULT 0,
  criada_em TEXT NOT NULL, atualizada_em TEXT NOT NULL,
  dados TEXT NOT NULL DEFAULT '{}');
-- Identidade externa e unica por workspace: o mesmo FAXINA-183 em dois clientes
-- sao duas tasks distintas, e nunca podem colidir.
CREATE UNIQUE INDEX IF NOT EXISTS ix_tasks_externa
  ON tasks(workspace_id, provider, chave_externa)
  WHERE provider IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_tasks_estado ON tasks(workspace_id, estado);

CREATE TABLE IF NOT EXISTS deps (
  task_id TEXT NOT NULL, depende_de TEXT NOT NULL,
  tipo TEXT NOT NULL DEFAULT 'blocks', motivo TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (task_id, depende_de));

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
  agente TEXT NOT NULL, estado TEXT NOT NULL, worker TEXT, workspace_path TEXT,
  branch TEXT, iniciado_em TEXT NOT NULL, encerrado_em TEXT,
  motivo TEXT NOT NULL DEFAULT '', custo_usd REAL NOT NULL DEFAULT 0,
  tokens INTEGER NOT NULL DEFAULT 0, chamadas_tool INTEGER NOT NULL DEFAULT 0,
  iteracoes INTEGER NOT NULL DEFAULT 0, dados TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ix_runs_ativos ON runs(workspace_id, estado);
CREATE INDEX IF NOT EXISTS ix_runs_task ON runs(task_id);

CREATE TABLE IF NOT EXISTS eventos (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, ts TEXT NOT NULL,
  tipo TEXT NOT NULL, task_id TEXT, run_id TEXT,
  ator TEXT NOT NULL DEFAULT 'engine', resumo TEXT NOT NULL DEFAULT '',
  dados TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ix_eventos_task ON eventos(task_id, ts);
CREATE INDEX IF NOT EXISTS ix_eventos_ws ON eventos(workspace_id, ts);

CREATE TABLE IF NOT EXISTS acoes (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, ts TEXT NOT NULL,
  agente TEXT NOT NULL, acao TEXT NOT NULL, recurso TEXT NOT NULL,
  efeito TEXT NOT NULL, risco TEXT NOT NULL, task_id TEXT, run_id TEXT,
  regra TEXT, motivo TEXT NOT NULL DEFAULT '', resultado TEXT NOT NULL DEFAULT '',
  duracao_ms INTEGER NOT NULL DEFAULT 0, custo_usd REAL NOT NULL DEFAULT 0,
  tokens INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_acoes_ws ON acoes(workspace_id, ts);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, task_id TEXT NOT NULL,
  run_id TEXT, estado TEXT NOT NULL, risco TEXT NOT NULL,
  o_que_aconteceu TEXT NOT NULL, por_que_importa TEXT NOT NULL,
  tentativas TEXT NOT NULL DEFAULT '[]', opcoes TEXT NOT NULL DEFAULT '[]',
  recomendacao TEXT, criada_em TEXT NOT NULL, decidida_em TEXT,
  decidida_por TEXT, escolha TEXT, nota TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_approvals_abertos ON approvals(workspace_id, estado);

-- A trava e (workspace, recurso), nunca so o recurso.
--
-- Dois clientes com um repositorio de MESMO NOME sao dois repositorios. Com o
-- recurso nu como chave, um cliente atrasaria o outro: conservador o bastante
-- para nunca corromper nada, e errado o bastante para ninguem descobrir por que
-- o motor do cliente B fica parado quando o cliente A trabalha.
CREATE TABLE IF NOT EXISTS leases (
  workspace_id TEXT NOT NULL, recurso TEXT NOT NULL, dono TEXT NOT NULL,
  expira_em TEXT NOT NULL, renovado_em TEXT NOT NULL,
  PRIMARY KEY (workspace_id, recurso));

CREATE TABLE IF NOT EXISTS contadores (
  workspace_id TEXT NOT NULL, dia TEXT NOT NULL, nome TEXT NOT NULL,
  valor INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (workspace_id, dia, nome));
"""

VERSAO_ESQUEMA = "2"


def _v1_para_v2(c: sqlite3.Connection) -> None:
    """Lease deixa de ser global por chave e passa a ser (workspace, recurso).

    A tabela e recriada em vez de alterada: `ALTER TABLE` do SQLite nao muda
    chave primaria, e um lease e estado EFEMERO por definicao -- ele expira
    sozinho. O pior caso desta migracao e um worker vivo perder a trava, e esse
    caso ja tem tratamento: o lease vence, a recuperacao devolve a task a fila e
    o proximo tick retoma. Preservar linhas aqui daria trabalho para salvar dado
    que o motor foi desenhado para descartar.
    """
    # `execute`, nunca `executescript`: este ultimo faz COMMIT implicito e
    # mataria a transacao da migracao no meio, deixando o banco entre duas
    # versoes -- o unico estado que a escada existe para impedir.
    c.execute("DROP TABLE IF EXISTS leases")
    c.execute("""CREATE TABLE leases (
                   workspace_id TEXT NOT NULL, recurso TEXT NOT NULL,
                   dono TEXT NOT NULL, expira_em TEXT NOT NULL,
                   renovado_em TEXT NOT NULL,
                   PRIMARY KEY (workspace_id, recurso))""")


#: Escada de migracao: versao de origem -> como chegar na proxima.
#:
#: Existe porque a alternativa e pedir ao dono que apague o banco -- e o banco e
#: exatamente onde vive o estado que o motor promete nao perder. Um bump de
#: esquema sem migracao transforma a promessa em pegadinha.
MIGRACOES: dict[str, tuple[str, Any]] = {
    "1": ("2", _v1_para_v2),
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
    nome = "sqlite"

    def __init__(self, caminho: str | Path):
        self.caminho = Path(caminho)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self.caminho), isolation_level=None,
                                    check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.execute("PRAGMA foreign_keys=ON")
        self._con.execute("PRAGMA busy_timeout=5000")

    def fecha(self) -> None:
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

    def migra(self) -> None:
        # `executescript` faz COMMIT implicito antes de rodar, entao ele NAO pode
        # viver dentro de `_tx`: a transacao morreria no meio e o COMMIT final
        # falharia. O DDL e idempotente (`IF NOT EXISTS`), o que torna seguro
        # roda-lo solto; o registro da versao vem depois, ai sim em transacao.
        self._con.executescript(ESQUEMA)
        with self._tx() as c:
            atual = c.execute("SELECT valor FROM meta WHERE chave='esquema'").fetchone()
            if atual is None:
                c.execute("INSERT INTO meta(chave, valor) VALUES('esquema', ?)", (VERSAO_ESQUEMA,))
            elif atual["valor"] != VERSAO_ESQUEMA:
                self._sobe(c, atual["valor"])

    def _sobe(self, c: sqlite3.Connection, de: str) -> None:
        """Aplica a escada, um degrau por vez, dentro da transacao aberta."""
        visitadas = {de}
        while de != VERSAO_ESQUEMA:
            if de not in MIGRACOES:
                raise EstadoCorrompido(
                    f"banco na versao {de} e nao ha caminho ate {VERSAO_ESQUEMA}. "
                    f"Este banco veio de uma versao mais nova do motor, ou de um "
                    f"caminho de migracao que ainda nao existe.")
            proxima, aplicar = MIGRACOES[de]
            aplicar(c)
            de = proxima
            if de in visitadas:
                raise EstadoCorrompido(f"ciclo na escada de migracao em {de}")
            visitadas.add(de)
        c.execute("UPDATE meta SET valor=? WHERE chave='esquema'", (VERSAO_ESQUEMA,))

    def verifica(self) -> None:
        linha = self._con.execute("SELECT valor FROM meta WHERE chave='esquema'").fetchone()
        if linha is None:
            raise EstadoCorrompido("banco sem versao de esquema: rode `regente init`")

    # ---- tenancy ---------------------------------------------------------

    def salva_workspace(self, w: Workspace) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO workspaces(id, client_id, nome, autonomia, raiz)
                         VALUES(?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET
                           client_id=excluded.client_id, nome=excluded.nome,
                           autonomia=excluded.autonomia, raiz=excluded.raiz""",
                      (w.id, w.client_id, w.nome, int(w.autonomia_maxima), w.raiz))

    def _workspace(self, r: sqlite3.Row) -> Workspace:
        return Workspace(id=r["id"], client_id=r["client_id"], nome=r["nome"],
                         autonomia_maxima=AutonomyLevel(r["autonomia"]), raiz=r["raiz"])

    def workspace(self, workspace_id: str) -> Workspace | None:
        r = self._con.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        return self._workspace(r) if r else None

    def workspaces(self) -> list[Workspace]:
        return [self._workspace(r) for r in
                self._con.execute("SELECT * FROM workspaces ORDER BY nome")]

    def salva_project(self, p: Project) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO projects(id, workspace_id, nome, ambiente_padrao, autonomia)
                         VALUES(?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET nome=excluded.nome,
                           ambiente_padrao=excluded.ambiente_padrao, autonomia=excluded.autonomia""",
                      (p.id, p.workspace_id, p.nome, p.ambiente_padrao,
                       int(p.autonomia_maxima) if p.autonomia_maxima is not None else None))

    def projects(self, workspace_id: str) -> list[Project]:
        return [Project(id=r["id"], workspace_id=r["workspace_id"], nome=r["nome"],
                        ambiente_padrao=r["ambiente_padrao"],
                        autonomia_maxima=(AutonomyLevel(r["autonomia"])
                                          if r["autonomia"] is not None else None))
                for r in self._con.execute(
                    "SELECT * FROM projects WHERE workspace_id=? ORDER BY nome", (workspace_id,))]

    def salva_repository(self, r: Repository) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO repositories(id, project_id, nome, branch_base, url)
                         VALUES(?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET nome=excluded.nome,
                           branch_base=excluded.branch_base, url=excluded.url""",
                      (r.id, r.project_id, r.nome, r.branch_base, r.url))

    # ---- tasks -----------------------------------------------------------

    def _task(self, r: sqlite3.Row) -> Task:
        externo = (ExternalRef(provider=r["provider"], key=r["chave_externa"], url=r["url"])
                   if r["provider"] else None)
        return Task(
            id=r["id"], workspace_id=r["workspace_id"], project_id=r["project_id"],
            titulo=r["titulo"], estado=TaskState(r["estado"]), externo=externo,
            descricao=r["descricao"], prioridade=r["prioridade"],
            risco=RiskLevel[r["risco"]] if r["risco"] else None,
            pausada_em=TaskState(r["pausada_em"]) if r["pausada_em"] else None,
            recursos=tuple(json.loads(r["recursos"])), tentativas=r["tentativas"],
            criada_em=_dt(r["criada_em"]), atualizada_em=_dt(r["atualizada_em"]),
            dados=json.loads(r["dados"]))

    def salva_task(self, t: Task) -> None:
        with self._tx() as c:
            self._salva_task(c, t)

    def _salva_task(self, c: sqlite3.Connection, t: Task) -> None:
        c.execute("""INSERT INTO tasks(id, workspace_id, project_id, titulo, estado, descricao,
                       provider, chave_externa, url, prioridade, risco, pausada_em, recursos,
                       tentativas, criada_em, atualizada_em, dados)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(id) DO UPDATE SET
                       titulo=excluded.titulo, estado=excluded.estado,
                       descricao=excluded.descricao, prioridade=excluded.prioridade,
                       risco=excluded.risco, pausada_em=excluded.pausada_em,
                       recursos=excluded.recursos, tentativas=excluded.tentativas,
                       atualizada_em=excluded.atualizada_em, dados=excluded.dados""",
                  (t.id, t.workspace_id, t.project_id, t.titulo, t.estado.value, t.descricao,
                   t.externo.provider if t.externo else None,
                   t.externo.key if t.externo else None,
                   t.externo.url if t.externo else None,
                   t.prioridade, t.risco.name if t.risco else None,
                   t.pausada_em.value if t.pausada_em else None,
                   _j(list(t.recursos)), t.tentativas,
                   _iso(t.criada_em), _iso(t.atualizada_em), _j(t.dados)))

    def task(self, task_id: str) -> Task | None:
        r = self._con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task(r) if r else None

    def task_por_chave(self, workspace_id: str, provider: str, key: str) -> Task | None:
        r = self._con.execute(
            "SELECT * FROM tasks WHERE workspace_id=? AND provider=? AND chave_externa=?",
            (workspace_id, provider, key)).fetchone()
        return self._task(r) if r else None

    def tasks(self, workspace_id: str, estados: list[TaskState] | None = None) -> list[Task]:
        if estados:
            marcas = ",".join("?" * len(estados))
            q = f"SELECT * FROM tasks WHERE workspace_id=? AND estado IN ({marcas})"
            args = [workspace_id] + [e.value for e in estados]
        else:
            q, args = "SELECT * FROM tasks WHERE workspace_id=?", [workspace_id]
        q += " ORDER BY prioridade, chave_externa, id"
        return [self._task(r) for r in self._con.execute(q, args)]

    def transiciona(self, task_id: str, destino: TaskState, ator: str,
                    motivo: str = "", dados: dict | None = None) -> Task:
        """Le, valida, grava e anota -- numa transacao so.

        Ler dentro da transacao (e nao antes) e o que impede dois ticks
        concorrentes de partirem do mesmo estado e ambos despacharem.
        """
        with self._tx() as c:
            r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if r is None:
                raise EstadoCorrompido(f"task {task_id} nao existe")
            t = self._task(r)
            origem = t.estado
            exige(origem, destino, t.pausada_em)

            t.pausada_em = origem if destino is TaskState.WAITING_HUMAN else None
            t.estado = destino
            t.atualizada_em = agora()
            self._salva_task(c, t)

            c.execute("""INSERT INTO eventos(id, workspace_id, ts, tipo, task_id, ator, resumo, dados)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (ids.novo(ids.EVENT), t.workspace_id, _iso(agora()), "transicao",
                       t.id, ator, f"{origem.value} -> {destino.value}",
                       _j({"de": origem.value, "para": destino.value,
                           "motivo": motivo, **(dados or {})})))
        return t

    def liga_dependencia(self, d: Dependency) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO deps(task_id, depende_de, tipo, motivo) VALUES(?,?,?,?)
                         ON CONFLICT(task_id, depende_de) DO UPDATE SET
                           tipo=excluded.tipo, motivo=excluded.motivo""",
                      (d.task_id, d.depende_de, d.tipo, d.motivo))

    def dependencias(self, workspace_id: str) -> list[Dependency]:
        return [Dependency(task_id=r["task_id"], depende_de=r["depende_de"],
                           tipo=r["tipo"], motivo=r["motivo"])
                for r in self._con.execute(
                    """SELECT d.* FROM deps d JOIN tasks t ON t.id = d.task_id
                       WHERE t.workspace_id=?""", (workspace_id,))]

    # ---- runs ------------------------------------------------------------

    def _run(self, r: sqlite3.Row) -> Run:
        return Run(id=r["id"], task_id=r["task_id"], workspace_id=r["workspace_id"],
                   agente=r["agente"], estado=RunState(r["estado"]), worker=r["worker"],
                   workspace_path=r["workspace_path"], branch=r["branch"],
                   iniciado_em=_dt(r["iniciado_em"]), encerrado_em=_dt(r["encerrado_em"]),
                   motivo=r["motivo"], custo_usd=r["custo_usd"], tokens=r["tokens"],
                   chamadas_tool=r["chamadas_tool"], iteracoes=r["iteracoes"],
                   dados=json.loads(r["dados"]))

    def salva_run(self, r: Run) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO runs(id, task_id, workspace_id, agente, estado, worker,
                           workspace_path, branch, iniciado_em, encerrado_em, motivo,
                           custo_usd, tokens, chamadas_tool, iteracoes, dados)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET estado=excluded.estado,
                           worker=excluded.worker, workspace_path=excluded.workspace_path,
                           branch=excluded.branch, encerrado_em=excluded.encerrado_em,
                           motivo=excluded.motivo, custo_usd=excluded.custo_usd,
                           tokens=excluded.tokens, chamadas_tool=excluded.chamadas_tool,
                           iteracoes=excluded.iteracoes, dados=excluded.dados""",
                      (r.id, r.task_id, r.workspace_id, r.agente, r.estado.value, r.worker,
                       r.workspace_path, r.branch, _iso(r.iniciado_em), _iso(r.encerrado_em),
                       r.motivo, r.custo_usd, r.tokens, r.chamadas_tool, r.iteracoes,
                       _j(r.dados)))

    def run(self, run_id: str) -> Run | None:
        r = self._con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._run(r) if r else None

    def runs_ativos(self, workspace_id: str) -> list[Run]:
        return [self._run(r) for r in self._con.execute(
            "SELECT * FROM runs WHERE workspace_id=? AND estado=? ORDER BY iniciado_em",
            (workspace_id, RunState.RUNNING.value))]

    def runs_da_task(self, task_id: str) -> list[Run]:
        return [self._run(r) for r in self._con.execute(
            "SELECT * FROM runs WHERE task_id=? ORDER BY iniciado_em", (task_id,))]

    # ---- trilha ----------------------------------------------------------

    def anota(self, e: Event) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO eventos(id, workspace_id, ts, tipo, task_id, run_id,
                           ator, resumo, dados) VALUES(?,?,?,?,?,?,?,?,?)""",
                      (e.id, e.workspace_id, _iso(e.ts), e.tipo, e.task_id, e.run_id,
                       e.ator, e.resumo, _j(e.dados)))

    def eventos(self, workspace_id: str, task_id: str | None = None,
                limite: int = 100) -> list[Event]:
        if task_id:
            q = "SELECT * FROM eventos WHERE workspace_id=? AND task_id=? ORDER BY ts DESC LIMIT ?"
            args: list[Any] = [workspace_id, task_id, limite]
        else:
            q = "SELECT * FROM eventos WHERE workspace_id=? ORDER BY ts DESC LIMIT ?"
            args = [workspace_id, limite]
        return [Event(id=r["id"], workspace_id=r["workspace_id"], tipo=r["tipo"],
                      ts=_dt(r["ts"]), task_id=r["task_id"], run_id=r["run_id"],
                      ator=r["ator"], resumo=r["resumo"], dados=json.loads(r["dados"]))
                for r in self._con.execute(q, args)]

    def registra_acao(self, a: ActionRecord) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO acoes(id, workspace_id, ts, agente, acao, recurso, efeito,
                           risco, task_id, run_id, regra, motivo, resultado, duracao_ms,
                           custo_usd, tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (a.id, a.workspace_id, _iso(a.ts), a.agente, a.acao, a.recurso,
                       a.efeito, a.risco, a.task_id, a.run_id, a.regra, a.motivo,
                       a.resultado, a.duracao_ms, a.custo_usd, a.tokens))

    def acoes(self, workspace_id: str, limite: int = 100) -> list[ActionRecord]:
        return [ActionRecord(id=r["id"], workspace_id=r["workspace_id"], agente=r["agente"],
                             acao=r["acao"], recurso=r["recurso"], efeito=r["efeito"],
                             risco=r["risco"], ts=_dt(r["ts"]), task_id=r["task_id"],
                             run_id=r["run_id"], regra=r["regra"], motivo=r["motivo"],
                             resultado=r["resultado"], duracao_ms=r["duracao_ms"],
                             custo_usd=r["custo_usd"], tokens=r["tokens"])
                for r in self._con.execute(
                    "SELECT * FROM acoes WHERE workspace_id=? ORDER BY ts DESC LIMIT ?",
                    (workspace_id, limite))]

    # ---- approvals -------------------------------------------------------

    def _approval(self, r: sqlite3.Row) -> Approval:
        return Approval(
            id=r["id"], workspace_id=r["workspace_id"], task_id=r["task_id"],
            o_que_aconteceu=r["o_que_aconteceu"], por_que_importa=r["por_que_importa"],
            o_que_o_agente_tentou=tuple(json.loads(r["tentativas"])),
            opcoes=tuple(Option(**o) for o in json.loads(r["opcoes"])),
            recomendacao=r["recomendacao"], risco=RiskLevel[r["risco"]],
            estado=ApprovalState(r["estado"]), run_id=r["run_id"],
            criada_em=_dt(r["criada_em"]), decidida_em=_dt(r["decidida_em"]),
            decidida_por=r["decidida_por"], escolha=r["escolha"], nota=r["nota"])

    def abre_approval(self, a: Approval) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO approvals(id, workspace_id, task_id, run_id, estado, risco,
                           o_que_aconteceu, por_que_importa, tentativas, opcoes, recomendacao,
                           criada_em) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (a.id, a.workspace_id, a.task_id, a.run_id, a.estado.value, a.risco.name,
                       a.o_que_aconteceu, a.por_que_importa, _j(list(a.o_que_o_agente_tentou)),
                       _j([{"id": o.id, "rotulo": o.rotulo, "efeito": o.efeito} for o in a.opcoes]),
                       a.recomendacao, _iso(a.criada_em)))
            c.execute("""INSERT INTO eventos(id, workspace_id, ts, tipo, task_id, run_id,
                           ator, resumo, dados) VALUES(?,?,?,?,?,?,?,?,?)""",
                      (ids.novo(ids.EVENT), a.workspace_id, _iso(agora()), "escalou",
                       a.task_id, a.run_id, "engine", a.o_que_aconteceu,
                       _j({"approval_id": a.id, "risco": a.risco.name})))

    def approvals_abertos(self, workspace_id: str) -> list[Approval]:
        return [self._approval(r) for r in self._con.execute(
            "SELECT * FROM approvals WHERE workspace_id=? AND estado=? ORDER BY risco DESC, criada_em",
            (workspace_id, ApprovalState.OPEN.value))]

    def approval(self, approval_id: str) -> Approval | None:
        r = self._con.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return self._approval(r) if r else None

    def decide_approval(self, approval_id: str, escolha: str, por: str, nota: str = "") -> Approval:
        with self._tx() as c:
            r = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if r is None:
                raise EstadoCorrompido(f"approval {approval_id} nao existe")
            a = self._approval(r)
            if a.estado is not ApprovalState.OPEN:
                raise EstadoCorrompido(f"approval {approval_id} ja foi decidido")
            validas = {o.id for o in a.opcoes}
            if validas and escolha not in validas:
                raise EstadoCorrompido(
                    f"escolha '{escolha}' nao esta entre as opcoes: {', '.join(sorted(validas))}")
            a.estado, a.escolha, a.decidida_por = ApprovalState.DECIDED, escolha, por
            a.decidida_em, a.nota = agora(), nota
            c.execute("""UPDATE approvals SET estado=?, escolha=?, decidida_por=?,
                           decidida_em=?, nota=? WHERE id=?""",
                      (a.estado.value, escolha, por, _iso(a.decidida_em), nota, approval_id))
            c.execute("""INSERT INTO eventos(id, workspace_id, ts, tipo, task_id, run_id,
                           ator, resumo, dados) VALUES(?,?,?,?,?,?,?,?,?)""",
                      (ids.novo(ids.EVENT), a.workspace_id, _iso(agora()), "decisao_humana",
                       a.task_id, a.run_id, por, f"escolheu '{escolha}'",
                       _j({"approval_id": approval_id, "nota": nota})))
        return a

    # ---- leases ----------------------------------------------------------

    def adquire_lease(self, recurso: str, dono: str, workspace_id: str,
                      segundos: int) -> Lease | None:
        """Concede se livre, vencido, ou ja do mesmo dono (renovacao)."""
        ts = agora()
        expira = ts + timedelta(seconds=segundos)
        with self._tx() as c:
            r = c.execute("SELECT * FROM leases WHERE workspace_id=? AND recurso=?",
                          (workspace_id, recurso)).fetchone()
            if r is not None:
                vivo = _dt(r["expira_em"]) > ts
                if vivo and r["dono"] != dono:
                    return None
            c.execute("""INSERT INTO leases(workspace_id, recurso, dono, expira_em, renovado_em)
                         VALUES(?,?,?,?,?)
                         ON CONFLICT(workspace_id, recurso) DO UPDATE SET
                           dono=excluded.dono, expira_em=excluded.expira_em,
                           renovado_em=excluded.renovado_em""",
                      (workspace_id, recurso, dono, _iso(expira), _iso(ts)))
        return Lease(recurso=recurso, dono=dono, expira_em=expira,
                     workspace_id=workspace_id, renovado_em=ts)

    def renova_lease(self, recurso: str, dono: str, segundos: int,
                     workspace_id: str | None = None) -> bool:
        ts = agora()
        with self._tx() as c:
            if workspace_id:
                cur = c.execute("""UPDATE leases SET expira_em=?, renovado_em=?
                                   WHERE workspace_id=? AND recurso=? AND dono=?""",
                                (_iso(ts + timedelta(seconds=segundos)), _iso(ts),
                                 workspace_id, recurso, dono))
            else:
                # Sem workspace, o dono do lease e o filtro. `dono` e um id de run,
                # que ja e unico -- entao isto continua seguro, so menos explicito.
                cur = c.execute("""UPDATE leases SET expira_em=?, renovado_em=?
                                   WHERE recurso=? AND dono=?""",
                                (_iso(ts + timedelta(seconds=segundos)), _iso(ts),
                                 recurso, dono))
            return cur.rowcount > 0

    def solta_lease(self, recurso: str, dono: str, workspace_id: str | None = None) -> None:
        with self._tx() as c:
            if workspace_id:
                c.execute("DELETE FROM leases WHERE workspace_id=? AND recurso=? AND dono=?",
                          (workspace_id, recurso, dono))
            else:
                c.execute("DELETE FROM leases WHERE recurso=? AND dono=?", (recurso, dono))

    def leases_vencidos(self, workspace_id: str, quando: datetime | None = None) -> list[Lease]:
        ts = quando or agora()
        return [Lease(recurso=r["recurso"], dono=r["dono"], expira_em=_dt(r["expira_em"]),
                      workspace_id=r["workspace_id"], renovado_em=_dt(r["renovado_em"]))
                for r in self._con.execute(
                    "SELECT * FROM leases WHERE workspace_id=? AND expira_em < ?",
                    (workspace_id, _iso(ts)))]

    # ---- contadores ------------------------------------------------------

    def conta_despachos(self, workspace_id: str, dia: str) -> int:
        r = self._con.execute(
            "SELECT valor FROM contadores WHERE workspace_id=? AND dia=? AND nome='despachos'",
            (workspace_id, dia)).fetchone()
        return r["valor"] if r else 0

    def marca_despacho(self, workspace_id: str, dia: str) -> None:
        with self._tx() as c:
            c.execute("""INSERT INTO contadores(workspace_id, dia, nome, valor) VALUES(?,?,'despachos',1)
                         ON CONFLICT(workspace_id, dia, nome) DO UPDATE SET valor = valor + 1""",
                      (workspace_id, dia))

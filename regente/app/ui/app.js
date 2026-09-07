// Mission Control — a tela.
//
// Uma regra governa este arquivo: **nada aqui decide**. Nao existe funcao que
// conclua que algo esta bloqueado, que um CI passou, que um lease venceu ou que
// uma task esta presa. Toda conclusao chega pronta da API, calculada por quem
// tem autoridade para calcula-la. O que este codigo faz e escolher onde por na
// tela o que ja veio decidido.
//
// A segunda regra e sobre ausencia. Um campo vazio numa tela le-se como "esta
// tudo bem". Entao ausencia aqui e sempre escrita: "sem pull request", "CI nao
// observado", "nenhum motivo registrado". Nunca um espaco em branco.

const REFRESH_MS = 5000;

// O segredo desta sessao, colocado pelo servidor nesta pagina. A tela nunca o
// escolhe, nunca o guarda e nunca diz quem e -- ela apresenta um segredo, e
// quem decide o que ele prova e o servidor.
const SESSION = (document.querySelector('meta[name="regente-session"]') || {})
  .content || "";

const state = {
  workspaces: [],
  workspace: null,
  // O que ESTA identidade recebeu, por workspace. Vem do servidor a cada
  // leitura. A tela usa isto para nao oferecer o que nao adianta -- e isso e
  // UX. A barreira continua sendo a API: se o botao aparecer por engano e a
  // pessoa clicar, a resposta e 403, e esta certo assim.
  identity: null,
  route: { page: "overview", args: [] },
  timer: null,
  lastRead: null,
};

// ---------------------------------------------------------------------------
// utilidades de renderizacao
// ---------------------------------------------------------------------------

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/** Texto, ou a ausencia dita em voz alta. Nunca vazio. */
const said = (v, absent = "nao registrado") =>
  v === null || v === undefined || v === "" ? `<em class="dim">${esc(absent)}</em>`
                                            : esc(v);

const when = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? esc(iso) : d.toLocaleString();
};

function headers(extra) {
  const h = { Accept: "application/json", ...(extra || {}) };
  if (SESSION) h.Authorization = `Bearer ${SESSION}`;
  return h;
}

async function get(path) {
  const r = await fetch(path, { headers: headers() });
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch { /* corpo nao-JSON */ }
    throw new Error(`${r.status} — ${detail}`);
  }
  return r.json();
}

/**
 * A unica escrita da tela.
 *
 * Devolve o corpo E o status, sempre -- inclusive na recusa. A tela precisa
 * distinguir "nao autenticado" de "outro ja decidiu" de "a policy proibiu",
 * porque cada um manda a pessoa fazer uma coisa diferente. Um "deu erro"
 * generico manda ela para o terminal, que era o que a tela deveria evitar.
 */
async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  let payload = null;
  try { payload = await r.json(); } catch { /* corpo nao-JSON */ }
  return { ok: r.ok, status: r.status, payload: payload || {} };
}

const ws = () => state.workspace;

/** O que o SERVIDOR disse que esta identidade pode aqui. Nunca uma deducao. */
const podeAqui = (ability) =>
  (((state.identity || {}).abilities || {})[ws()] || []).includes(ability);
const api = (suffix) => `/api/workspaces/${encodeURIComponent(ws())}${suffix}`;

// ---------------------------------------------------------------------------
// pecas reutilizadas
// ---------------------------------------------------------------------------

/**
 * Um estado, com nome, idade e dono — nunca so uma cor.
 *
 * `owner` vem do read model, que o deriva da maquina de estados. A tela nao
 * sabe (e nao deve saber) quais estados o motor consegue avancar sozinho.
 */
function stateChip(s) {
  return `<span class="state" data-owner="${esc(s.owner)}" title="${esc(s.meaning)}">
    <span class="name">${esc(s.name)}</span>
    <span class="age">${esc(s.age)}</span>
  </span>`;
}

function stateBlock(s) {
  return `<div class="state-block" data-owner="${esc(s.owner)}">
    <div class="big">${esc(s.name)}</div>
    <div class="sub" style="margin:4px 0 0">${said(s.meaning, "sem descricao")}</div>
    <dl>
      <dt>motivo</dt><dd>${said(s.reason, "nenhum motivo registrado")}</dd>
      <dt>desde</dt><dd>${when(s.since)}</dd>
      <dt>ha</dt><dd>${esc(s.age)}</dd>
      <dt>quem move</dt><dd class="owner">${esc(s.owner)}</dd>
      <dt>proximo passo</dt><dd>${said(s.next_action, "nenhum passo automatico")}</dd>
    </dl>
  </div>`;
}

function blockers(list) {
  if (!list || !list.length) {
    return `<p class="empty">nenhum bloqueio registrado</p>`;
  }
  return list.map((b) => `<div class="blocker" data-kind="${esc(b.kind)}">
      <div class="kind">${esc(b.kind)}</div>
      <div>${said(b.summary)}</div>
      <div class="detail">${said(b.detail, "nenhum detalhe registrado")}</div>
      ${b.since ? `<div class="detail">desde ${when(b.since)}</div>` : ""}
      ${b.action ? `<div class="act">→ ${esc(b.action)}</div>` : ""}
    </div>`).join("");
}

/**
 * A cadeia de entrega, sem etapa inventada.
 *
 * Cada elo le um campo que a API preencheu a partir de uma coluna escrita. Um
 * elo ausente aparece tracejado, dizendo que esta ausente. O elo de CI so fica
 * verde quando a API mandou `ci_green` — a tela nao interpreta estado de CI.
 */
function chain(d) {
  const links = [
    { step: "task", val: d.task_key, done: !!d.task_key },
    { step: "run", val: d.run_id, done: !!d.run_id },
    { step: "commit", val: (d.commit_sha || "").slice(0, 12), done: !!d.commit_sha },
    {
      step: "push",
      val: d.pushed ? (d.push_target || "empurrado") : "nao empurrado",
      done: d.pushed, absent: !d.pushed,
    },
    {
      step: "pull request",
      val: d.pull_request ? `#${d.pull_request}` : d.pull_request_absent,
      done: !!d.pull_request, absent: !d.pull_request,
      href: d.pull_request_url,
    },
    {
      step: "ci",
      val: d.ci_state === "NOT_OBSERVED"
        ? "nao observado"
        : d.ci_state + (d.ci_result ? ` / ${d.ci_result}` : ""),
      done: d.ci_green,
      absent: d.ci_state === "NOT_OBSERVED",
      waiting: d.ci_state === "PENDING",
      bad: d.ci_state === "CONCLUDED" && !d.ci_green,
    },
  ];
  return `<div class="chain">${links.map((l) => {
    const cls = ["link", l.done ? "done" : "", l.absent ? "absent" : "",
                 l.waiting ? "waiting" : "", l.bad ? "bad" : ""]
      .filter(Boolean).join(" ");
    const body = l.href
      ? `<a href="${esc(l.href)}" target="_blank" rel="noreferrer">${said(l.val)}</a>`
      : said(l.val);
    return `<div class="${cls}"><div class="step">${esc(l.step)}</div>
              <div class="val">${body}</div></div>`;
  }).join("")}</div>
  ${d.ci_reason ? `<p class="sub" style="margin:6px 0 0">ci: ${esc(d.ci_reason)}</p>` : ""}`;
}

function timeline(entries) {
  if (!entries || !entries.length) {
    return `<p class="empty">nenhum evento gravado para isto</p>`;
  }
  return `<ul class="timeline">${entries.map((e) => `<li>
      <div class="when">${when(e.at)} · ${esc(e.actor)}</div>
      <div class="what">${esc(e.kind)}</div>
      <div class="said">${said(e.summary, "sem resumo")}</div>
    </li>`).join("")}</ul>`;
}

function taskTable(rows, opts = {}) {
  if (!rows.length) return `<p class="empty">${esc(opts.empty || "nenhuma task")}</p>`;
  return `<table><thead><tr>
      <th>task</th><th>titulo</th><th>estado</th><th>ha</th><th>quem move</th>
    </tr></thead><tbody>${rows.map((t) => `<tr>
      <td class="key"><a href="#/task/${encodeURIComponent(t.id)}">${esc(t.key)}</a></td>
      <td>${said(t.title, "sem titulo")}</td>
      <td>${stateChip(t.state)}</td>
      <td class="dim">${esc(t.state.age)}</td>
      <td class="dim owner">${esc(t.state.owner)}</td>
    </tr>`).join("")}</tbody></table>`;
}

// ---------------------------------------------------------------------------
// telas
// ---------------------------------------------------------------------------

const pages = {};

pages.overview = async () => {
  const [o, tasks] = await Promise.all([get(api("/overview")), get(api("/tasks"))]);
  const rows = tasks.tasks;

  // Agrupamento por DONO do proximo passo, nao por aparencia. As quatro
  // categorias sao semanticamente diferentes e ficam separadas: misturar
  // "precisa de mim" com "esperando o CI" faz a fila humana mentir.
  const byOwner = (owner) => rows.filter((t) => t.state.owner === owner);
  const humans = byOwner("human");
  const stuck = byOwner("nobody").filter((t) => t.state.next_action);
  const waiting = byOwner("external");
  const blocked = rows.filter((t) => t.blocked);

  return `
    <div class="banner" data-l="${esc(o.health.level)}">
      <span class="word">${esc(o.health.level)}</span>
      <span class="dim">${esc(o.workspace_name)} · cliente ${esc(o.client)}</span>
      <span class="dim">ultimo tick ${esc(o.last_tick_age)}</span>
      <span class="dim">ultima atividade ${esc(o.last_activity_age)}</span>
      <a href="#/health" style="margin-left:auto">ver saude →</a>
    </div>

    <h2>estado operacional</h2>
    <div class="grid wide">
      ${o.tasks_by_state.map((c) => `<div class="panel tile">
          <div class="n">${c.count}</div><div class="l">${esc(c.name)}</div>
        </div>`).join("") || `<p class="empty">nenhuma task neste workspace</p>`}
    </div>

    <div class="grid wide" style="margin-top:12px">
      <div class="panel tile"><div class="n">${o.active_runs}</div>
        <div class="l">runs ativos</div>
        <div class="why">${o.workers} worker(s) identificado(s)</div></div>
      <div class="panel tile"><div class="n">${o.leases_live}</div>
        <div class="l">leases vivos</div>
        <div class="why">${o.leases_expired} vencido(s)</div></div>
      <div class="panel tile"><div class="n">${o.deliveries_in_flight}</div>
        <div class="l">entregas em voo</div>
        <div class="why">esperando sistema de fora</div></div>
      <div class="panel tile"><div class="n">${o.needs_human}</div>
        <div class="l">precisam de voce</div>
        <div class="why">decisoes na fila</div></div>
    </div>

    <h2>precisa de atencao</h2>
    <div class="grid two">
      <div class="panel">
        <h2 style="margin-top:0">precisa de voce</h2>
        ${taskTable(humans, { empty: "nenhuma decisao pendente" })}
      </div>
      <div class="panel">
        <h2 style="margin-top:0">bloqueadas</h2>
        ${taskTable(blocked, { empty: "nenhuma task bloqueada" })}
      </div>
      <div class="panel">
        <h2 style="margin-top:0">sem rota de saida</h2>
        ${taskTable(stuck, { empty: "nenhuma task estacionada" })}
      </div>
      <div class="panel">
        <h2 style="margin-top:0">aguardando sistema externo</h2>
        ${taskTable(waiting, { empty: "nada aguardando fora" })}
      </div>
    </div>

    <h2>atividade recente</h2>
    <div class="panel">${timeline((await get(api("/events?limit=12"))).events)}</div>`;
};

pages.tasks = async (filter) => {
  const q = filter ? `?state=${encodeURIComponent(filter)}` : "";
  const [all, filtered] = await Promise.all([
    get(api("/tasks")), filter ? get(api(`/tasks${q}`)) : null,
  ]);
  const rows = filtered ? filtered.tasks : all.tasks;
  const states = [...new Set(all.tasks.map((t) => t.state.name))].sort();

  return `
    <h1>tasks</h1>
    <p class="sub">${rows.length} de ${all.tasks.length}</p>
    <div class="filters">
      <a href="#/tasks" class="${filter ? "" : "on"}">todas</a>
      ${states.map((s) => `<a href="#/tasks/${encodeURIComponent(s)}"
          class="${filter === s ? "on" : ""}">${esc(s)}</a>`).join("")}
    </div>
    <div class="panel">${taskTable(rows)}</div>`;
};

pages.task = async (id) => {
  const d = await get(api(`/tasks/${encodeURIComponent(id)}`));
  const runLine = (r, label) => r
    ? `<dt>${label}</dt><dd><a href="#/run/${encodeURIComponent(r.id)}">${esc(r.id)}</a>
         · ${esc(r.state)} · ${esc(r.duration)} · ${esc(r.agent)}</dd>`
    : `<dt>${label}</dt><dd><em class="dim">nenhum</em></dd>`;

  return `
    <div class="crumb"><a href="#/tasks">tasks</a> / ${esc(d.card.key)}</div>
    <h1>${esc(d.card.key)} — ${said(d.card.title, "sem titulo")}</h1>
    <p class="sub">${esc(d.card.workspace_name)} · cliente ${esc(d.card.client)}
       ${d.card.repository ? ` · ${esc(d.card.repository)}` : ""}</p>

    <div class="panel">${stateBlock(d.card.state)}</div>

    <h2>bloqueios</h2>
    ${blockers(d.blockers)}

    <h2>execucao</h2>
    <div class="panel"><div class="state-block" style="border-left-color:var(--line)">
      <dl>
        ${runLine(d.current_run, "run atual")}
        ${runLine(d.last_run, "ultimo run")}
        <dt>ultimo resultado</dt><dd>${said(d.last_result, "nenhum resultado registrado")}</dd>
        <dt>tentativas</dt><dd>${esc(d.card.attempts)}</dd>
        <dt>estado anterior</dt><dd>${said(d.previous_state, "nao pausou")}</dd>
      </dl>
    </div></div>

    <h2>alvo</h2>
    <div class="panel">${d.target ? `
      <div class="state-block" style="border-left-color:var(--engine)">
        <div class="big">${esc(d.target.repository)}</div>
        <dl>
          <dt>origem</dt><dd>${esc(d.target.source)}</dd>
          <dt>confianca</dt><dd>${esc(d.target.confidence)}</dd>
          <dt>validado</dt><dd>${d.target.validated ? "sim, por execucao real" : "nao"}</dd>
          <dt>confirmacoes</dt><dd>${esc(d.target.confirmations)}</dd>
          <dt>evidencia</dt><dd>${d.target.evidence.length
            ? d.target.evidence.map((e) => esc(e)).join("<br>")
            : `<em class="dim">nenhuma evidencia gravada</em>`}</dd>
          <dt>alternativas</dt><dd>${d.target.alternatives.length
            ? d.target.alternatives.map((a) => esc(a)).join("<br>")
            : `<em class="dim">nenhuma</em>`}</dd>
        </dl>
      </div>` : `<p class="empty">nenhum alvo resolvido para esta task</p>`}
    </div>

    <h2>entrega</h2>
    <div class="panel">${d.deliveries.length
      ? d.deliveries.map(chain).join("<hr style='border:0;border-top:1px solid var(--line);margin:16px 0'>")
      : `<p class="empty">nada foi entregue por esta task</p>`}</div>

    ${d.escalation ? `<h2>na fila humana</h2>
      <div class="panel">${escalationCard(d.escalation)}</div>` : ""}

    <h2>linha do tempo</h2>
    <div class="panel">${timeline(d.timeline)}</div>`;
};

pages.runs = async () => {
  const { runs } = await get(api("/runs?limit=100"));
  if (!runs.length) return `<h1>runs</h1><p class="empty">nenhum run registrado</p>`;
  return `<h1>runs</h1><p class="sub">${runs.length} mais recentes</p>
    <div class="panel"><table><thead><tr>
      <th>run</th><th>task</th><th>agente</th><th>estado</th>
      <th>duracao</th><th>motivo</th>
    </tr></thead><tbody>${runs.map((r) => `<tr>
      <td class="key"><a href="#/run/${encodeURIComponent(r.id)}">${esc(r.id)}</a></td>
      <td>${said(r.task_key)}</td>
      <td class="dim">${esc(r.agent)}</td>
      <td>${esc(r.state)}</td>
      <td class="dim">${esc(r.duration)}</td>
      <td class="dim">${said(r.reason, "—")}</td>
    </tr>`).join("")}</tbody></table></div>`;
};

pages.run = async (id) => {
  const d = await get(api(`/runs/${encodeURIComponent(id)}`));
  const r = d.run;
  return `
    <div class="crumb"><a href="#/runs">runs</a> / ${esc(r.id)}</div>
    <h1>${esc(r.id)}</h1>
    <p class="sub">${esc(d.workspace_name)} · cliente ${esc(r.client)}</p>

    <div class="panel"><div class="state-block" style="border-left-color:var(--engine)">
      <div class="big">${esc(r.state)}</div>
      <dl>
        <dt>task</dt><dd>${r.task_id
          ? `<a href="#/task/${encodeURIComponent(r.task_id)}">${esc(r.task_key)}</a>`
          : said(r.task_key, "sem task guardada")}</dd>
        <dt>agente</dt><dd>${esc(r.agent)}</dd>
        <dt>repositorio</dt><dd>${said(d.repository, "nao registrado")}</dd>
        <dt>branch</dt><dd>${said(r.branch, "nenhuma")}</dd>
        <dt>area</dt><dd><code class="mono">${said(d.workspace_path, "nenhuma")}</code></dd>
        <dt>inicio</dt><dd>${when(r.started_at)}</dd>
        <dt>fim</dt><dd>${r.ended_at ? when(r.ended_at) : "<em class='dim'>em andamento</em>"}</dd>
        <dt>duracao</dt><dd>${esc(r.duration)}</dd>
        <dt>iteracoes</dt><dd>${esc(d.iterations)} · ${esc(d.tool_calls)} tool call(s)</dd>
        <dt>custo</dt><dd>US$ ${esc(r.cost_usd.toFixed(4))} · ${esc(r.tokens)} tokens</dd>
        <dt>posse agora</dt><dd>${d.leases.length
          ? d.leases.map((l) => `<code class="mono">${esc(l)}</code>`).join("<br>")
          : `<em class="dim">nenhum lease vivo</em>`}</dd>
        <dt>motivo</dt><dd>${said(r.reason, "nenhum motivo registrado")}</dd>
      </dl>
    </div></div>

    <h2>bloqueios</h2>
    ${blockers(d.blockers)}

    <h2>entrega</h2>
    <div class="panel">${d.delivery ? chain(d.delivery)
      : `<p class="empty">este run nao abriu entrega</p>`}</div>

    <h2>eventos deste run</h2>
    <div class="panel">${timeline(d.timeline)}</div>`;
};

/**
 * Um item da fila humana, com as opcoes REAIS que o motor ofereceu.
 *
 * As opcoes vem da API. Nao ha lista fixa aqui: um botao que a tela inventa e
 * uma acao que o Core nunca prometeu aceitar, e a pessoa so descobre isso
 * depois de clicar.
 */
function escalationCard(e) {
  const buttons = e.options.map((o) => `
    <button class="choice" data-approval="${esc(e.id)}" data-choice="${esc(o.id)}"
      title="${esc(o.effect)}">${esc(o.label)}${
        e.recommendation === o.id ? " ★" : ""}</button>`).join("");

  return `<div class="blocker" data-kind="HUMAN" data-approval="${esc(e.id)}">
    <div class="kind">${esc(e.task_key)} · risco ${esc(e.risk)} · esperando ${esc(e.waiting)}</div>
    <div>${said(e.what_happened)}</div>
    <div class="detail">${said(e.why_it_matters, "sem justificativa registrada")}</div>
    ${e.what_was_tried.length ? `<div class="detail">tentado:<br>${
      e.what_was_tried.map((t) => esc(t)).join("<br>")}</div>` : ""}
    ${buttons ? `<div class="choices">${buttons}
      ${e.recommendation ? `<span class="hint">★ recomendada pelo motor</span>` : ""}
    </div>` : `<p class="empty">esta escalada nao ofereceu opcoes</p>`}
    <div class="outcome" data-for="${esc(e.id)}"></div>
  </div>`;
}

/**
 * Envia a decisao e RELE o estado.
 *
 * `200` significa que o servidor aceitou, e nao que a tela sabe o que ficou
 * gravado. A resposta da escrita nao vira segunda fonte de verdade: o que a
 * pessoa passa a ver vem da leitura seguinte.
 */
async function decide(approvalId, choice, slot) {
  slot.className = "outcome working";
  slot.textContent = "enviando…";

  const r = await post(
    `/api/workspaces/${encodeURIComponent(ws())}/approvals/${
      encodeURIComponent(approvalId)}/decision`, { choice });

  if (!r.ok) {
    // Cada recusa manda a pessoa fazer uma coisa diferente.
    const says = {
      401: "esta sessao nao esta autenticada; reabra a Mission Control",
      403: "esta sessao nao tem autoridade para decidir aqui",
      404: "esta escalada nao existe neste escopo",
      409: "alguem decidiu antes; nada do que voce escolheu foi perdido",
      422: "esta escolha nao esta entre as opcoes oferecidas",
    }[r.status] || r.payload.detail || "a decisao foi recusada";
    slot.className = "outcome bad";
    slot.textContent = `${r.status} · ${says}`;
    await render();             // relemos mesmo na recusa: o mundo pode ter mudado
    return;
  }

  slot.className = "outcome good";
  slot.textContent = "registrada; relendo o estado…";
  await render();
}

document.addEventListener("click", async (ev) => {
  const revogar = ev.target.closest("button.choice.revoke");
  if (revogar) {
    const slot = document.querySelector('.outcome[data-for="grant"]')
      || document.createElement("div");
    const r = await fetch(
      `/api/workspaces/${encodeURIComponent(ws())}/access/${
        encodeURIComponent(revogar.dataset.principal)}`,
      { method: "DELETE", headers: headers() });
    slot.className = r.ok ? "outcome good" : "outcome bad";
    slot.textContent = r.ok ? "revogado; relendo…" : `${r.status} · recusado`;
    await render();
    return;
  }

  const revogarCred = ev.target.closest("button.choice.revoke-cred");
  if (revogarCred) {
    const slot = document.querySelector('.outcome[data-for="cred"]')
      || document.createElement("div");
    const r = await fetch(
      `/api/workspaces/${encodeURIComponent(ws())}/credentials/${
        encodeURIComponent(revogarCred.dataset.id)}`,
      { method: "DELETE", headers: headers() });
    slot.className = r.ok ? "outcome good" : "outcome bad";
    slot.textContent = r.ok ? "revogada; relendo…" : `${r.status} · recusado`;
    await render();
    return;
  }

  if (ev.target.id === "registrar-cred") {
    const slot = document.querySelector('.outcome[data-for="cred"]');
    const dias = parseInt(document.getElementById("cred-dias").value, 10);
    const r = await post(`/api/workspaces/${encodeURIComponent(ws())}/credentials`, {
      name: document.getElementById("cred-nome").value.trim(),
      provider: document.getElementById("cred-provider").value.trim(),
      secret_ref: document.getElementById("cred-ref").value.trim(),
      capabilities: document.getElementById("cred-caps").value
        .split(",").map((x) => x.trim()).filter(Boolean),
      expires_in_days: Number.isFinite(dias) ? dias : 0,
    });
    const diz = {
      401: "esta sessao nao esta autenticada",
      403: "esta identidade nao administra credenciais aqui, ou a policy recusou",
      409: "ja existe credencial viva para este provider e nome",
      422: "referencia ou capacidades invalidas",
    }[r.status] || r.payload.detail || "recusado";
    slot.className = r.ok ? "outcome good" : "outcome bad";
    slot.textContent = r.ok ? "registrada; relendo…" : `${r.status} · ${diz}`;
    await render();
    return;
  }

  if (ev.target.id === "conceder") {
    const slot = document.querySelector('.outcome[data-for="grant"]');
    const r = await post(`/api/workspaces/${encodeURIComponent(ws())}/access`, {
      principal: document.getElementById("alvo").value.trim(),
      role: document.getElementById("papel").value,
      note: document.getElementById("nota").value,
    });
    const diz = {
      401: "esta sessao nao esta autenticada",
      403: "esta identidade nao pode conceder aqui, ou a policy recusou",
      404: "workspace nao encontrado neste escopo",
      409: "esta pessoa ja tem concessao viva; revogue antes",
      422: "identidade ou papel invalido",
    }[r.status] || r.payload.detail || "recusado";
    slot.className = r.ok ? "outcome good" : "outcome bad";
    slot.textContent = r.ok ? "concedido; relendo…" : `${r.status} · ${diz}`;
    await render();
    return;
  }

  const button = ev.target.closest("button.choice");
  if (!button) return;
  const slot = document.querySelector(
    `.outcome[data-for="${CSS.escape(button.dataset.approval)}"]`);
  // Desabilitar e apresentacao, nao seguranca: o servidor recusa a segunda
  // decisao por conta propria, e e isso que impede duas abas de decidirem.
  button.closest(".blocker").querySelectorAll("button.choice")
    .forEach((b) => { b.disabled = true; });
  decide(button.dataset.approval, button.dataset.choice, slot);
});

pages.needs = async () => {
  const [{ escalations }, { tasks }, { events }] = await Promise.all([
    get(api("/escalations")), get(api("/tasks")), get(api("/events?limit=60")),
  ]);
  // Quem decidiu e quando, lido da trilha do motor -- nao do que o navegador
  // acabou de enviar. O que a tela mostra e sempre o que ficou gravado.
  const decided = events.filter((e) => e.kind === "decisao_humana_autenticada");
  const stuck = tasks.filter((t) => t.state.owner === "nobody" && t.state.next_action);
  const waiting = tasks.filter((t) => t.state.owner === "external");
  const blocked = tasks.filter((t) => t.blocked);

  return `
    <h1>precisa de atencao</h1>
    <p class="sub">quatro categorias, mantidas separadas: uma decisao humana nao
       e um bloqueio, e nenhuma das duas e uma espera.</p>

    <h2>precisa de voce — decisao humana</h2>
    <div class="panel">${escalations.length
      ? escalations.map(escalationCard).join("")
      : `<p class="empty">nenhuma decisao na fila</p>`}</div>
    ${decided.length ? `<h2>decididas recentemente</h2>
      <div class="panel">${decided.map((e) => `<div class="blocker" data-kind="DONE">
        <div class="kind">${esc(e.summary)}</div>
        <div class="detail">${when(e.at)} · por ${esc(e.actor)}</div>
      </div>`).join("")}</div>` : ""}

    <h2>bloqueado — algo impede, e nao e voce</h2>
    <div class="panel">${taskTable(blocked, { empty: "nada bloqueado" })}</div>

    <h2>sem rota — o motor nao tem etapa que avance daqui</h2>
    <div class="panel">${taskTable(stuck, { empty: "nenhuma task estacionada" })}</div>

    <h2>aguardando — sistema de fora ainda nao respondeu</h2>
    <div class="panel">${taskTable(waiting, { empty: "nada aguardando" })}</div>`;
};

pages.deliveries = async () => {
  const { deliveries } = await get(api("/deliveries"));
  if (!deliveries.length) {
    return `<h1>entregas</h1><p class="empty">nenhuma entrega registrada</p>`;
  }
  return `<h1>entregas</h1><p class="sub">${deliveries.length} registrada(s)</p>
    ${deliveries.map((d) => `<div class="panel">
      <div class="sub" style="margin:0 0 8px">${esc(d.repository)} · ${esc(d.branch)}</div>
      ${chain(d)}</div>`).join("")}`;
};

pages.access = async () => {
  let concessoes = [];
  let recusa = "";
  try {
    concessoes = (await get(api("/access"))).access;
  } catch (e) {
    // Nao poder listar e uma resposta, e ela e dita. Uma pagina vazia leria-se
    // como "ninguem tem acesso", que e a informacao oposta.
    recusa = e.message;
  }

  const linha = (g) => `<tr>
    <td class="key">${esc(g.principal)}</td>
    <td class="dim">${esc(g.provider)}</td>
    <td>${g.abilities.map((a) => `<code class="mono">${esc(a)}</code>`).join("<br>")}</td>
    <td class="dim">${esc(g.granted_by)}<br>${when(g.granted_at)}</td>
    <td>${g.active ? "<strong>viva</strong>"
      : `<span class="dim">revogada por ${esc(g.revoked_by)}<br>${when(g.revoked_at)}</span>`}</td>
    <td>${g.active && podeAqui("workspace.access.revoke")
      ? `<button class="choice revoke" data-principal="${esc(g.principal)}">revogar</button>`
      : ""}</td>
  </tr>`;

  const formulario = podeAqui("workspace.access.grant") ? `
    <h2>conceder acesso</h2>
    <div class="panel">
      <div class="choices">
        <input id="alvo" placeholder="provedor:sujeito" size="42">
        <select id="papel">
          <option value="operator">operator — decide escaladas</option>
          <option value="admin">admin — administra acesso</option>
          <option value="owner">owner — os dois</option>
        </select>
        <input id="nota" placeholder="por que (opcional)" size="26">
        <button class="choice" id="conceder">conceder</button>
      </div>
      <p class="sub" style="margin:10px 0 0">
        a identidade e <code class="mono">provedor:sujeito</code> — o
        identificador estavel que o provedor emite, nunca o nome de exibicao.
        conceder a si mesmo e recusado pelo motor.
      </p>
      <div class="outcome" data-for="grant"></div>
    </div>` : `<p class="sub">esta identidade nao recebeu autoridade para
        conceder acesso aqui.</p>`;

  return `
    <h1>acesso</h1>
    <p class="sub">quem pode agir neste workspace, quem concedeu, e quando.
       identidade nao e autorizacao: autenticar responde <em>quem e voce</em>;
       a concessao responde <em>voce recebeu acesso</em>.</p>
    ${recusa ? `<div class="err">${esc(recusa)}</div>` : `
    <div class="panel"><table><thead><tr>
      <th>identidade</th><th>provedor</th><th>capacidades</th>
      <th>concedida por</th><th>estado</th><th></th>
    </tr></thead><tbody>${concessoes.length
      ? concessoes.map(linha).join("")
      : `<tr><td colspan="6" class="empty">nenhuma concessao registrada</td></tr>`
    }</tbody></table></div>
    ${formulario}`}`;
};

pages.credentials = async () => {
  let itens = [];
  let recusa = "";
  try {
    itens = (await get(api("/credentials"))).credentials;
  } catch (e) {
    recusa = e.message;
  }

  // NAO ha coluna de valor, e nao ha rota que o devolva. Uma tela nunca precisa
  // do segredo para administrar a autoridade dele.
  const linha = (c) => `<tr>
    <td class="key">${esc(c.provider)}/${esc(c.name)}</td>
    <td><span class="level" data-l="${c.status === "ACTIVE" ? "OK"
      : c.status === "EXPIRED" ? "ATTENTION" : "STUCK"}">${esc(c.status)}</span></td>
    <td><code class="mono">${esc(c.secret_ref)}</code><br>
        <span class="dim">${esc(c.kind)}</span></td>
    <td>${c.capabilities.map((u) => `<code class="mono">${esc(u)}</code>`).join("<br>")}</td>
    <td class="dim">${esc(c.granted_by)}<br>${when(c.granted_at)}</td>
    <td class="dim">${c.expires_at ? when(c.expires_at) : "sem validade"}</td>
    <td>${c.status !== "REVOKED" && podeAqui("workspace.credential.revoke")
      ? `<button class="choice revoke-cred" data-id="${esc(c.id)}">revogar</button>`
      : c.revoked_by ? `<span class="dim">por ${esc(c.revoked_by)}</span>` : ""}</td>
  </tr>`;

  const formulario = podeAqui("workspace.credential.grant") ? `
    <h2>registrar credencial</h2>
    <div class="panel">
      <div class="choices">
        <input id="cred-nome" placeholder="nome" size="14">
        <input id="cred-provider" placeholder="provider" size="16">
        <input id="cred-ref" placeholder="env:NOME | arquivo:X | helper:Y" size="30">
        <input id="cred-caps" placeholder="repo.read,repo.pr" size="22">
        <input id="cred-dias" placeholder="validade (dias)" size="12">
        <button class="choice" id="registrar-cred">registrar</button>
      </div>
      <p class="sub" style="margin:10px 0 0">
        a referencia e um <strong>endereco</strong>, nunca o valor. o Regente
        nao guarda material secreto e nao ha rota que o devolva.
      </p>
      <div class="outcome" data-for="cred"></div>
    </div>` : `<p class="sub">esta identidade nao recebeu autoridade para
        administrar credenciais aqui.</p>`;

  return `
    <h1>credenciais</h1>
    <p class="sub">a autoridade de usar um segredo, com quem concedeu, para que
       capacidades e ate quando. o valor vive fora do Regente; aqui so existe o
       endereco. testar conexao e trabalho de terminal:
       <code class="mono">regente credentials testar</code>.</p>
    ${recusa ? `<div class="err">${esc(recusa)}</div>` : `
    <div class="panel"><table><thead><tr>
      <th>credencial</th><th>estado</th><th>referencia</th><th>capacidades</th>
      <th>concedida por</th><th>vence</th><th></th>
    </tr></thead><tbody>${itens.length ? itens.map(linha).join("")
      : `<tr><td colspan="7" class="empty">nenhuma credencial registrada</td></tr>`
    }</tbody></table></div>
    ${formulario}`}`;
};

pages.health = async () => {
  const h = await get(api("/health"));
  return `
    <h1>saude</h1>
    <p class="sub">calculada pelo motor, reexibida aqui. A tela nao tem uma
       segunda opiniao sobre estar tudo bem.</p>
    <div class="banner" data-l="${esc(h.level)}">
      <span class="word">${esc(h.level)}</span>
      <span class="dim">${h.healthy ? "sem sinal de problema" : "ha sinal que exige leitura"}</span>
      <span class="dim">medido em ${when(h.at)}</span>
    </div>
    <div class="panel">${h.signals.map((s) => `<div class="signal">
        <div class="level" data-l="${esc(s.level)}">${esc(s.level)}</div>
        <div><div class="q">${esc(s.question)}</div>
          <div class="a">${said(s.detail)}</div>
          ${s.evidence.length ? `<ul>${s.evidence.map((e) =>
            `<li>${esc(e)}</li>`).join("")}</ul>` : ""}</div>
      </div>`).join("")}</div>
    <h2>medidas</h2>
    <div class="panel"><table><tbody>${
      Object.entries(h.measurements).sort().map(([k, v]) =>
        `<tr><td class="key">${esc(k)}</td><td class="dim">${esc(v)}</td></tr>`
      ).join("")}</tbody></table></div>`;
};

pages.events = async () => {
  const { events } = await get(api("/events?limit=200"));
  return `<h1>eventos</h1>
    <p class="sub">${events.length} mais recentes, como o motor os gravou</p>
    <div class="panel">${timeline(events)}</div>`;
};

// ---------------------------------------------------------------------------
// roteamento e atualizacao
// ---------------------------------------------------------------------------

const NAV = [
  ["overview", "painel", "#/"],
  ["tasks", "tasks", "#/tasks"],
  ["needs", "atencao", "#/needs"],
  ["runs", "runs", "#/runs"],
  ["deliveries", "entregas", "#/deliveries"],
  ["events", "eventos", "#/events"],
  ["access", "acesso", "#/access"],
  ["credentials", "credenciais", "#/credentials"],
  ["health", "saude", "#/health"],
];

function parseRoute() {
  const raw = (location.hash || "#/").slice(2);
  const parts = raw.split("/").filter(Boolean).map(decodeURIComponent);
  if (!parts.length) return { page: "overview", args: [] };
  return { page: parts[0], args: parts.slice(1) };
}

function renderNav() {
  document.getElementById("nav").innerHTML = NAV.map(([page, label, href]) =>
    `<a href="${href}" class="${state.route.page === page ? "on" : ""}">${label}</a>`
  ).join("");
}

function renderPicker() {
  const el = document.getElementById("workspace-picker");
  el.innerHTML = state.workspaces.map((w) =>
    `<option value="${esc(w.id)}" ${w.id === state.workspace ? "selected" : ""}
      >${esc(w.client)} / ${esc(w.name)}</option>`).join("");
}

/**
 * Marca quao fresca e a leitura.
 *
 * Existe porque uma tela que atualiza sozinha e indistinguivel de uma tela
 * congelada — e as duas parecem igualmente calmas quando nada esta mudando.
 */
function markFreshness(ok, message) {
  const dot = document.getElementById("freshness-dot");
  dot.className = `dot ${ok ? "live" : "stale"}`;
  document.getElementById("freshness").textContent =
    message || (ok ? `lido ${new Date().toLocaleTimeString()}` : "leitura falhou");
}

async function render() {
  state.route = parseRoute();
  renderNav();
  const view = document.getElementById("view");
  const page = pages[state.route.page];
  if (!page) {
    view.innerHTML = `<div class="err">rota desconhecida: ${esc(state.route.page)}</div>`;
    return;
  }
  try {
    // A identidade e relida a CADA render: uma revogacao feita noutro lugar
    // precisa aparecer aqui sem ninguem recarregar a pagina.
    try { renderIdentity((await get("/api/health")).identity); } catch { /* a
      pagina abaixo dira o que houve */ }
    view.innerHTML = await page(...state.route.args);
    state.lastRead = new Date();
    markFreshness(true);
  } catch (e) {
    // Falha de leitura nunca vira tela vazia nem estado antigo apresentado como
    // atual. O erro fica visivel e a marca de frescor fica vermelha.
    view.innerHTML = `<div class="err"><strong>a leitura falhou</strong><br>
      ${esc(e.message)}<br><span class="dim">a tela nao inventa estado entre
      duas leituras; o que estava aqui pode ter mudado.</span></div>`;
    markFreshness(false);
  }
}

function schedule() {
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(() => {
    if (!document.hidden) render();
  }, REFRESH_MS);
}

/**
 * Diz quem esta olhando e o que protege esta sessao.
 *
 * Fica sempre visivel, e nao so quando algo da errado: um mecanismo de
 * desenvolvimento que ninguem consegue distinguir de um real cria a sensacao
 * de que ha autenticacao. Aqui ele se anuncia.
 */
function renderIdentity(id) {
  state.identity = id;
  const el = document.getElementById("whoami");
  if (!id) { el.textContent = ""; return; }
  const aqui = (id.abilities || {})[ws()] || [];
  const pode = aqui.length ? aqui.length + " capacidade(s) aqui"
                           : "nenhuma capacidade aqui";
  el.className = id.development_only ? "whoami dev" : "whoami";
  el.textContent = `${id.authenticated ? id.display || id.subject : "nao autenticado"}`
    + ` · ${id.method} · ${pode}`
    + (id.development_only ? " · IDENTIDADE DE DESENVOLVIMENTO" : "");
  el.title = `${id.mechanism || ""}\nemissor: ${id.issuer || "nao informado"}`;
}

async function boot() {
  try {
    const health = await get("/api/health");
    renderIdentity(health.identity);
    const { workspaces } = await get("/api/workspaces");
    state.workspaces = workspaces;
    if (!workspaces.length) {
      // Nao e "nao ha nada". E "voce nao ve nada" -- e a diferenca decide se a
      // pessoa procura trabalho ou procura quem lhe da acesso.
      const anon = health.identity && !health.identity.authenticated;
      document.getElementById("view").innerHTML = `<div class="err">${
        anon ? "esta sessao nao esta autenticada; reabra a Mission Control pelo "
             + "endereco que o <code class='mono'>regente ui</code> imprimiu"
             : "nenhum workspace visivel para este operador"}.</div>`;
      markFreshness(false, anon ? "nao autenticado" : "sem workspace");
      return;
    }
    const saved = localStorage.getItem("regente.workspace");
    state.workspace = workspaces.some((w) => w.id === saved) ? saved : workspaces[0].id;
    renderPicker();
    document.getElementById("workspace-picker").addEventListener("change", (e) => {
      // Trocar de workspace troca TODOS os dados da tela: nada da leitura
      // anterior sobrevive, porque nada dela pertence a este workspace.
      state.workspace = e.target.value;
      localStorage.setItem("regente.workspace", state.workspace);
      location.hash = "#/";
      render();
    });
    window.addEventListener("hashchange", render);
    await render();
    schedule();
  } catch (e) {
    document.getElementById("view").innerHTML =
      `<div class="err">nao foi possivel falar com a API: ${esc(e.message)}</div>`;
    markFreshness(false);
  }
}

boot();

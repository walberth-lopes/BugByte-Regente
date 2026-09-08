// A conversa com o Regente.
//
// Uma regra governa este arquivo: **nada aqui decide**. Nao existe funcao que
// conclua que algo esta bloqueado, que um CI passou, que um lease venceu ou que
// uma task esta presa. Toda conclusao chega pronta da API, calculada por quem
// tem autoridade para calcula-la.
//
// A segunda regra e sobre a diferenca entre ler e escrever. Uma leitura que
// falha LANCA, porque a tela nao tem o que mostrar e precisa dizer isso. Uma
// escrita que e recusada NAO lanca: quem chamou precisa distinguir "nao
// autenticado" de "outro ja decidiu" de "a policy proibiu", porque cada um
// manda a pessoa fazer uma coisa diferente.

/** O segredo desta sessao, colocado pelo servidor nesta pagina. */
export const SESSION =
  document.querySelector('meta[name="regente-session"]')?.content || "";

function headers(extra) {
  const h = { Accept: "application/json", ...(extra || {}) };
  if (SESSION) h.Authorization = `Bearer ${SESSION}`;
  return h;
}

export class ErroDeLeitura extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
  }
}

export async function get(path) {
  const r = await fetch(path, { headers: headers() });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      detail = (await r.json()).detail || detail;
    } catch {
      /* nao-JSON */
    }
    throw new ErroDeLeitura(r.status, detail);
  }
  return r.json();
}

async function escrita(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: body ? headers({ "Content-Type": "application/json" }) : headers(),
    body: body ? JSON.stringify(body) : undefined,
  });
  let payload = null;
  try {
    payload = await r.json();
  } catch {
    /* nao-JSON */
  }
  return { ok: r.ok, status: r.status, payload: payload || {} };
}

export const post = (path, body) => escrita("POST", path, body);
export const del = (path) => escrita("DELETE", path, null);

/** As rotas de um workspace, montadas num lugar so. */
export const rota = (workspace, sufixo) =>
  `/api/workspaces/${encodeURIComponent(workspace)}${sufixo}`;

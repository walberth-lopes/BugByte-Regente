// A cadeia commit -> push -> pull request -> CI.
//
// O veredito de "verde" vem do motor, em `ci_green`. Se a tela decidisse isso,
// existiriam duas definicoes de verde no sistema -- e a que o operador ve seria
// a que nao conhece `PREEXISTING_FAILURE`, `NO_CHECKS` nem `UNAVAILABLE`.
//
// Cada elo diz a ausencia em voz alta: "Sem pull request", "Não observado".
// Um elo em branco leria-se como "deu tudo certo", que e a leitura oposta.

import { Painel, Tecnico } from "../ui.jsx";
import { Frase, when } from "../present.js";

export default function Entrega({ entrega: d }) {
  const elos = [
    {
      passo: "Commit",
      valor: (d.commit_sha || "").slice(0, 12) || "Sem commit",
      feito: !!d.commit_sha,
      ausente: !d.commit_sha,
    },
    {
      passo: "Push",
      valor: d.pushed ? "Enviado" : "Não enviado",
      feito: d.pushed,
      ausente: !d.pushed,
    },
    {
      passo: "Pull request",
      valor: d.pull_request
        ? `#${d.pull_request}`
        : d.pull_request_absent || "Sem pull request",
      feito: !!d.pull_request,
      ausente: !d.pull_request,
      href: d.pull_request_url,
    },
    {
      passo: "CI",
      valor:
        d.ci_state === "NOT_OBSERVED"
          ? "Não observado"
          : d.ci_green
            ? "Passou"
            : d.ci_state === "PENDING"
              ? "Em andamento"
              : Frase(String(d.ci_result || d.ci_state || "").replace(/_/g, " ")),
      feito: d.ci_green,
      ausente: d.ci_state === "NOT_OBSERVED",
      esperando: d.ci_state === "PENDING",
      ruim: d.ci_state === "CONCLUDED" && !d.ci_green,
    },
  ];

  return (
    <Painel>
      <div className="row">
        <h3>
          {d.pull_request ? `Pull request #${d.pull_request}` : d.task_key}
        </h3>
        <span className="spacer" style={{ marginLeft: "auto" }} />
        {d.pull_request_url && (
          <a
            className="btn btn-sm"
            href={d.pull_request_url}
            target="_blank"
            rel="noreferrer"
          >
            Abrir no provedor ↗
          </a>
        )}
      </div>

      <div className="dim" style={{ margin: "var(--s-2) 0 var(--s-4)" }}>
        Task {d.task_key} · branch <code>{d.branch || "—"}</code>
      </div>

      <div className="chain">
        {elos.map((l) => (
          <div
            key={l.passo}
            className={[
              "link",
              l.feito ? "done" : "",
              l.ausente ? "absent" : "",
              l.esperando ? "waiting" : "",
              l.ruim ? "bad" : "",
            ]
              .filter(Boolean)
              .join(" ")}
          >
            <div className="step">{l.passo}</div>
            <div className="val">
              {l.href ? (
                <a href={l.href} target="_blank" rel="noreferrer">
                  {l.valor}
                </a>
              ) : (
                l.valor
              )}
            </div>
          </div>
        ))}
      </div>

      {d.ci_reason && (
        <p
          className="muted"
          style={{ marginTop: "var(--s-3)", fontSize: "var(--fs-sm)" }}
        >
          {Frase(d.ci_reason)}
        </p>
      )}

      <Tecnico
        pares={[
          ["Entrega", d.id],
          ["Execução", d.run_id],
          ["Repositório", d.repository],
          ["Destino do push", d.push_target],
          ["Head do pull request", d.pull_request_head],
          ["Estado do CI", d.ci_state],
          ["Resultado do CI", d.ci_result],
          ["Observações de CI", d.ci_observations],
          ["CI observado em", d.ci_observed_at ? when(d.ci_observed_at) : ""],
          ["Criada em", d.created_at ? when(d.created_at) : ""],
        ]}
      />
    </Painel>
  );
}

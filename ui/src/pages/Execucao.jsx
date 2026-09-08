// Uma execução, inteira: o que o agente fez, quanto gastou, e o que entregou.

import { useRegente, useLeitura } from "../estado.jsx";
import {
  Alerta,
  Badge,
  Dado,
  Leitura,
  LinhaDoTempo,
  Painel,
  Secao,
  Tecnico,
} from "../ui.jsx";
import { Frase, when } from "../present.js";
import Entrega from "../components/Entrega.jsx";

export default function Execucao({ args }) {
  const { api } = useRegente();
  const leitura = useLeitura(api(`/runs/${encodeURIComponent(args[0] || "")}`));

  return (
    <Leitura estado={leitura} oQue="esta execução">
      {(d) => {
        const r = d.run;
        return (
          <>
            <div className="secao-cabeca">
              <a className="hint" href="#/execucoes">
                ← Todas as execuções
              </a>
            </div>

            <Painel>
              <div className="row">
                <h1>{r.task_key || r.id}</h1>
                <Badge termo={r.state} />
              </div>

              <div className="dados" style={{ marginTop: "var(--s-5)" }}>
                <Dado
                  rotuloTexto="Durou"
                  valor={r.duration || "—"}
                  porque={
                    r.ended_at ? `Até ${when(r.ended_at)}` : "Ainda rodando"
                  }
                />
                <Dado
                  rotuloTexto="Agente"
                  valor={r.agent || "Não informado"}
                  porque={`${d.iterations} iteração(ões)`}
                />
                <Dado
                  rotuloTexto="Custo"
                  valor={`US$ ${Number(r.cost_usd || 0).toFixed(2)}`}
                  porque={`${r.tokens || 0} tokens`}
                />
                <Dado
                  rotuloTexto="Chamadas de ferramenta"
                  valor={d.tool_calls}
                  porque="O que o agente executou"
                />
              </div>

              {r.reason && (
                <Painel tight style={{ marginTop: "var(--s-5)" }}>
                  <strong>Resultado</strong>
                  <br />
                  <span className="muted">{Frase(r.reason)}</span>
                </Painel>
              )}

              <Tecnico
                pares={[
                  ["Identificador", r.id],
                  ["Estado interno", r.state],
                  ["Começou", r.started_at ? when(r.started_at) : ""],
                  ["Terminou", r.ended_at ? when(r.ended_at) : ""],
                  ["Branch", r.branch],
                  ["Repositório", d.repository],
                  ["Área de trabalho", d.workspace_path],
                  ["Reservas que segura", (d.leases || []).join(", ")],
                ]}
              />
            </Painel>

            {(d.blockers || []).length > 0 && (
              <Secao titulo="Bloqueios">
                <div className="stack">
                  {d.blockers.map((b, i) => (
                    <Alerta
                      key={i}
                      tone="danger"
                      titulo={Frase(b.summary || b.kind)}
                      detalhe={b.detail || "Nenhum detalhe registrado."}
                    />
                  ))}
                </div>
              </Secao>
            )}

            {d.delivery && (
              <Secao titulo="O que foi entregue">
                <Entrega entrega={d.delivery} />
              </Secao>
            )}

            {(d.timeline || []).length > 0 && (
              <Secao titulo="História">
                <Painel>
                  <LinhaDoTempo eventos={d.timeline} alto />
                </Painel>
              </Secao>
            )}
          </>
        );
      }}
    </Leitura>
  );
}

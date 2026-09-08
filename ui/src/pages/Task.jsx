// Uma task, inteira.
//
// A pergunta que esta pagina responde e "e agora?". Por isso os quatro dados do
// topo sao situacao, proximo passo, prioridade e ha quanto tempo -- e o
// SIGNIFICADO do estado vem junto, escrito pelo motor em `state.meaning`. Um
// nome de enum sozinho obriga quem le a ir procurar o que ele quer dizer.

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
import { DONO, Frase, rotulo, when } from "../present.js";
import CartaoDecisao from "../components/Decisao.jsx";
import Entrega from "../components/Entrega.jsx";

export default function Task({ args }) {
  const { api } = useRegente();
  const leitura = useLeitura(api(`/tasks/${encodeURIComponent(args[0] || "")}`));

  return (
    <Leitura estado={leitura} oQue="esta task">
      {(d) => {
        const c = d.card;
        const s = c.state;
        return (
          <>
            <div className="secao-cabeca">
              <a className="hint" href="#/tasks">
                ← Todas as tasks
              </a>
            </div>

            <Painel>
              <div className="row">
                <h1>{c.key}</h1>
                <Badge termo={s.name} />
                {c.eligible === false && (
                  <span className="badge" data-tone="idle">
                    Fora da fila
                  </span>
                )}
              </div>
              <p
                className="lede"
                style={{ fontSize: "var(--fs-1)", marginTop: "var(--s-2)" }}
              >
                {c.title}
              </p>

              <div className="dados" style={{ marginTop: "var(--s-5)" }}>
                <Dado
                  rotuloTexto="Situação"
                  valor={rotulo(s.name)}
                  porque={Frase(s.meaning)}
                />
                <Dado
                  rotuloTexto="Próximo passo"
                  valor={
                    s.next_action ? Frase(s.next_action) : "Sem próxima etapa"
                  }
                  porque={DONO[s.owner] || s.owner}
                />
                <Dado
                  rotuloTexto="Prioridade"
                  valor={c.priority}
                  porque="Número menor roda antes"
                />
                <Dado
                  rotuloTexto="Nesta situação há"
                  valor={s.age}
                  porque={s.since ? `Desde ${when(s.since)}` : ""}
                />
              </div>

              {s.reason && (
                <Painel tight style={{ marginTop: "var(--s-5)" }}>
                  <strong>Motivo registrado</strong>
                  <br />
                  <span className="muted">{Frase(s.reason)}</span>
                </Painel>
              )}

              {(c.why || []).length > 0 && (
                <Painel tight style={{ marginTop: "var(--s-4)" }}>
                  <strong>Por que esta prioridade</strong>
                  <br />
                  <span className="muted">{c.why.join("; ")}</span>
                </Painel>
              )}

              {c.excluded_by && (
                <Painel tight style={{ marginTop: "var(--s-4)" }}>
                  <strong>Fora da fila</strong>
                  <br />
                  <span className="muted">
                    Excluída pela regra “{c.excluded_by}”.
                  </span>
                </Painel>
              )}

              <Tecnico
                pares={[
                  ["Identificador", c.id],
                  ["Estado interno", s.name],
                  ["Estado anterior", d.previous_state],
                  ["Dono do próximo passo", s.owner],
                  ["Situação no board", c.external_status],
                  ["Prioridade da origem", c.origin_priority],
                  ["Tentativas", c.attempts],
                  ["Risco", c.risk],
                  ["Repositório", c.repository],
                  ["Projeto", c.project],
                  ["Criada em", c.created_at ? when(c.created_at) : ""],
                  ["Atualizada em", c.updated_at ? when(c.updated_at) : ""],
                ]}
              />
            </Painel>

            {d.escalation && (
              <Secao titulo="Precisa de você">
                <CartaoDecisao escalada={d.escalation} />
              </Secao>
            )}

            {(d.blockers || []).length > 0 && (
              <Secao titulo="Bloqueios">
                <div className="stack">
                  {d.blockers.map((b, i) => (
                    <Alerta
                      key={i}
                      tone="danger"
                      titulo={Frase(b.summary || b.kind)}
                      detalhe={b.detail || "Nenhum detalhe registrado."}
                      acao={
                        b.action ? (
                          <span className="dim">{Frase(b.action)}</span>
                        ) : null
                      }
                    />
                  ))}
                </div>
              </Secao>
            )}

            {d.description && (
              <Painel>
                <h2>Descrição</h2>
                <p
                  className="muted"
                  style={{ marginTop: "var(--s-3)", whiteSpace: "pre-wrap" }}
                >
                  {d.description}
                </p>
              </Painel>
            )}

            {(d.deliveries || []).length > 0 && (
              <Secao titulo="Entregas desta task">
                <div className="stack">
                  {d.deliveries.map((x) => (
                    <Entrega key={x.id} entrega={x} />
                  ))}
                </div>
              </Secao>
            )}

            {(d.timeline || []).length > 0 && (
              <Secao titulo="História">
                <Painel>
                  <LinhaDoTempo eventos={d.timeline} />
                </Painel>
              </Secao>
            )}
          </>
        );
      }}
    </Leitura>
  );
}

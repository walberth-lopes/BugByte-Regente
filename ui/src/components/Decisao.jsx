// Uma escalada humana, com as opcoes que o motor ofereceu.
//
// Tres perguntas, nesta ordem: o que aconteceu, por que importa, e o que da
// para fazer. Um cartao que responde as duas primeiras e cala na terceira e uma
// notificacao de mau humor.
//
// As opcoes vem da API. NAO ha lista fixa aqui: um botao que a tela inventa e
// uma acao que o Core nunca prometeu aceitar, e a pessoa so descobre isso
// depois de clicar.

import { useState } from "react";
import { post } from "../api.js";
import { useRegente } from "../estado.jsx";
import { Botao, Dito } from "../ui.jsx";
import { Frase, digaOErro } from "../present.js";

export default function CartaoDecisao({ escalada }) {
  const { api, podeAqui, recarregar } = useRegente();
  const [desfecho, setDesfecho] = useState(null);
  const [enviando, setEnviando] = useState(false);
  const pode = podeAqui("approval.decide");

  /**
   * Envia a decisao e RELE o estado.
   *
   * `200` significa que o servidor aceitou, e nao que a tela sabe o que ficou
   * gravado. A resposta da escrita nao vira segunda fonte de verdade: o que a
   * pessoa passa a ver vem da leitura seguinte.
   */
  async function decidir(escolha) {
    setEnviando(true);
    setDesfecho({ tipo: "working", texto: "Enviando sua decisão…" });
    const r = await post(
      api(`/approvals/${encodeURIComponent(escalada.id)}/decision`),
      { choice: escolha },
    );
    if (!r.ok) {
      setEnviando(false);
      setDesfecho({ tipo: "bad", texto: digaOErro(r) });
    } else {
      setDesfecho({ tipo: "good", texto: "Decisão registrada. Relendo o estado…" });
    }
    // Relemos mesmo na recusa: o mundo pode ter mudado enquanto se decidia.
    recarregar();
  }

  const risco = String(escalada.risk || "").toUpperCase();

  return (
    <article className="decision">
      <div className="row">
        <a className="key" href={`#/task/${encodeURIComponent(escalada.task_id)}`}>
          {escalada.task_key}
        </a>
        <span
          className="badge"
          data-tone={
            risco === "HIGH" ? "danger" : risco === "MEDIUM" ? "warn" : "idle"
          }
        >
          Risco {risco.toLowerCase() || "não informado"}
        </span>
        <span className="spacer" style={{ marginLeft: "auto" }} />
        <span className="dim">Esperando há {escalada.waiting}</span>
      </div>

      <div className="q">
        <span className="rot">O que aconteceu</span>
        <Dito
          valor={Frase(escalada.what_happened)}
          ausente="Nada foi registrado"
        />
      </div>

      <div className="q">
        <span className="rot">Por que isso importa</span>
        <Dito
          valor={Frase(escalada.why_it_matters)}
          ausente="Nenhuma justificativa registrada"
        />
      </div>

      {(escalada.what_was_tried || []).length > 0 && (
        <div className="q">
          <span className="rot">O que já foi tentado</span>
          <ul className="perms">
            {escalada.what_was_tried.map((t, i) => (
              <li key={i}>
                <span aria-hidden="true" className="dim">
                  ·
                </span>
                {t}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="q">
        <span className="rot">O que você pode fazer</span>
        {(escalada.options || []).length ? (
          <>
            <div className="choices">
              {escalada.options.map((o) => (
                <Botao
                  key={o.id}
                  variante={escalada.recommendation === o.id ? "btn-primary" : ""}
                  title={o.effect || ""}
                  disabled={!pode || enviando}
                  onClick={() => decidir(o.id)}
                >
                  {o.label}
                  {escalada.recommendation === o.id ? " ★" : ""}
                </Botao>
              ))}
            </div>
            {escalada.recommendation && (
              <p className="hint" style={{ marginTop: "var(--s-2)" }}>
                ★ é a opção que o motor recomenda. A escolha continua sendo sua.
              </p>
            )}
            {!pode && (
              <p className="hint" style={{ marginTop: "var(--s-2)" }}>
                Você não tem permissão para decidir neste workspace.
              </p>
            )}
          </>
        ) : (
          <p className="muted">
            Esta escalada não ofereceu opções. Ela precisa ser resolvida fora do
            Regente.
          </p>
        )}
      </div>

      {desfecho && (
        <div className={`outcome ${desfecho.tipo}`} role="status">
          {desfecho.texto}
        </div>
      )}
    </article>
  );
}

// Formulário: campos, confirmação e feedback.
//
// Os campos de um provedor não são escritos aqui: eles vêm do CATÁLOGO, que o
// servidor entrega a partir da camada de adapters. Se a lista de campos do Jira
// morasse no frontend, existiriam duas definições do formato do Jira — e a que
// divergisse aceitaria o que o motor recusa.

import { useEffect, useRef, useState } from "react";

/** Um campo, desenhado a partir do que o catálogo disse sobre ele. */
export function Campo({ campo, valor, aoMudar }) {
  const id = `campo-${campo.key}`;
  const comum = {
    id,
    className: "input",
    value: valor ?? "",
    onChange: (e) => aoMudar(campo.key, e.target.value),
    placeholder: campo.example || "",
    "aria-describedby": campo.help ? `${id}-ajuda` : undefined,
    required: campo.required || undefined,
  };

  return (
    <div className="field">
      <label htmlFor={id}>
        {campo.label}
        {campo.required && (
          <span className="req" title="Obrigatório">
            *
          </span>
        )}
      </label>

      {campo.kind === "booleano" ? (
        <label className="check-inline">
          <input
            id={id}
            type="checkbox"
            checked={!!valor}
            onChange={(e) => aoMudar(campo.key, e.target.checked)}
          />
          <span>{campo.help || "Ativado"}</span>
        </label>
      ) : campo.kind === "numero" ? (
        <input {...comum} type="number" inputMode="decimal" />
      ) : campo.kind === "lista" ? (
        // Uma lista é digitada como texto separado por espaços, e a tela monta
        // o array. Pedir `["python", "app.py"]` a quem instalou o Regente hoje
        // é pedir que ele aprenda JSON para preencher um campo.
        <input {...comum} type="text" />
      ) : (
        <input {...comum} type="text" />
      )}

      {campo.help && campo.kind !== "booleano" && (
        <span className="help" id={`${id}-ajuda`}>
          {campo.help}
        </span>
      )}
      {!campo.help && campo.example && (
        <span className="help" id={`${id}-ajuda`}>
          Por exemplo: <code>{campo.example}</code>
        </span>
      )}
    </div>
  );
}

/**
 * Uma pergunta antes do que não se desfaz.
 *
 * A confirmação diz o IMPACTO, e não "tem certeza?". Quem revoga uma credencial
 * precisa saber que o Regente para de alcançar aquele serviço — essa é a
 * informação que muda a decisão.
 */
export function Confirmar({ aberto, titulo, impacto, rotuloOk, aoConfirmar, aoCancelar }) {
  const ref = useRef(null);
  useEffect(() => {
    if (aberto) ref.current?.focus();
  }, [aberto]);

  useEffect(() => {
    if (!aberto) return;
    const ao = (e) => e.key === "Escape" && aoCancelar();
    window.addEventListener("keydown", ao);
    return () => window.removeEventListener("keydown", ao);
  }, [aberto, aoCancelar]);

  if (!aberto) return null;
  return (
    <div className="modal-fundo" onClick={aoCancelar}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={titulo}
        onClick={(e) => e.stopPropagation()}
      >
        <h3>{titulo}</h3>
        <p className="muted">{impacto}</p>
        <div className="form-actions">
          <button className="btn" onClick={aoCancelar} ref={ref}>
            Cancelar
          </button>
          <button className="btn btn-danger" onClick={aoConfirmar}>
            {rotuloOk}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * O que aconteceu depois de uma ação.
 *
 * Toda escrita responde alguma coisa. Um botão que salva e não diz nada obriga
 * a pessoa a recarregar a página para descobrir se funcionou.
 */
export function useFeedback() {
  const [msg, setMsg] = useState(null);

  useEffect(() => {
    if (!msg || msg.tipo !== "good") return;
    const t = setTimeout(() => setMsg(null), 6000);
    return () => clearTimeout(t);
  }, [msg]);

  const nodo = msg ? (
    <div className={`outcome ${msg.tipo}`} role="status">
      {msg.texto}
    </div>
  ) : null;

  return [
    nodo,
    {
      ok: (texto) => setMsg({ tipo: "good", texto }),
      erro: (texto) => setMsg({ tipo: "bad", texto }),
      indo: (texto) => setMsg({ tipo: "working", texto }),
      limpar: () => setMsg(null),
    },
  ];
}

/** Uma área que só interessa a quem já sabe o que está fazendo. */
export const Avancado = ({ titulo = "Opções avançadas", children }) => (
  <details className="tech">
    <summary>{titulo}</summary>
    <div className="tech-body avancado">{children}</div>
  </details>
);

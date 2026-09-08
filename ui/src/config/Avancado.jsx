// Configuração avançada: o mesmo ajuste, no formato que o motor lê.
//
// Isto NÃO é a experiência principal — as abas anteriores montam estas mesmas
// estruturas por formulário. Está aqui por dois motivos honestos:
//
// 1. quem já sabe o que está fazendo às vezes é mais rápido editando direto;
// 2. mostrar o que a interface gerou é a forma de alguém confiar nela.
//
// A escrita passa pelo mesmo serviço e pela mesma validação: não existe atalho
// aqui que as outras abas não tenham.

import { useEffect, useState } from "react";
import { post } from "../api.js";
import { useRegente, useLeitura } from "../estado.jsx";
import { Alerta, Leitura, Painel, Secao } from "../ui.jsx";
import { useFeedback } from "../components/Formulario.jsx";
import { digaOErro, when } from "../present.js";
import Procedencia from "./Procedencia.jsx";

const CHAVES = [
  [
    "providers",
    "Conexões",
    "Um objeto por papel, com o nome do serviço e as opções dele.",
  ],
  [
    "status_map",
    "Mapeamento de status",
    "Um objeto {etapa do Regente: [status do board]}. A aba “Status do board” monta isto perguntando na direção contrária, que é a que uma pessoa usa.",
  ],
  [
    "selection",
    "Regras de seleção e prioridade",
    "Uma lista de regras, avaliadas de cima para baixo.",
  ],
];

export default function Avancado() {
  const { api } = useRegente();
  const leitura = useLeitura(api("/settings"));

  return (
    <Leitura estado={leitura} oQue="a configuração">
      {(cfg) => (
        <>
          <Alerta
            tone="info"
            icone="i"
            titulo="Esta é a saída das outras abas"
            detalhe="O que você configurou por formulário aparece aqui exatamente como o motor vai ler. Editar direto funciona, e passa pela mesma validação — as mensagens de erro vêm do motor, não da tela."
          />

          {cfg.changed_by && (
            <p className="hint">
              Última alteração pela interface: {cfg.changed_by} em{" "}
              {when(cfg.changed_at)}.
            </p>
          )}

          {CHAVES.map(([chave, titulo, ajuda]) => (
            <Secao key={chave} titulo={titulo}>
              <Painel>
                <Procedencia campo={cfg.fields[chave]} chave={chave} />
                <Editor chave={chave} campo={cfg.fields[chave]} ajuda={ajuda} />
              </Painel>
            </Secao>
          ))}
        </>
      )}
    </Leitura>
  );
}

function Editor({ chave, campo, ajuda }) {
  const { api, podeAqui, recarregar } = useRegente();
  const pode = podeAqui("workspace.settings.write");
  const [feedback, diga] = useFeedback();
  const gravado = JSON.stringify(campo.value ?? (chave === "selection" ? [] : {}), null, 2);
  const [texto, setTexto] = useState(gravado);
  const [sujo, setSujo] = useState(false);

  // A releitura de cinco em cinco segundos não pode apagar o que alguém está
  // digitando. Enquanto houver rascunho, o texto é de quem edita.
  useEffect(() => {
    if (!sujo) setTexto(gravado);
  }, [gravado, sujo]);

  async function salvar() {
    let valor;
    try {
      valor = JSON.parse(texto);
    } catch (e) {
      diga.erro(`O texto não é um JSON válido: ${e.message}`);
      return;
    }
    diga.indo("Salvando…");
    const r = await post(api(`/settings/${encodeURIComponent(chave)}`), {
      value: valor,
    });
    if (!r.ok) {
      // A mensagem do motor nomeia o campo, o balde ou o efeito que não existe.
      diga.erro(digaOErro(r));
      return;
    }
    setSujo(false);
    diga.ok("Configuração salva.");
    recarregar();
  }

  return (
    <div className="field" style={{ marginTop: "var(--s-4)" }}>
      <label htmlFor={`cfg-${chave}`}>Definição</label>
      <textarea
        id={`cfg-${chave}`}
        className="input mono"
        rows={10}
        spellCheck={false}
        readOnly={!pode}
        value={texto}
        onChange={(e) => {
          setTexto(e.target.value);
          setSujo(true);
        }}
      />
      <span className="help">{ajuda}</span>
      {pode ? (
        <div className="form-actions">
          <button className="btn btn-primary" onClick={salvar} disabled={!sujo}>
            Salvar
          </button>
          {sujo && (
            <button
              className="btn btn-ghost"
              onClick={() => {
                setTexto(gravado);
                setSujo(false);
                diga.limpar();
              }}
            >
              Descartar alterações
            </button>
          )}
          {feedback}
        </div>
      ) : (
        <span className="help">
          Você não tem permissão para alterar a configuração deste workspace.
        </span>
      )}
    </div>
  );
}

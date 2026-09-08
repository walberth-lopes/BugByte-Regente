// O que os status do SEU board significam para o Regente.
//
// Esta é a configuração que mais falha em silêncio quando fica de fora: um
// status não mapeado deixa a task fora da fila para sempre, e a tela de tasks
// não tem como dizer por quê — ela mostra uma lista curta e correta, que é a
// pior forma de erro.
//
// Por isso a página é feita ao contrário do arquivo: ela parte dos status que o
// Regente REALMENTE ENCONTROU no board, e pergunta o que fazer com cada um.
// Escrever `status_map:` do zero exige adivinhar a grafia exata de cada coluna.

import { useEffect, useMemo, useState } from "react";
import { post } from "../api.js";
import { useRegente, useLeitura } from "../estado.jsx";
import { Alerta, Leitura, Painel, Secao, Vazio } from "../ui.jsx";
import { useFeedback } from "../components/Formulario.jsx";
import { digaOErro } from "../present.js";
import Procedencia from "./Procedencia.jsx";

/**
 * Os baldes do motor (`ports/tasks.py::STATUS_BUCKETS`), ditos em português.
 *
 * A ordem é a do caminho que uma task percorre. Um `<select>` em ordem
 * alfabética faria "andamento" vir antes de "analise", o que ensina errado.
 */
const BALDES = [
  ["available", "Pronta para o Regente pegar", "É daqui que ele tira trabalho."],
  ["analise", "Sendo analisada", "Alguém está entendendo o problema."],
  ["andamento", "Em andamento", "O trabalho já começou."],
  ["revisao", "Em revisão", "Esperando alguém revisar."],
  ["validacao", "Em validação", "Esperando teste ou homologação."],
  ["done", "Concluída", "Acabou. O Regente não mexe mais."],
  ["ignorado", "Ignorar", "O Regente não deve pegar tasks assim."],
];

const NOME_DO_BALDE = Object.fromEntries(BALDES.map(([k, v]) => [k, v]));

/**
 * As duas direções deste mapa, e por que elas são diferentes.
 *
 * O MOTOR guarda `{balde: [nomes do board]}` — um balde tem vários status, e
 * essa é a forma natural de escrever à mão. Uma PESSOA olhando o próprio board
 * pergunta o contrário: "e a coluna 'A fazer', o que ela significa?".
 *
 * A tela pergunta na direção da pessoa e grava na direção do motor. As duas
 * funções abaixo são a única tradução; escrever o formato do motor à mão em
 * mais de um lugar seria uma segunda definição dele, e a que divergisse
 * gravaria o que o motor recusa — que foi exatamente o defeito que existiu aqui.
 */
const paraTela = (doMotor) => {
  const saida = {};
  for (const [balde, nomes] of Object.entries(doMotor || {})) {
    for (const nome of nomes || []) saida[nome] = balde;
  }
  return saida;
};

const paraMotor = (daTela) => {
  const saida = {};
  for (const [nome, balde] of Object.entries(daTela || {})) {
    if (!balde) continue;
    (saida[balde] = saida[balde] || []).push(nome);
  }
  return saida;
};

export default function StatusMap() {
  const { api } = useRegente();
  const leitura = useLeitura([api("/settings"), api("/queue")]);

  return (
    <Leitura estado={leitura} oQue="o mapeamento de status">
      {(cfg, fila) => <Editor campo={cfg.fields.status_map} fila={fila} />}
    </Leitura>
  );
}

function Editor({ campo, fila }) {
  const { api, podeAqui, recarregar } = useRegente();
  const pode = podeAqui("workspace.settings.write");
  const [feedback, diga] = useFeedback();

  const gravado = useMemo(
    () =>
      paraTela(campo.value && typeof campo.value === "object" ? campo.value : {}),
    [campo.value],
  );
  const [mapa, setMapa] = useState(gravado);
  const [sujo, setSujo] = useState(false);
  const [novo, setNovo] = useState("");

  useEffect(() => {
    if (!sujo) setMapa(gravado);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(gravado)]);

  /**
   * Os status que o Regente viu no board, e não os que alguém imaginou.
   *
   * `/queue` traz o `external_status` de cada task descoberta. É a única fonte
   * honesta: listar os status possíveis do Jira exigiria uma credencial e uma
   * chamada que esta página não faz.
   */
  const encontrados = useMemo(() => {
    const todos = [...(fila.queue || []), ...(fila.excluded || [])];
    return [
      ...new Set(todos.map((t) => t.external_status).filter(Boolean)),
    ].sort((a, b) => a.localeCompare(b, "pt-BR"));
  }, [fila]);

  const naoMapeados = encontrados.filter((s) => !(s in mapa));
  const mapeados = Object.keys(mapa).sort((a, b) => a.localeCompare(b, "pt-BR"));

  async function salvar(proximo, msg) {
    setMapa(proximo);
    setSujo(true);
    diga.indo("Salvando…");
    const r = await post(api("/settings/status_map"), {
      value: paraMotor(proximo),
    });
    if (!r.ok) {
      diga.erro(digaOErro(r));
      return;
    }
    setSujo(false);
    diga.ok(msg);
    recarregar();
  }

  const definir = (status, balde) => {
    const p = { ...mapa };
    if (balde) p[status] = balde;
    else delete p[status];
    salvar(
      p,
      balde
        ? `“${status}” agora significa “${NOME_DO_BALDE[balde]}”.`
        : `“${status}” voltou a ficar sem mapeamento.`,
    );
  };

  return (
    <>
      <p className="muted">
        Cada coluna do seu board precisa dizer ao Regente em que etapa a task
        está. Um status que ficar de fora continua desconhecido, e o Regente
        <strong> não pega a task</strong> — de propósito, para ele não adivinhar.
      </p>

      <Procedencia campo={campo} chave="status_map" />

      {naoMapeados.length > 0 && (
        <Alerta
          tone="warn"
          titulo={`${naoMapeados.length} status ainda precisam ser configurados`}
          detalhe={`O Regente encontrou ${naoMapeados
            .map((s) => `“${s}”`)
            .join(", ")} no seu board, e ainda não sabe como tratá-los. Enquanto isso, essas tasks ficam fora da fila.`}
        />
      )}

      <Secao
        titulo="Status encontrados no seu board"
        hint={
          encontrados.length
            ? `${encontrados.length} status em ${
                (fila.queue?.length || 0) + (fila.excluded?.length || 0)
              } task(s)`
            : undefined
        }
      >
        <Painel flush>
          {encontrados.length ? (
            <div className="mapa">
              <div className="mapa-cabeca">
                <span>Status no board</span>
                <span>O Regente entende como</span>
              </div>
              {encontrados.map((s) => (
                <Linha
                  key={s}
                  status={s}
                  valor={mapa[s] || ""}
                  pode={pode}
                  aoMudar={(b) => definir(s, b)}
                />
              ))}
            </div>
          ) : (
            <Vazio
              icone="status"
              titulo="Nenhum status encontrado ainda"
              texto="O Regente precisa ler o board pelo menos uma vez para saber quais status existem. Conecte um board e rode um ciclo — os status aparecem aqui sozinhos."
            />
          )}
        </Painel>
        {feedback}
      </Secao>

      {mapeados.some((s) => !encontrados.includes(s)) && (
        <Secao
          titulo="Mapeados, mas não vistos no board"
          hint="Podem ser status antigos, ou de tasks que ainda não apareceram"
        >
          <Painel flush>
            <div className="mapa">
              {mapeados
                .filter((s) => !encontrados.includes(s))
                .map((s) => (
                  <Linha
                    key={s}
                    status={s}
                    valor={mapa[s]}
                    pode={pode}
                    aoMudar={(b) => definir(s, b)}
                  />
                ))}
            </div>
          </Painel>
        </Secao>
      )}

      {pode && (
        <Painel>
          <h3>Adicionar um status manualmente</h3>
          <p className="muted" style={{ margin: "var(--s-2) 0 var(--s-4)" }}>
            Útil quando você já sabe o nome de uma coluna que ainda não apareceu
            em nenhuma task. Escreva exatamente como está no board.
          </p>
          <div className="form-row">
            <div className="field">
              <label htmlFor="novo-status">Nome do status no board</label>
              <input
                id="novo-status"
                className="input"
                value={novo}
                placeholder="Waiting for Customer"
                onChange={(e) => setNovo(e.target.value)}
              />
            </div>
            <div className="form-actions">
              <button
                className="btn btn-primary"
                disabled={!novo.trim() || novo.trim() in mapa}
                onClick={() => {
                  definir(novo.trim(), "ignorado");
                  setNovo("");
                }}
              >
                Adicionar
              </button>
            </div>
          </div>
        </Painel>
      )}
    </>
  );
}

function Linha({ status, valor, pode, aoMudar }) {
  const id = `mapa-${status.replace(/\W/g, "-")}`;
  return (
    <div className="mapa-linha" data-vazio={String(!valor)}>
      <label htmlFor={id}>
        <code>{status}</code>
      </label>
      <div>
        <select
          id={id}
          className="input"
          value={valor}
          disabled={!pode}
          onChange={(e) => aoMudar(e.target.value)}
        >
          <option value="">— ainda não configurado —</option>
          {BALDES.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
        {valor ? (
          <span className="help">
            {BALDES.find(([v]) => v === valor)?.[2]}
          </span>
        ) : (
          <span className="help" style={{ color: "var(--warn)" }}>
            Enquanto ficar assim, o Regente não pega tasks neste status.
          </span>
        )}
      </div>
    </div>
  );
}

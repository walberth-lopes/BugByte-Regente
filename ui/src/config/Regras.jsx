// As regras de seleção e prioridade, montadas por formulário.
//
// O usuário pensa "quero que tasks com FAXINA no título rodem primeiro". Esta
// tela traduz isso para a estrutura que o motor lê:
//
//     {name, field, match, value, effect, delta}
//
// Ela NÃO reimplementa a regra: a avaliação, a ordem e a validação continuam no
// motor. O que ela faz é perguntar, montar e mandar salvar — e mostrar de volta
// a frase em português e a fila resultante, para a pessoa conferir o que
// acabou de configurar antes de ligar o Regente.
//
// Uma condição por regra, e não duas: `core/selection.py` avalia um campo por
// regra. Uma tela que oferecesse "E"/"OU" prometeria o que o motor não cumpre,
// e a pessoa só descobriria isso vendo a fila sair errada.

import { useEffect, useState } from "react";
import { post, del } from "../api.js";
import { useRegente, useLeitura } from "../estado.jsx";
import {
  Alerta,
  Badge,
  Leitura,
  Painel,
  Secao,
  Tabela,
  Titulo,
  Vazio,
  Dito,
} from "../ui.jsx";
import { Confirmar, useFeedback } from "../components/Formulario.jsx";
import Gaveta from "../components/Gaveta.jsx";
import { digaOErro, fraseDaRegra } from "../present.js";
import Procedencia from "./Procedencia.jsx";

/** Os campos que o motor sabe olhar (`core/selection.py::FIELDS`). */
const CAMPOS = [
  ["title", "O título"],
  ["key", "A chave"],
  ["project", "O projeto"],
  ["status", "O status no board"],
  ["labels", "Os rótulos"],
  ["priority", "A prioridade da origem"],
  ["assignee", "O responsável"],
  ["type", "O tipo"],
  ["components", "Os componentes"],
];

/** As comparações que o motor sabe fazer (`core/selection.py::Match`). */
const COMPARACOES = [
  ["contains", "contém"],
  ["equals", "é exatamente"],
  ["starts_with", "começa com"],
  ["in", "é um destes"],
  ["lt", "é menor que"],
  ["gt", "é maior que"],
];

/**
 * O que a regra faz.
 *
 * Os três primeiros são `effect: priority` com deltas diferentes. Escolher um
 * número de -100 a 100 não é uma pergunta que se faça a quem acabou de instalar
 * o Regente; escolher "alta" é. O número continua editável em avançado.
 */
const EFEITOS = [
  {
    id: "alta",
    rotulo: "Prioridade alta",
    desc: "Roda antes das demais.",
    monta: () => ({ effect: "priority", delta: -100 }),
    casa: (r) => r.effect === "priority" && r.delta < 0,
  },
  {
    id: "baixa",
    rotulo: "Prioridade baixa",
    desc: "Fica para o fim da fila.",
    monta: () => ({ effect: "priority", delta: 100 }),
    casa: (r) => r.effect === "priority" && r.delta > 0,
  },
  {
    id: "exclude",
    rotulo: "Ignorar",
    desc: "O Regente não pega essas tasks.",
    monta: () => ({ effect: "exclude", delta: 0 }),
    casa: (r) => r.effect === "exclude",
  },
  {
    id: "require",
    rotulo: "Somente estas",
    desc: "Havendo uma regra dessas, só o que ela aceita entra na fila.",
    monta: () => ({ effect: "require", delta: 0 }),
    casa: (r) => r.effect === "require",
  },
];

const efeitoDe = (r) => EFEITOS.find((e) => e.casa(r)) || EFEITOS[0];

const NOVA = {
  name: "",
  field: "title",
  match: "contains",
  value: "",
  effect: "priority",
  delta: -100,
};

export default function Regras() {
  const { api } = useRegente();
  const leitura = useLeitura([api("/settings"), api("/queue")]);

  return (
    <Leitura estado={leitura} oQue="as regras">
      {(cfg, fila) => (
        <Editor
          campo={cfg.fields.selection}
          fila={fila}
        />
      )}
    </Leitura>
  );
}

function Editor({ campo, fila }) {
  const { api, podeAqui, recarregar } = useRegente();
  const pode = podeAqui("workspace.settings.write");
  const [feedback, diga] = useFeedback();
  const [confirmar, setConfirmar] = useState(null);
  const [editando, setEditando] = useState(null);

  // O que está gravado. Guardado em estado para o rascunho da edição não ser
  // apagado pela releitura de cinco em cinco segundos.
  const gravadas = Array.isArray(campo.value) ? campo.value : [];
  const [regras, setRegras] = useState(gravadas);
  const [sujo, setSujo] = useState(false);

  useEffect(() => {
    if (!sujo) setRegras(gravadas);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(gravadas)]);

  async function salvar(proximas, oQueMudou) {
    diga.indo("Salvando…");
    const r = await post(api("/settings/selection"), { value: proximas });
    if (!r.ok) {
      // A mensagem do motor nomeia o campo ou o efeito que não existe — ela é
      // mais útil que qualquer frase genérica que a tela pudesse inventar.
      diga.erro(digaOErro(r));
      return false;
    }
    setSujo(false);
    diga.ok(oQueMudou);
    recarregar();
    return true;
  }

  const aplicar = (proximas, msg) => {
    setRegras(proximas);
    setSujo(true);
    return salvar(proximas, msg);
  };

  const mover = (i, passo) => {
    const j = i + passo;
    if (j < 0 || j >= regras.length) return;
    const p = [...regras];
    [p[i], p[j]] = [p[j], p[i]];
    aplicar(p, "Ordem das regras salva.");
  };

  return (
    <>
      <p className="muted">
        O Regente pega sempre a task de menor prioridade, e usa a chave como
        desempate — a mesma ordem, toda vez. Estas regras somam à prioridade que
        veio do board.
      </p>

      <Procedencia campo={campo} chave="selection" />

      <Secao
        titulo="Suas regras"
        hint={
          regras.length
            ? `${regras.length} regra(s), aplicadas de cima para baixo`
            : undefined
        }
        acao={
          pode ? (
            <button
              className="btn btn-primary btn-sm"
              onClick={() => setEditando({ ...NOVA, _novo: true })}
            >
              + Criar regra
            </button>
          ) : null
        }
      >
        {regras.length ? (
          <div className="stack">
            {regras.map((r, i) => (
              <CartaoRegra
                key={`${r.name}-${i}`}
                regra={r}
                fila={fila}
                pode={pode}
                primeira={i === 0}
                ultima={i === regras.length - 1}
                aoSubir={() => mover(i, -1)}
                aoDescer={() => mover(i, +1)}
                aoEditar={() => setEditando({ ...r, _indice: i })}
                aoDuplicar={() =>
                  aplicar(
                    [
                      ...regras.slice(0, i + 1),
                      { ...r, name: `${r.name} (cópia)` },
                      ...regras.slice(i + 1),
                    ],
                    "Regra duplicada.",
                  )
                }
                aoRemover={() =>
                  setConfirmar({
                    titulo: `Remover “${r.name}”?`,
                    impacto:
                      "A ordem da fila volta a ser calculada sem esta regra no próximo ciclo. As tasks não são alteradas.",
                    rotuloOk: "Remover regra",
                    faz: () =>
                      aplicar(
                        regras.filter((_, k) => k !== i),
                        "Regra removida.",
                      ),
                  })
                }
              />
            ))}
          </div>
        ) : (
          <Painel>
            <Vazio
              icone="regras"
              titulo="Nenhuma regra configurada"
              texto="Sem regras, o Regente respeita a ordem que veio do board. Crie uma regra para dizer o que deve rodar primeiro — ou o que ele deve ignorar."
              acao={
                pode ? (
                  <button
                    className="btn btn-primary"
                    onClick={() => setEditando({ ...NOVA, _novo: true })}
                  >
                    Criar a primeira regra
                  </button>
                ) : (
                  <span className="hint">
                    Você não tem permissão para alterar a configuração deste
                    workspace.
                  </span>
                )
              }
            />
          </Painel>
        )}
        {feedback}
      </Secao>

      {editando && (
        <FormRegra
          inicial={editando}
          fila={fila}
          aoCancelar={() => setEditando(null)}
          aoSalvar={async (r) => {
            const proximas =
              editando._indice === undefined
                ? [...regras, r]
                : regras.map((x, k) => (k === editando._indice ? r : x));
            const foi = await aplicar(
              proximas,
              editando._indice === undefined ? "Regra criada." : "Regra salva.",
            );
            if (foi) setEditando(null);
          }}
        />
      )}

      <Previa fila={fila} />

      <Confirmar
        aberto={!!confirmar}
        titulo={confirmar?.titulo}
        impacto={confirmar?.impacto}
        rotuloOk={confirmar?.rotuloOk}
        aoCancelar={() => setConfirmar(null)}
        aoConfirmar={() => {
          confirmar.faz();
          setConfirmar(null);
        }}
      />
    </>
  );
}

/**
 * Uma regra, dita em português, com quantas tasks ela pega agora.
 *
 * O impacto vem de `/queue`, que reavalia com as regras de AGORA. Mostrar a
 * contagem é o que permite alguém perceber que escreveu "FAXNA" antes de ligar
 * o Regente e ver a fila sair errada.
 */
function CartaoRegra({
  regra,
  fila,
  pode,
  primeira,
  ultima,
  aoSubir,
  aoDescer,
  aoEditar,
  aoDuplicar,
  aoRemover,
}) {
  const efeito = efeitoDe(regra);
  const pegas = tasksDaRegra(regra, fila);

  return (
    <article className="regra">
      <div className="regra-cabeca">
        <div>
          <h3>{regra.name || "Regra sem nome"}</h3>
          <p className="frase">{fraseDaRegra(regra)}</p>
        </div>
        <span className="spacer" />
        <span
          className="badge"
          data-tone={
            efeito.id === "alta" ? "ok" : efeito.id === "exclude" ? "idle" : "info"
          }
        >
          {efeito.rotulo}
        </span>
      </div>

      <div className="regra-impacto">
        {pegas.length ? (
          <>
            <strong>{pegas.length}</strong> task(s) combinam com esta regra
            agora:{" "}
            <span className="dim">
              {pegas
                .slice(0, 4)
                .map((t) => t.key)
                .join(", ")}
              {pegas.length > 4 ? ` e mais ${pegas.length - 4}` : ""}
            </span>
          </>
        ) : (
          <span className="dim">
            Nenhuma task combina com esta regra agora. Ela não está errada por
            isso — pode ser que a task ainda não exista.
          </span>
        )}
      </div>

      {pode && (
        <div className="regra-acoes">
          <button className="btn btn-sm" onClick={aoEditar}>
            Editar
          </button>
          <button className="btn btn-sm btn-ghost" onClick={aoDuplicar}>
            Duplicar
          </button>
          <button
            className="btn btn-sm btn-ghost"
            onClick={aoSubir}
            disabled={primeira}
            title="Mover para cima"
            aria-label={`Mover ${regra.name} para cima`}
          >
            ↑
          </button>
          <button
            className="btn btn-sm btn-ghost"
            onClick={aoDescer}
            disabled={ultima}
            title="Mover para baixo"
            aria-label={`Mover ${regra.name} para baixo`}
          >
            ↓
          </button>
          <span className="spacer" />
          <button className="btn btn-sm btn-danger" onClick={aoRemover}>
            Remover
          </button>
        </div>
      )}
    </article>
  );
}

/**
 * Quais tasks esta regra pegou, segundo a PRÉVIA do servidor.
 *
 * A tela não reavalia a regra: ela lê o motivo que o motor já escreveu em cada
 * linha da prévia (`why` e `excluded_by`). Reavaliar aqui seria uma segunda
 * implementação da seleção, e a que divergisse seria a que a pessoa vê.
 */
function tasksDaRegra(regra, fila) {
  const nome = regra.name;
  const todas = [...(fila.queue || []), ...(fila.excluded || [])];
  return todas.filter(
    (t) =>
      t.excluded_by === nome ||
      (t.why || []).some((m) => String(m).startsWith(`${nome}:`)),
  );
}

function FormRegra({ inicial, fila, aoCancelar, aoSalvar }) {
  const [r, setR] = useState(() => ({ ...inicial }));
  const [avancado, setAvancado] = useState(false);
  const em = (k, v) => setR((x) => ({ ...x, [k]: v }));
  const efeito = efeitoDe(r);

  // `in` compara com uma LISTA. A pessoa digita "FAXINA, URGENTE" e a tela monta
  // o array — pedir `["FAXINA","URGENTE"]` é pedir que ela aprenda JSON.
  const valorTexto = Array.isArray(r.value) ? r.value.join(", ") : r.value ?? "";

  const montar = () => {
    const valor =
      r.match === "in"
        ? String(valorTexto)
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean)
        : ["lt", "gt"].includes(r.match)
          ? Number(valorTexto)
          : valorTexto;
    return {
      name: r.name.trim() || `${efeito.rotulo}: ${valorTexto}`.slice(0, 60),
      field: r.field,
      match: r.match,
      value: valor,
      effect: r.effect,
      delta: Number(r.delta) || 0,
    };
  };

  const previa = montar();
  const pegaria = tasksDaRegra({ ...previa, name: inicial.name }, fila);

  return (
    <Gaveta
      aberta
      titulo={inicial._novo ? "Criar regra" : "Editar regra"}
      sub="Uma condição, e o que o Regente deve fazer quando ela casar."
      aoFechar={aoCancelar}
      rodape={
        <>
          <button className="btn" onClick={aoCancelar}>
            Cancelar
          </button>
          <button
            className="btn btn-primary"
            disabled={!String(valorTexto).trim()}
            onClick={() => aoSalvar(montar())}
          >
            {inicial._novo ? "Criar regra" : "Salvar regra"}
          </button>
        </>
      }
    >
      <div className="field">
        <label htmlFor="regra-nome">Nome desta regra</label>
        <input
          id="regra-nome"
          className="input"
          value={r.name}
          placeholder="Faxina primeiro"
          onChange={(e) => em("name", e.target.value)}
        />
        <span className="help">
          Só para você reconhecer a regra depois. Em branco, a tela dá um nome.
        </span>
      </div>

      <fieldset className="grupo">
        <legend>Quando</legend>
        <div className="condicao-linha">
          <div className="field">
            <label htmlFor="regra-campo">O que olhar</label>
            <select
              id="regra-campo"
              className="input"
              value={r.field}
              onChange={(e) => em("field", e.target.value)}
            >
              {CAMPOS.map(([v, l]) => (
                <option key={v} value={v}>
                  {l}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="regra-match">Como comparar</label>
            <select
              id="regra-match"
              className="input"
              value={r.match}
              onChange={(e) => em("match", e.target.value)}
            >
              {COMPARACOES.map(([v, l]) => (
                <option key={v} value={v}>
                  {l}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="regra-valor">
              {r.match === "in" ? "Um destes valores" : "Valor"}
            </label>
            <input
              id="regra-valor"
              className="input"
              value={valorTexto}
              placeholder={r.match === "in" ? "FAXINA, URGENTE" : "FAXINA"}
              onChange={(e) => em("value", e.target.value)}
            />
            {r.match === "in" && (
              <span className="help">Separe por vírgula.</span>
            )}
          </div>
        </div>
        <p className="hint">
          Uma condição por regra. Para combinar critérios, crie mais de uma
          regra — os efeitos de prioridade se somam, e “ignorar” vence tudo.
        </p>
      </fieldset>

      <fieldset className="grupo">
        <legend>O Regente deve</legend>
        <div className="opcoes">
          {EFEITOS.map((e) => (
            <label
              key={e.id}
              className={`opcao${efeito.id === e.id ? " escolhida" : ""}`}
            >
              <input
                type="radio"
                name="efeito"
                checked={efeito.id === e.id}
                onChange={() => setR((x) => ({ ...x, ...e.monta() }))}
              />
              <span>
                <span className="t">{e.rotulo}</span>
                <span className="d">{e.desc}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      <details
        className="tech"
        open={avancado}
        onToggle={(e) => setAvancado(e.target.open)}
      >
        <summary>Opções avançadas</summary>
        <div className="tech-body avancado">
          <div className="field">
            <label htmlFor="regra-delta">Quanto muda a prioridade</label>
            <input
              id="regra-delta"
              className="input"
              type="number"
              value={r.delta}
              disabled={r.effect !== "priority"}
              onChange={(e) => em("delta", e.target.value)}
            />
            <span className="help">
              Número menor roda antes. O motor aceita de −1000 a 1000, e recusa
              uma regra de prioridade que não mude nada.
            </span>
          </div>
        </div>
      </details>

      <div className="resumo-regra">
        <span className="rot">Em português</span>
        <p>{fraseDaRegra(previa)}</p>
        <span className="dim">
          {pegaria.length
            ? `Combina com ${pegaria.length} task(s) conhecidas agora.`
            : "Nenhuma task conhecida combina com isto agora."}
        </span>
      </div>

    </Gaveta>
  );
}

const COLUNAS = [
  {
    rot: "Task",
    classe: "key",
    corpo: (t) => <Titulo chave={t.key} sub={t.title} />,
  },
  {
    rot: "No board",
    classe: "dim",
    corpo: (t) => <Dito valor={t.external_status} ausente="—" />,
  },
  {
    rot: "Prioridade",
    num: true,
    corpo: (t) =>
      t.eligible === false ? (
        <span className="dim">Fora</span>
      ) : (
        <strong>{t.priority}</strong>
      ),
  },
  {
    rot: "Por quê",
    classe: "dim",
    corpo: (t) =>
      t.excluded_by ? (
        <>Excluída pela regra “{t.excluded_by}”</>
      ) : (t.why || []).length ? (
        t.why.join("; ")
      ) : (
        "Prioridade que veio do board"
      ),
  },
];

/**
 * A fila resultante, reavaliada com as regras de AGORA.
 *
 * Mostrar o veredito guardado faria editar uma regra e não ver nada mudar — a
 * confusão exata que esta página existe para evitar. Nada é executado aqui: é
 * leitura.
 */
function Previa({ fila }) {
  const linhas = [
    ...(fila.queue || []),
    ...(fila.excluded || []).map((t) => ({ ...t, eligible: false })),
  ];
  const naoAplicadas = (fila.queue || []).filter((t) => t.applied === false).length;

  return (
    <Secao
      titulo="Como a fila fica"
      hint={`Sobre ${fila.discovered} task(s) já descobertas. Nada é executado aqui.`}
    >
      {naoAplicadas > 0 && (
        <Alerta
          tone="info"
          icone="i"
          titulo="Ainda não aplicado"
          detalhe={`${naoAplicadas} task(s) só passam a usar estas regras no próximo ciclo do Regente.`}
        />
      )}
      <Painel flush>
        <Tabela
          colunas={COLUNAS}
          linhas={linhas}
          chave={(t) => t.key}
          vazio={
            <Vazio
              icone="regras"
              titulo="Nenhuma task descoberta ainda"
              texto="O Regente precisa rodar ao menos um ciclo para ler o board. Assim que ele rodar, esta prévia mostra a ordem que as suas regras produzem."
            />
          }
        />
      </Painel>
    </Secao>
  );
}

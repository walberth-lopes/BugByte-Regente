// Quem pode o quê neste workspace.
//
// A página mostra PAPÉIS e o que cada um permite fazer, em português. Os nomes
// internos (`workspace.engine.control`) continuam disponíveis em "Ver detalhes
// técnicos" — quem investiga uma recusa precisa deles, e quem só quer dar
// acesso a um colega não.
//
// Revogar não apaga a história: a concessão continua listada, marcada como
// revogada, com quem revogou e quando.

import { useState } from "react";
import { post, del } from "../api.js";
import { useRegente, useLeitura } from "../estado.jsx";
import {
  Leitura,
  Painel,
  Secao,
  Tabela,
  Tecnico,
  Titulo,
  Vazio,
} from "../ui.jsx";
import { Confirmar, useFeedback } from "../components/Formulario.jsx";
import { ACAO, digaOErro, when } from "../present.js";
import { SemAcesso, ComoConceder } from "../components/Acesso.jsx";
import { chaveDaSessao } from "../estado.jsx";

/** Os papéis do motor (`core/access.py::ROLES`), ditos pelo que permitem. */
const PAPEIS = [
  {
    id: "operator",
    rotulo: "Operador",
    desc: "Opera o Regente no dia a dia.",
    faz: [
      "Ligar, pausar e parar o processamento",
      "Decidir quando o Regente não consegue seguir sozinho",
    ],
  },
  {
    id: "admin",
    rotulo: "Administrador",
    desc: "Cuida de quem entra e de como o workspace é configurado.",
    faz: [
      "Dar e tirar acesso de outras pessoas",
      "Alterar conexões, status e regras",
    ],
  },
  {
    id: "keeper",
    rotulo: "Guardião de credenciais",
    desc: "Cuida das autorizações de acesso a serviços externos.",
    faz: ["Registrar e revogar credenciais", "Ver quais credenciais existem"],
  },
  {
    id: "service",
    rotulo: "Serviço",
    desc: "Uma identidade de máquina, e não uma pessoa.",
    faz: ["Usar credenciais durante um ciclo — e nada além disso"],
  },
  {
    id: "owner",
    rotulo: "Dono",
    desc: "Tudo acima.",
    faz: ["Todas as permissões deste workspace"],
  },
];

/** O papel que melhor descreve um conjunto de permissões concedido. */
function papelAparente(abilities) {
  const tem = (p) => abilities.some((a) => a.startsWith(p));
  const opera = abilities.includes("workspace.engine.control");
  const administra = abilities.includes("workspace.access.grant");
  const guarda = abilities.includes("workspace.credential.grant");
  if (opera && administra && guarda) return "Dono";
  if (administra) return "Administrador";
  if (guarda) return "Guardião de credenciais";
  if (opera) return "Operador";
  if (tem("workspace.credential.use")) return "Serviço";
  return "Acesso limitado";
}

export default function Acesso() {
  const { api, podeAqui } = useRegente();
  const pode = podeAqui("workspace.access.list");
  const leitura = useLeitura(api("/access"), { pular: !pode });

  if (!pode) {
    return (
      <SemAcesso
        area="as permissões deste workspace"
        capacidade="workspace.access.list"
      />
    );
  }

  return (
    <Leitura estado={leitura} oQue="as permissões">
      {({ access }) => <Conteudo concessoes={access} />}
    </Leitura>
  );
}

function Conteudo({ concessoes }) {
  const { api, podeAqui, recarregar, identidade } = useRegente();
  const podeConceder = podeAqui("workspace.access.grant");
  const podeRevogar = podeAqui("workspace.access.revoke");
  const [feedback, diga] = useFeedback();
  const [confirmar, setConfirmar] = useState(null);
  const [criando, setCriando] = useState(false);

  async function revogar(g) {
    diga.indo("Revogando…");
    const r = await del(api(`/access/${encodeURIComponent(g.principal)}`));
    if (!r.ok) {
      diga.erro(digaOErro(r));
      return;
    }
    diga.ok(`Acesso de ${g.principal} revogado.`);
    recarregar();
  }

  const COLUNAS = [
    {
      rot: "Quem",
      classe: "key",
      corpo: (g) => (
        <Titulo chave={g.principal} sub={`Identificado por ${g.provider}`} />
      ),
    },
    {
      rot: "Papel",
      corpo: (g) => (
        <>
          <strong>{papelAparente(g.abilities || [])}</strong>
          <div className="dim" style={{ fontSize: "var(--fs-xs)" }}>
            {(g.abilities || []).length} permissão(ões)
          </div>
        </>
      ),
    },
    {
      rot: "Pode",
      corpo: (g) => (
        <ul className="perms">
          {(g.abilities || []).map((a) => (
            <li key={a}>
              <span className="yes" aria-hidden="true">
                ✓
              </span>
              {ACAO[a] || a}
            </li>
          ))}
        </ul>
      ),
    },
    {
      rot: "Concedido",
      classe: "dim",
      corpo: (g) => (
        <>
          {g.granted_by}
          <br />
          {when(g.granted_at)}
        </>
      ),
    },
    {
      rot: "Situação",
      corpo: (g) =>
        g.active ? (
          <span className="badge" data-tone="ok">
            Ativo
          </span>
        ) : (
          <>
            <span className="badge" data-tone="idle">
              Revogado
            </span>
            <div className="dim" style={{ fontSize: "var(--fs-xs)" }}>
              por {g.revoked_by} em {when(g.revoked_at)}
            </div>
          </>
        ),
    },
    {
      rot: "Ações",
      oculto: true,
      corpo: (g) =>
        g.active && podeRevogar ? (
          <button
            className="btn btn-sm btn-danger"
            onClick={() =>
              setConfirmar({
                titulo: `Revogar o acesso de ${g.principal}?`,
                impacto:
                  "A partir de agora essa identidade não consegue mais agir neste workspace. O que ela já fez continua na trilha, e a concessão fica registrada como revogada.",
                rotuloOk: "Revogar acesso",
                faz: () => revogar(g),
              })
            }
          >
            Revogar
          </button>
        ) : null,
    },
  ];

  return (
    <>
      <p className="muted">
        Estar autenticado responde <em>quem é você</em>; ter uma concessão
        responde <em>o que você pode fazer aqui</em>. As duas coisas são
        separadas de propósito — e é por isso que abrir esta tela não dá poder
        nenhum a ninguém.
      </p>

      <Secao
        titulo="Quem tem acesso"
        acao={
          podeConceder && !criando ? (
            <button
              className="btn btn-primary btn-sm"
              onClick={() => setCriando(true)}
            >
              + Dar acesso
            </button>
          ) : null
        }
      >
        <Painel flush>
          <Tabela
            colunas={COLUNAS}
            linhas={concessoes}
            chave={(g) => g.id}
            vazio={
              <Vazio
                icone="acesso"
                titulo="Nenhuma concessão registrada"
                texto="Ninguém recebeu acesso a este workspace ainda. A primeira concessão é feita no terminal — é a única que não passa por outra pessoa."
                acao={<ComoConceder chave={chaveDaSessao(identidade)} />}
              />
            }
          />
        </Painel>
        {feedback}
      </Secao>

      {criando && (
        <FormAcesso
          aoFechar={() => setCriando(false)}
          aoPronto={(m) => {
            diga.ok(m);
            setCriando(false);
          }}
        />
      )}

      <Tecnico titulo="O que cada permissão significa">
        <dl>
          {Object.entries(ACAO).map(([k, v]) => (
            <div key={k} style={{ display: "contents" }}>
              <dt>
                <code>{k}</code>
              </dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      </Tecnico>

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

function FormAcesso({ aoFechar, aoPronto }) {
  const { api, recarregar } = useRegente();
  const [feedback, diga] = useFeedback();
  const [alvo, setAlvo] = useState("");
  const [papel, setPapel] = useState("operator");
  const [nota, setNota] = useState("");
  const [indo, setIndo] = useState(false);

  const escolhido = PAPEIS.find((p) => p.id === papel);
  const formaOk = /^[^:\s]+:[^\s]+$/.test(alvo.trim());

  async function salvar() {
    setIndo(true);
    diga.indo("Concedendo…");
    const r = await post(api("/access"), {
      principal: alvo.trim(),
      role: papel,
      note: nota,
    });
    setIndo(false);
    if (!r.ok) {
      diga.erro(digaOErro(r));
      return;
    }
    recarregar();
    aoPronto(`${alvo.trim()} agora é ${escolhido.rotulo.toLowerCase()} aqui.`);
  }

  return (
    <Painel className="form-acesso">
      <div className="row">
        <h2>Dar acesso a alguém</h2>
        <span className="spacer" />
        <button className="btn btn-ghost btn-sm" onClick={aoFechar}>
          Fechar
        </button>
      </div>

      <div className="field" style={{ marginTop: "var(--s-4)" }}>
        <label htmlFor="acesso-alvo">Identidade</label>
        <input
          id="acesso-alvo"
          className="input"
          value={alvo}
          placeholder="github:12345"
          onChange={(e) => setAlvo(e.target.value)}
          aria-describedby="acesso-alvo-ajuda"
        />
        <span className="help" id="acesso-alvo-ajuda">
          No formato <code>provedor:identificador</code> — o identificador
          estável que o provedor emite, e não o nome de exibição. Nomes mudam;
          identificadores não.
        </span>
        {alvo.trim() && !formaOk && (
          <span className="help" style={{ color: "var(--warn)" }}>
            Falta o provedor antes dos dois-pontos. Sem ele a identidade é
            ambígua, e uma chave ambígua casa com quem não devia.
          </span>
        )}
      </div>

      <fieldset className="grupo">
        <legend>O que essa pessoa poderá fazer</legend>
        <div className="opcoes">
          {PAPEIS.map((p) => (
            <label
              key={p.id}
              className={`opcao${papel === p.id ? " escolhida" : ""}`}
            >
              <input
                type="radio"
                name="papel"
                checked={papel === p.id}
                onChange={() => setPapel(p.id)}
              />
              <span>
                <span className="t">{p.rotulo}</span>
                <span className="d">{p.desc}</span>
                <ul className="perms">
                  {p.faz.map((f) => (
                    <li key={f}>
                      <span className="yes" aria-hidden="true">
                        ✓
                      </span>
                      {f}
                    </li>
                  ))}
                </ul>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      <div className="field">
        <label htmlFor="acesso-nota">Motivo</label>
        <input
          id="acesso-nota"
          className="input"
          value={nota}
          placeholder="Opcional"
          onChange={(e) => setNota(e.target.value)}
        />
        <span className="help">
          Fica na trilha, junto de quem concedeu e quando.
        </span>
      </div>

      {feedback}

      <div className="form-actions">
        <button className="btn" onClick={aoFechar}>
          Cancelar
        </button>
        <button
          className="btn btn-primary"
          disabled={!formaOk || indo}
          onClick={salvar}
        >
          Dar acesso
        </button>
        <span className="hint">
          As permissões ficam gravadas como estão hoje. Mudar a definição de um
          papel depois não altera quem já recebeu.
        </span>
      </div>
    </Painel>
  );
}

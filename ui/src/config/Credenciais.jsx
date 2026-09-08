// Credenciais: a autorização para o Regente falar com um serviço em seu nome.
//
// Duas coisas precisam ficar claras para quem nunca leu a arquitetura:
//
// 1. O REGENTE NÃO GUARDA O SEGREDO. Ele guarda o ENDEREÇO de onde buscá-lo —
//    uma variável de ambiente, um arquivo, um programa. Não existe tela nem
//    rota que devolva o valor, e esta página não pede um.
//
// 2. UMA CREDENCIAL NÃO AUTORIZA TUDO. Ela autoriza uma lista de coisas, e o
//    Regente recusa o resto mesmo quando o serviço permitiria. Por isso as
//    permissões são caixas marcáveis, e não um campo de texto: `repo.pr` não é
//    algo que se peça a alguém digitar.

import { useState } from "react";
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
} from "../ui.jsx";
import { Confirmar, useFeedback } from "../components/Formulario.jsx";
import { PERMISSAO, digaOErro, when } from "../present.js";
import { SemAcesso } from "../components/Acesso.jsx";

/** De onde o segredo pode vir. O Regente resolve o endereço na hora de usar. */
const FONTES = [
  {
    id: "env",
    rotulo: "Uma variável de ambiente",
    desc: "O segredo já está no ambiente desta máquina. É a forma mais comum.",
    rotuloCampo: "Nome da variável",
    exemplo: "JIRA_API_TOKEN",
    ajuda:
      "O Regente lê o valor desta variável no momento de usar. Ele não a copia para lugar nenhum.",
    monta: (v) => `env:${v}`,
  },
  {
    id: "arquivo",
    rotulo: "Um arquivo desta máquina",
    desc: "O segredo está guardado num arquivo, e só nele.",
    rotuloCampo: "Caminho do arquivo",
    exemplo: "C:/Users/voce/.regente/jira.token",
    ajuda:
      "O conteúdo do arquivo é lido no momento de usar. Guarde-o fora do repositório.",
    monta: (v) => `arquivo:${v}`,
  },
  {
    id: "helper",
    rotulo: "Um gerenciador de credenciais",
    desc: "Um programa produz o segredo na hora. Nada fica guardado em disco.",
    rotuloCampo: "Nome do gerenciador",
    exemplo: "github",
    ajuda:
      "O nome precisa estar declarado em `helpers:` no arquivo de configuração, que é onde se diz qual programa executar.",
    monta: (v) => `helper:${v}`,
  },
];

const leFonte = (ref) => {
  const [esquema, ...resto] = String(ref || "").split(":");
  return { esquema, valor: resto.join(":") };
};

/** As permissões que se pode conceder, agrupadas pelo que a pessoa quer fazer. */
const GRUPOS = [
  {
    titulo: "Board de tasks",
    usos: ["task.read", "task.write"],
  },
  {
    titulo: "Repositório",
    usos: ["repo.read", "repo.push", "repo.pr"],
  },
  {
    titulo: "Integração contínua e agente",
    usos: ["ci.read", "agent.run"],
  },
];

const RISCO = {
  "task.write": "Altera o seu board.",
  "repo.push": "Envia commits para o repositório.",
  "repo.pr": "Abre pull requests em seu nome.",
};

export default function Credenciais() {
  const { api, podeAqui } = useRegente();
  const pode = podeAqui("workspace.credential.list");
  const leitura = useLeitura([api("/credentials"), api("/connections")], {
    pular: !pode,
  });

  // Sem a capacidade de listar, a chamada volta recusada — e não adianta
  // fazê-la só para traduzir a recusa depois.
  if (!pode) {
    return <SemAcesso area="as credenciais" capacidade="workspace.credential.list" />;
  }

  return (
    <Leitura estado={leitura} oQue="as credenciais">
      {(c, con) => <Conteudo itens={c.credentials} conexoes={con.connections} />}
    </Leitura>
  );
}

function Conteudo({ itens, conexoes }) {
  const { api, podeAqui, recarregar } = useRegente();
  const podeRegistrar = podeAqui("workspace.credential.grant");
  const podeRevogar = podeAqui("workspace.credential.revoke");
  const [feedback, diga] = useFeedback();
  const [confirmar, setConfirmar] = useState(null);
  const [criando, setCriando] = useState(false);

  const faltando = conexoes.filter(
    (c) => c.needs_credential && !c.live_credentials && c.adapter,
  );

  async function revogar(c) {
    diga.indo("Revogando…");
    const r = await del(api(`/credentials/${encodeURIComponent(c.id)}`));
    if (!r.ok) {
      diga.erro(digaOErro(r));
      return;
    }
    diga.ok(`Credencial de ${c.provider} revogada.`);
    recarregar();
  }

  const COLUNAS = [
    {
      rot: "Credencial",
      classe: "key",
      corpo: (c) => (
        <Titulo
          chave={c.provider}
          sub={`${c.name} · registrada por ${c.granted_by}`}
        />
      ),
    },
    { rot: "Situação", corpo: (c) => <Badge termo={c.status} /> },
    {
      rot: "Permite",
      corpo: (c) =>
        (c.capabilities || []).length ? (
          <ul className="perms">
            {c.capabilities.map((u) => (
              <li key={u}>
                <span className="yes" aria-hidden="true">
                  ✓
                </span>
                {PERMISSAO[u] || u}
              </li>
            ))}
          </ul>
        ) : (
          <span className="dim">Nada</span>
        ),
    },
    {
      rot: "Onde está o segredo",
      corpo: (c) => (
        <>
          <code>{c.secret_ref}</code>
          <div className="dim" style={{ fontSize: "var(--fs-xs)" }}>
            O Regente guarda só este endereço.
          </div>
        </>
      ),
    },
    {
      rot: "Vence",
      classe: "dim",
      corpo: (c) => (c.expires_at ? when(c.expires_at) : "Sem validade"),
    },
    {
      rot: "Ações",
      oculto: true,
      corpo: (c) =>
        c.status !== "REVOKED" && podeRevogar ? (
          <button
            className="btn btn-sm btn-danger"
            onClick={() =>
              setConfirmar({
                titulo: `Revogar a credencial de ${c.provider}?`,
                impacto:
                  "O Regente para imediatamente de alcançar este serviço, e tudo que depender dele fica parado até você registrar outra. O registro da revogação fica na trilha — nada é apagado.",
                rotuloOk: "Revogar credencial",
                faz: () => revogar(c),
              })
            }
          >
            Revogar
          </button>
        ) : c.revoked_by ? (
          <span className="dim">Revogada por {c.revoked_by}</span>
        ) : null,
    },
  ];

  return (
    <>
      <p className="muted">
        Uma credencial dá ao Regente autoridade para falar com um serviço em seu
        nome. <strong>O Regente nunca guarda o seu segredo</strong> — ele guarda
        o endereço de onde buscá-lo, e não existe tela nem rota que o devolva.
      </p>

      {faltando.length > 0 && (
        <Alerta
          tone="warn"
          titulo={`${faltando.length} conexão(ões) esperando credencial`}
          detalhe={`${faltando
            .map((c) => c.adapter)
            .join(", ")} está configurado e o Regente ainda não tem autorização para usá-lo.`}
        />
      )}

      <Secao
        titulo="Credenciais registradas"
        acao={
          podeRegistrar && !criando ? (
            <button
              className="btn btn-primary btn-sm"
              onClick={() => setCriando(true)}
            >
              + Registrar credencial
            </button>
          ) : null
        }
      >
        <Painel flush>
          <Tabela
            colunas={COLUNAS}
            linhas={itens}
            chave={(c) => c.id}
            vazio={
              <Vazio
                icone="credencial"
                titulo="Nenhuma credencial registrada"
                texto="Sem credencial, o Regente só alcança o que já está nesta máquina. Registre uma para ele poder ler o seu board ou publicar mudanças."
                acao={
                  podeRegistrar ? (
                    <button
                      className="btn btn-primary"
                      onClick={() => setCriando(true)}
                    >
                      Registrar a primeira
                    </button>
                  ) : (
                    <span className="hint">
                      Você não tem permissão para registrar credenciais aqui.
                    </span>
                  )
                }
              />
            }
          />
        </Painel>
        {feedback}
      </Secao>

      {criando && (
        <FormCredencial
          conexoes={conexoes}
          aoFechar={() => setCriando(false)}
          aoPronto={(msg) => {
            diga.ok(msg);
            setCriando(false);
          }}
        />
      )}

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

function FormCredencial({ conexoes, aoFechar, aoPronto }) {
  const { api, recarregar } = useRegente();
  const [feedback, diga] = useFeedback();
  const [papel, setPapel] = useState(conexoes[0]?.role || "tasks");
  const [nome, setNome] = useState("principal");
  const [fonte, setFonte] = useState("env");
  const [ondeEsta, setOndeEsta] = useState("");
  const [usos, setUsos] = useState([]);
  const [dias, setDias] = useState("");
  const [indo, setIndo] = useState(false);

  const f = FONTES.find((x) => x.id === fonte);
  const conexao = conexoes.find((c) => c.role === papel);

  const alterna = (u) =>
    setUsos((x) => (x.includes(u) ? x.filter((y) => y !== u) : [...x, u]));

  async function salvar() {
    setIndo(true);
    diga.indo("Registrando…");
    const corpo = {
      provider: papel,
      name: nome.trim() || "principal",
      // O que vai para o servidor é o ENDEREÇO, montado a partir da escolha.
      // O valor do segredo não passa por esta tela em nenhum momento.
      secret_ref: f.monta(ondeEsta.trim()),
      capabilities: usos,
    };
    const n = parseInt(dias, 10);
    if (Number.isFinite(n) && n > 0) corpo.expires_in_days = n;

    const r = await post(api("/credentials"), corpo);
    setIndo(false);
    if (!r.ok) {
      diga.erro(digaOErro(r));
      return;
    }
    recarregar();
    aoPronto(`Credencial de ${papel} registrada.`);
  }

  return (
    <Painel className="form-credencial">
      <div className="row">
        <h2>Registrar credencial</h2>
        <span className="spacer" />
        <button className="btn btn-ghost btn-sm" onClick={aoFechar}>
          Fechar
        </button>
      </div>

      <fieldset className="grupo">
        <legend>Para qual conexão</legend>
        <div className="form-row">
          <div className="field">
            <label htmlFor="cred-papel">Conexão</label>
            <select
              id="cred-papel"
              className="input"
              value={papel}
              onChange={(e) => setPapel(e.target.value)}
            >
              {conexoes.map((c) => (
                <option key={c.role} value={c.role}>
                  {c.adapter ? `${c.role} — ${c.adapter}` : c.role}
                </option>
              ))}
            </select>
            {conexao && !conexao.needs_credential && (
              <span className="help" style={{ color: "var(--warn)" }}>
                Este serviço não precisa de credencial. Registrar uma aqui não
                faz mal, e também não muda nada.
              </span>
            )}
          </div>
          <div className="field">
            <label htmlFor="cred-nome">Um nome para você</label>
            <input
              id="cred-nome"
              className="input"
              value={nome}
              onChange={(e) => setNome(e.target.value)}
            />
            <span className="help">
              Para distinguir duas credenciais do mesmo serviço.
            </span>
          </div>
          <div className="field">
            <label htmlFor="cred-dias">Validade em dias</label>
            <input
              id="cred-dias"
              className="input"
              type="number"
              min="1"
              value={dias}
              placeholder="30"
              onChange={(e) => setDias(e.target.value)}
            />
            <span className="help">Em branco: sem validade.</span>
          </div>
        </div>
      </fieldset>

      <fieldset className="grupo">
        <legend>Onde está o segredo</legend>
        <div className="opcoes">
          {FONTES.map((o) => (
            <label
              key={o.id}
              className={`opcao${fonte === o.id ? " escolhida" : ""}`}
            >
              <input
                type="radio"
                name="fonte"
                checked={fonte === o.id}
                onChange={() => setFonte(o.id)}
              />
              <span>
                <span className="t">{o.rotulo}</span>
                <span className="d">{o.desc}</span>
              </span>
            </label>
          ))}
        </div>

        <div className="field" style={{ marginTop: "var(--s-4)" }}>
          <label htmlFor="cred-onde">{f.rotuloCampo}</label>
          <input
            id="cred-onde"
            className="input"
            value={ondeEsta}
            placeholder={f.exemplo}
            onChange={(e) => setOndeEsta(e.target.value)}
          />
          <span className="help">{f.ajuda}</span>
        </div>

        <Alerta
          tone="info"
          icone="i"
          titulo="Não cole o segredo aqui"
          detalhe="Este campo é um endereço, não um valor. O Regente vai até lá no momento de usar — e assim o segredo nunca entra no banco de dados dele nem passa por esta página."
        />
      </fieldset>

      <fieldset className="grupo">
        <legend>O que esta credencial pode fazer</legend>
        <p className="hint">
          Marque só o necessário. O Regente recusa o que não estiver marcado,
          mesmo que o serviço permitiria.
        </p>
        {GRUPOS.map((g) => (
          <div className="grupo-perms" key={g.titulo}>
            <div className="grupo-titulo">{g.titulo}</div>
            {g.usos.map((u) => (
              <label className="check-inline" key={u}>
                <input
                  type="checkbox"
                  checked={usos.includes(u)}
                  onChange={() => alterna(u)}
                />
                <span>
                  {PERMISSAO[u]}
                  {RISCO[u] && <span className="dim"> — {RISCO[u]}</span>}
                </span>
              </label>
            ))}
          </div>
        ))}
      </fieldset>

      {feedback}

      <div className="form-actions">
        <button className="btn" onClick={aoFechar}>
          Cancelar
        </button>
        <button
          className="btn btn-primary"
          disabled={!ondeEsta.trim() || !usos.length || indo}
          onClick={salvar}
        >
          Registrar credencial
        </button>
        {!usos.length && (
          <span className="hint">
            Escolha ao menos uma permissão — uma credencial que não autoriza nada
            é recusada pelo motor.
          </span>
        )}
      </div>
    </Painel>
  );
}

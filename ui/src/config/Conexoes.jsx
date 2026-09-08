// As conexões: com o que o Regente fala em seu nome.
//
// Cada papel — board de tasks, repositório, agente — é um cartão com o estado
// REAL. "Conectado" aqui nunca significa "existe configuração": significa que
// há credencial viva com a capacidade necessária, ou que o provedor não precisa
// de nenhuma. Essa distinção é o que impede alguém de achar que ligou o Jira
// porque escreveu o endereço dele.
//
// O formulário de cada provedor vem do CATÁLOGO, que o servidor monta a partir
// da camada de adapters. A tela não sabe o que um Jira precisa; ela pergunta.

import { useEffect, useState } from "react";
import { get, post } from "../api.js";
import { useRegente, useLeitura } from "../estado.jsx";
import {
  Alerta,
  Badge,
  Dado,
  Leitura,
  Link,
  Painel,
  Secao,
  Tecnico,
  Vazio,
} from "../ui.jsx";
import { Campo, Confirmar, useFeedback } from "../components/Formulario.jsx";
import { ALCANCE, PERMISSAO, USO_POR_PAPEL, digaOErro, rotulo } from "../present.js";
import { Icon } from "../Icon.jsx";
import Procedencia from "./Procedencia.jsx";

/** O ícone de cada papel. Reconhecer o cartão num relance é mais rápido que
 *  ler o título de todos eles. */
const ICONE = {
  tasks: "jira",
  repository: "ramo",
  repository_write: "entregas",
  cicd: "teste",
  runner: "agente",
  workspace_provider: "workspace",
};

export default function Conexoes() {
  const { api } = useRegente();
  const leitura = useLeitura([api("/connections"), api("/settings"), "/api/catalog"]);

  return (
    <Leitura estado={leitura} oQue="as conexões">
      {(con, cfg, cat) => (
        <Conteudo
          conexoes={con.connections}
          campo={cfg.fields.providers}
          papeis={cat.roles}
        />
      )}
    </Leitura>
  );
}

function Conteudo({ conexoes, campo, papeis }) {
  const { podeAqui } = useRegente();
  const pode = podeAqui("workspace.settings.write");
  const [editando, setEditando] = useState(null);
  const configurado = campo.value && typeof campo.value === "object" ? campo.value : {};

  const daConexao = (papel) => conexoes.find((c) => c.role === papel) || {};

  return (
    <>
      <p className="muted">
        Conexões são os serviços com que o Regente conversa em seu nome. Uma
        conexão declarada mas sem credencial válida <strong>não funciona</strong>{" "}
        — e aparece assim aqui, nunca como “conectada”.
      </p>

      <Procedencia campo={campo} chave="providers" />

      <div className="providers">
        {papeis.map((p) => (
          <Cartao
            key={p.role}
            papel={p}
            conexao={daConexao(p.role)}
            atual={configurado[p.role] || null}
            pode={pode}
            aoConfigurar={() => setEditando(p.role)}
          />
        ))}
      </div>

      {editando && (
        <FormConexao
          papel={papeis.find((p) => p.role === editando)}
          configurado={configurado}
          aoFechar={() => setEditando(null)}
        />
      )}
    </>
  );
}

function Cartao({ papel, conexao, atual, pode, aoConfigurar }) {
  const { api } = useRegente();
  const [prova, setProva] = useState(null);
  const [indo, setIndo] = useState(false);

  const oferta = papel.options.find((o) => o.name === (atual?.name || conexao.adapter));

  async function testar() {
    setIndo(true);
    setProva({ estado: "indo" });
    const r = await post(api("/credentials/conexao/test"), {
      provider: papel.role,
      use: oferta?.credential_use || USO_POR_PAPEL[papel.role] || "repo.read",
    });
    setIndo(false);
    if (!r.ok) {
      setProva({ estado: "erro", texto: digaOErro(r) });
      return;
    }
    setProva({ estado: "ok", d: r.payload });
  }

  return (
    <article className="provider">
      <div className="top">
        <span className="bolha">
          <Icon nome={ICONE[papel.role] || "conexao"} tamanho={20} />
        </span>
        <h3>{papel.label}</h3>
        <Badge termo={conexao.state || "NAO_CONFIGURADO"} />
      </div>
      <p className="desc">{papel.description}</p>

      {oferta ? (
        <div className="provider-atual">
          <strong>{oferta.label}</strong>
          {atual &&
            Object.entries(atual)
              .filter(([k]) => k !== "name")
              .slice(0, 2)
              .map(([k, v]) => (
                <div className="dim" key={k}>
                  {String(v)}
                </div>
              ))}
        </div>
      ) : (
        <p className="dim">
          {papel.essential
            ? "Nenhum serviço escolhido. Sem isto o Regente não tem o que fazer."
            : "Nenhum serviço escolhido para este papel."}
        </p>
      )}

      {conexao.needs_credential === false && oferta && (
        <p className="hint">Este serviço não precisa de credencial.</p>
      )}

      {(conexao.capabilities || []).length > 0 && (
        <ul className="perms">
          {conexao.capabilities.map((u) => (
            <li key={u}>
              <span className="yes" aria-hidden="true">
                ✓
              </span>
              {PERMISSAO[u] || u}
            </li>
          ))}
        </ul>
      )}

      {conexao.state === "SEM_CREDENCIAL" && (
        <Alerta
          tone="warn"
          titulo="Falta a credencial"
          detalhe="O serviço está escolhido, e o Regente ainda não tem autorização para falar com ele."
          acao={
            <Link href="#/config/credenciais" variante="btn btn-sm">
              Registrar credencial
            </Link>
          }
        />
      )}

      <div className="row">
        {pode && (
          <button className="btn btn-sm btn-primary" onClick={aoConfigurar}>
            {oferta ? "Configurar" : "Escolher serviço"}
          </button>
        )}
        {conexao.needs_credential && conexao.live_credentials > 0 && (
          <button className="btn btn-sm" onClick={testar} disabled={indo}>
            Testar conexão
          </button>
        )}
      </div>

      {prova && <Prova prova={prova} />}
    </article>
  );
}

/**
 * O resultado de provar a credencial contra o provedor.
 *
 * Quatro fatos separados, e nenhum deles o segredo. Reduzir tudo a "erro de
 * conexão" apagaria a diferença entre "a credencial não serve" e "não deu para
 * perguntar" — e as duas mandam a pessoa fazer coisas diferentes.
 */
function Prova({ prova }) {
  if (prova.estado === "indo") {
    return (
      <div className="outcome working" role="status">
        Provando a credencial contra o provedor…
      </div>
    );
  }
  if (prova.estado === "erro") {
    return (
      <div className="outcome bad" role="status">
        {prova.texto}
      </div>
    );
  }
  const d = prova.d;
  return (
    <div className="prova">
      <div className={`outcome ${d.usable ? "good" : "bad"}`} role="status">
        {d.usable ? "A conexão funciona." : "A conexão não funciona."}{" "}
        {d.detail}
      </div>
      <div className="dados compacto">
        <Dado
          rotuloTexto="O Regente autoriza"
          valor={d.authorized ? "Sim" : "Não"}
          porque="A credencial existe e tem a permissão"
        />
        <Dado
          rotuloTexto="O provedor respondeu"
          valor={ALCANCE[d.reach] || d.reach}
          porque="O que aconteceu na ida até lá"
        />
        <Dado
          rotuloTexto="A ferramenta suporta"
          valor={d.capability_supported ? "Sim" : "Não"}
          porque="Se este serviço sabe fazer esta operação"
        />
      </div>
      <Tecnico
        pares={[
          ["Recusa", d.refusal],
          ["Alcance", d.reach],
        ]}
      />
    </div>
  );
}

/**
 * O formulário de um papel: escolher o serviço, e preencher o que ele pede.
 *
 * A escrita vai para `/settings/providers` com o objeto INTEIRO, porque é assim
 * que a sobreposição funciona — ela substitui a chave toda. Mandar só o papel
 * editado apagaria os outros.
 */
function FormConexao({ papel, configurado, aoFechar }) {
  const { api, recarregar } = useRegente();
  const [feedback, diga] = useFeedback();
  const atual = configurado[papel.role] || {};
  const [escolhido, setEscolhido] = useState(atual.name || "");
  const [valores, setValores] = useState(() => ({ ...atual }));
  const [confirmar, setConfirmar] = useState(false);
  const [indo, setIndo] = useState(false);

  const oferta = papel.options.find((o) => o.name === escolhido);

  useEffect(() => {
    if (!oferta) return;
    // Trocar de serviço não carrega as opções do anterior: `site` do Jira não
    // quer dizer nada para o adapter de arquivos, e mandá-lo junto faria o
    // motor recusar uma configuração que a pessoa não escreveu.
    if (escolhido !== atual.name) {
      const padroes = {};
      for (const c of oferta.fields) {
        if (c.default !== null && c.default !== undefined) padroes[c.key] = c.default;
      }
      setValores({ ...padroes, name: escolhido });
    } else {
      setValores({ ...atual });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [escolhido]);

  const em = (k, v) => setValores((x) => ({ ...x, [k]: v }));

  const faltando = (oferta?.fields || [])
    .filter((c) => c.required && !String(valores[c.key] ?? "").trim())
    .map((c) => c.label);

  async function salvar() {
    setIndo(true);
    diga.indo("Salvando…");
    const limpo = { name: escolhido };
    for (const c of oferta.fields) {
      const v = valores[c.key];
      if (v === "" || v === undefined || v === null) continue;
      limpo[c.key] =
        c.kind === "numero"
          ? Number(v)
          : c.kind === "lista"
            ? String(v).split(/\s+/).filter(Boolean)
            : c.kind === "booleano"
              ? !!v
              : v;
    }
    const r = await post(api("/settings/providers"), {
      value: { ...configurado, [papel.role]: limpo },
    });
    setIndo(false);
    if (!r.ok) {
      diga.erro(digaOErro(r));
      return;
    }
    diga.ok(`${papel.label} configurado.`);
    recarregar();
    aoFechar();
  }

  return (
    <Painel className="form-conexao">
      <div className="row">
        <h2>Configurar: {papel.label}</h2>
        <span className="spacer" />
        <button className="btn btn-ghost btn-sm" onClick={aoFechar}>
          Fechar
        </button>
      </div>
      <p className="muted">{papel.description}</p>

      <fieldset className="grupo">
        <legend>Qual serviço</legend>
        <div className="opcoes">
          {papel.options.map((o) => (
            <label
              key={o.name}
              className={`opcao${escolhido === o.name ? " escolhida" : ""}`}
            >
              <input
                type="radio"
                name={`servico-${papel.role}`}
                checked={escolhido === o.name}
                onChange={() => setEscolhido(o.name)}
              />
              <span>
                <span className="t">{o.label}</span>
                <span className="d">{o.description}</span>
                <span className="d dim">
                  {o.needs_credential
                    ? "Precisa de uma credencial autorizada."
                    : "Não precisa de credencial."}
                </span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      {oferta && (
        <>
          <fieldset className="grupo">
            <legend>O que o {oferta.label} precisa saber</legend>
            <div className="form-row">
              {oferta.fields
                .filter((c) => !c.advanced)
                .map((c) => (
                  <Campo key={c.key} campo={c} valor={valores[c.key]} aoMudar={em} />
                ))}
            </div>
          </fieldset>

          {oferta.fields.some((c) => c.advanced) && (
            <details className="tech">
              <summary>Opções avançadas</summary>
              <div className="tech-body avancado">
                <div className="form-row">
                  {oferta.fields
                    .filter((c) => c.advanced)
                    .map((c) => (
                      <Campo
                        key={c.key}
                        campo={c}
                        valor={valores[c.key]}
                        aoMudar={em}
                      />
                    ))}
                </div>
              </div>
            </details>
          )}

          {oferta.needs_credential && (
            <Alerta
              tone="info"
              icone="i"
              titulo="Este serviço também precisa de uma credencial"
              detalhe={`Salvar aqui diz ao Regente COM QUEM falar. Para ele poder falar, registre uma credencial com a permissão “${
                PERMISSAO[oferta.credential_use] || oferta.credential_use
              }”.`}
              acao={
                <Link href="#/config/credenciais" variante="btn btn-sm">
                  Ir para credenciais
                </Link>
              }
            />
          )}
        </>
      )}

      {feedback}

      <div className="form-actions">
        <button className="btn" onClick={aoFechar}>
          Cancelar
        </button>
        <button
          className="btn btn-primary"
          disabled={!oferta || faltando.length > 0 || indo}
          onClick={() =>
            atual.name && atual.name !== escolhido
              ? setConfirmar(true)
              : salvar()
          }
          title={faltando.length ? `Falta preencher: ${faltando.join(", ")}` : ""}
        >
          Salvar
        </button>
        {faltando.length > 0 && (
          <span className="hint">Falta preencher: {faltando.join(", ")}.</span>
        )}
      </div>

      <Confirmar
        aberto={confirmar}
        titulo={`Trocar ${papel.label} de ${atual.name} para ${escolhido}?`}
        impacto="As opções do serviço anterior são descartadas, e as tasks já descobertas continuam como estão. As credenciais registradas não são apagadas."
        rotuloOk="Trocar serviço"
        aoCancelar={() => setConfirmar(false)}
        aoConfirmar={() => {
          setConfirmar(false);
          salvar();
        }}
      />
    </Painel>
  );
}

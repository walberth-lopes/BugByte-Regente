// Integrações: conectar um serviço, e escolher o que ele traz.
//
// A tela inteira existe para uma frase: *chego, conecto o GitHub, escolho meus
// repositórios, e o Regente trabalha.* Tudo o que estiver entre a pessoa e isso
// é defeito.
//
// A primeira versão desta página era uma ferramenta de administrador: escolha
// um adapter, digite a organização, vá registrar uma credencial em outra aba,
// volte, aperte "buscar". Cinco passos em três lugares para dizer "use o meu
// GitHub" — e cada um deles era uma decisão que o Regente podia ter tomado
// sozinho.
//
// Agora é um botão. Ele abre o navegador quando falta autorização, pergunta de
// qual conta, e traz a lista. BUSCAR DEIXOU DE SER UM BOTÃO: conectar já mostra
// o que existe, porque quem acabou de conectar quer ver, e não procurar.
//
// O que NÃO mudou é o que está por baixo: a configuração vai pelo mesmo serviço
// que o terminal usa, a credencial é registrada pelo caminho governado, e a
// escolha continua sendo humana e gravada com nome e data.

import { useEffect, useRef, useState } from "react";
import { del, get, post } from "../api.js";
import { useRegente, useLeitura } from "../estado.jsx";
import {
  Alerta,
  Badge,
  Botao,
  Leitura,
  Painel,
  Secao,
  Tabela,
  Tecnico,
  Vazio,
} from "../ui.jsx";
import { useFeedback } from "../components/Formulario.jsx";
import {
  AVISO_DA_CONEXAO,
  FALHA_DA_BUSCA,
  PAPEL_DO_RECURSO,
  SITUACAO,
  digaOErro,
  tipoPlural,
} from "../present.js";

/** O ícone de cada serviço. Reconhecer o cartão num relance. */
const ICONE = { github: "ramo", jira: "jira", clickup: "tasks" };

export default function Integracoes() {
  const { api } = useRegente();
  const leitura = useLeitura([
    api("/connectors"),
    api("/resources"),
    api("/resources/providers"),
  ]);

  return (
    <Leitura estado={leitura} oQue="as integrações">
      {(conectores, escolhidos, arvores) => (
        <Conteudo
          conectores={conectores.connectors}
          escolhidos={escolhidos.resources}
          editavel={escolhidos.editable}
          arvores={arvores.providers}
        />
      )}
    </Leitura>
  );
}

function Conteudo({ conectores, escolhidos, editavel, arvores }) {
  const { podeAqui } = useRegente();
  const pode = editavel && podeAqui("workspace.resource.select");
  const arvoreDe = (p) => arvores.find((a) => a.provider === p)?.tree || [];

  return (
    <>
      <p className="muted">
        Conecte um serviço e escolha o que este workspace usa. Só o que você
        escolher passa a ser alcançável pelo Regente — o resto continua invisível
        para ele, mesmo estando na mesma conta.
      </p>

      {!conectores.length ? (
        <Secao titulo="Serviços">
          <Vazio
            icone="conexao"
            titulo="Nenhum serviço disponível para conectar"
            texto="Esta instalação não trouxe conectores."
          />
        </Secao>
      ) : (
        conectores.map((c) => (
          <Servico
            key={c.connector}
            servico={c}
            arvore={arvoreDe(c.connector)}
            escolhidos={escolhidos.filter((r) => r.provider === c.connector)}
            pode={pode}
          />
        ))
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Um serviço: conectar, e o que ele traz
// ---------------------------------------------------------------------------

function Servico({ servico, arvore, escolhidos, pode }) {
  const { api, recarregar } = useRegente();
  const [aviso, diga] = useFeedback();
  const [contas, setContas] = useState(null);
  const [indo, setIndo] = useState("");
  const [passo, setPasso] = useState(servico.step);
  const [conflito, setConflito] = useState(null);
  const [achado, setAchado] = useState(null);

  // O nível onde estão as coisas que se escolhem: a folha da árvore.
  const folha = arvore[arvore.length - 1] || "";
  const conectado = servico.connected;

  // Ao abrir já conectado, a lista aparece sozinha. Quem conectou ontem não
  // deveria precisar apertar nada hoje para ver o que tem.
  const jaBuscou = useRef(false);
  useEffect(() => {
    if (!conectado || !folha || jaBuscou.current) return;
    jaBuscou.current = true;
    buscar();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conectado, folha]);

  async function buscar() {
    setIndo("buscando");
    const r = await post(`${api("/resources")}/discover`, {
      provider: servico.connector,
      kind: folha,
    });
    setIndo("");
    if (!r.ok) {
      diga.erro(digaOErro(r));
      return;
    }
    setAchado(r.payload);
  }

  async function autorizar() {
    setIndo("autorizando");
    const r = await post(
      `${api("/connectors")}/${servico.connector}/autorizar`,
      {},
    );
    setIndo("");
    if (!r.ok) {
      diga.erro(digaOErro(r));
      return;
    }
    setPasso(r.payload);
  }

  async function verContas() {
    setIndo("contas");
    try {
      const r = await get(`${api("/connectors")}/${servico.connector}/accounts`);
      setContas(r.accounts || []);
    } catch (e) {
      diga.erro("Não deu para ler as contas deste serviço.");
    }
    setIndo("");
  }

  async function conectar(conta, substituir = false) {
    setIndo(conta);
    const r = await post(
      `${api("/connectors")}/${servico.connector}/conectar`,
      { account: conta, ...(substituir ? { replace: true } : {}) },
    );
    setIndo("");
    if (!r.ok) {
      // Uma credencial antiga no caminho não é um beco: a tela oferece trocar.
      if (r.payload?.credential_id) {
        setConflito({ conta, detalhe: r.payload.detail });
        return;
      }
      diga.erro(digaOErro(r));
      return;
    }
    setConflito(null);
    setContas(null);
    // A frase é DAQUI. O motor manda um código; quem escreve português é a
    // tela, como em todo o resto do vocabulário.
    if (r.payload.warning) {
      diga.erro(
        AVISO_DA_CONEXAO[r.payload.warning] ||
          "Conectado, mas algo ficou faltando.",
      );
    } else {
      diga.ok(`${servico.title} conectado. Escolha o que este workspace usa.`);
    }
    recarregar();
  }

  return (
    <Secao
      titulo={servico.title}
      hint={conectado && servico.current ? servico.current : ""}
      acao={
        conectado ? (
          <Botao variante="btn-sm" icone="editar" onClick={verContas}>
            Trocar conta
          </Botao>
        ) : null
      }
    >
      {aviso}

      {!conectado && (
        <Conectar
          servico={servico}
          passo={passo}
          indo={indo}
          contas={contas}
          onAutorizar={autorizar}
          onVerContas={verContas}
          onConectar={conectar}
          onRever={recarregar}
        />
      )}

      {conectado && contas && (
        <Contas
          contas={contas}
          atual={servico.current}
          indo={indo}
          onEscolher={(c) => conectar(c)}
          onCancelar={() => setContas(null)}
        />
      )}

      {conflito && (
        <Alerta
          tone="warn"
          titulo="Já existe uma credencial para este papel"
          detalhe={conflito.detalhe}
          acao={
            <Botao
              variante="btn-primary btn-sm"
              onClick={() => conectar(conflito.conta, true)}
            >
              Substituir e conectar
            </Botao>
          }
        />
      )}

      {conectado && (
        <Recursos
          servico={servico}
          folha={folha}
          achado={achado}
          buscando={indo === "buscando"}
          escolhidos={escolhidos}
          pode={pode}
          onRebuscar={buscar}
        />
      )}
    </Secao>
  );
}

// ---------------------------------------------------------------------------
// O botão
// ---------------------------------------------------------------------------

function Conectar({
  servico,
  passo,
  indo,
  contas,
  onAutorizar,
  onVerContas,
  onConectar,
  onRever,
}) {
  if (contas) {
    return (
      <Contas
        contas={contas}
        indo={indo}
        onEscolher={onConectar}
        onCancelar={onRever}
      />
    );
  }

  if (passo.code === "instalar") {
    return (
      <>
        <Vazio
          icone={ICONE[servico.connector] || "conexao"}
          titulo={passo.title}
          texto={passo.detail}
        />
        {passo.command && <Comando texto={passo.command} />}
      </>
    );
  }

  if (passo.code === "autorizar") {
    return (
      <>
        <p className="muted">{servico.description}</p>
        <div className="toolbar">
          <Botao
            variante="btn-primary"
            icone={ICONE[servico.connector] || "conexao"}
            disabled={indo === "autorizando"}
            onClick={onAutorizar}
          >
            {indo === "autorizando"
              ? "Abrindo o navegador…"
              : `Conectar ${servico.title}`}
          </Botao>
          <Botao variante="btn-sm" icone="atualizar" onClick={onRever}>
            Já autorizei
          </Botao>
        </div>
        <p className="muted">{passo.detail}</p>
        {passo.command && <Comando texto={passo.command} />}
      </>
    );
  }

  return (
    <>
      <p className="muted">{servico.description}</p>
      <div className="toolbar">
        <Botao
          variante="btn-primary"
          icone={ICONE[servico.connector] || "conexao"}
          disabled={indo === "contas"}
          onClick={onVerContas}
        >
          {indo === "contas" ? "Carregando…" : `Conectar ${servico.title}`}
        </Botao>
      </div>
      {passo.detail && <p className="muted">{passo.detail}</p>}
    </>
  );
}

/** O mesmo, para quem prefere o terminal. Nunca obrigatório. */
function Comando({ texto }) {
  return (
    <Tecnico titulo="Prefiro rodar no terminal">
      <code>{texto}</code>
    </Tecnico>
  );
}

function Contas({ contas, atual, indo, onEscolher, onCancelar }) {
  if (!contas.length) {
    return (
      <Vazio
        icone="identidade"
        titulo="Nenhuma conta encontrada"
        texto="A autorização pode não ter concluído. Tente conectar de novo."
      />
    );
  }
  return (
    <>
      <Painel flush>
        <Tabela
          chave={(c) => c.id}
          colunas={[
            {
              rot: "Conta",
              corpo: (c) => (
                <>
                  <strong>{c.name}</strong>
                  <div className="muted">
                    {c.kind === "pessoal" ? "Sua conta" : "Organização"}
                    {c.id === atual ? " · em uso" : ""}
                  </div>
                </>
              ),
            },
            {
              rot: "Usar",
              oculto: true,
              corpo: (c) => (
                <Botao
                  variante="btn-primary btn-sm"
                  disabled={indo === c.id || c.id === atual}
                  onClick={() => onEscolher(c.id)}
                >
                  {indo === c.id ? "Conectando…" : "Usar esta"}
                </Botao>
              ),
            },
          ]}
          linhas={contas}
        />
      </Painel>
      {onCancelar && (
        <div className="toolbar">
          <Botao variante="btn-sm" onClick={onCancelar}>
            Cancelar
          </Botao>
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// O que o serviço traz, e o que este workspace usa
// ---------------------------------------------------------------------------

function Recursos({
  servico,
  folha,
  achado,
  buscando,
  escolhidos,
  pode,
  onRebuscar,
}) {
  const { api, recarregar } = useRegente();
  const [aviso, diga] = useFeedback();
  const [marcados, setMarcados] = useState(() => new Set());
  const [busca, setBusca] = useState("");

  async function adicionar() {
    const ids = [...marcados];
    if (!ids.length) return;
    const r = await post(`${api("/resources")}/select`, {
      provider: servico.connector,
      kind: folha,
      ids,
    });
    if (!r.ok) {
      diga.erro(digaOErro(r));
      return;
    }
    diga.ok(
      `${ids.length} ${ids.length === 1 ? "item passou" : "itens passaram"} a ` +
        "fazer parte deste workspace.",
    );
    setMarcados(new Set());
    recarregar();
    onRebuscar();
  }

  async function remover(r) {
    const resposta = await del(
      `${api("/resources")}/${encodeURIComponent(r.ref)}`,
    );
    if (!resposta.ok) {
      diga.erro(digaOErro(resposta));
      return;
    }
    diga.ok(`O Regente deixa de alcançar ${r.name} a partir de agora.`);
    recarregar();
    onRebuscar();
  }

  const filtro = busca.trim().toLowerCase();
  const itens = (achado?.resources || []).filter((i) =>
    filtro ? `${i.label} ${i.id}`.toLowerCase().includes(filtro) : true,
  );
  const marcaveis = itens.filter(
    (i) => i.selectable && i.status !== "SELECIONADO",
  );

  return (
    <>
      {aviso}

      {escolhidos.length > 0 && (
        <Painel flush>
          <Tabela
            chave={(r) => r.ref}
            colunas={[
              { rot: "Em uso", corpo: (r) => <strong>{r.name}</strong> },
              {
                rot: "Para quê",
                corpo: (r) => PAPEL_DO_RECURSO[r.role] || r.role,
              },
              ...(pode
                ? [
                    {
                      rot: "Tirar do workspace",
                      oculto: true,
                      corpo: (r) => (
                        <Botao
                          variante="btn-danger btn-sm"
                          icone="remover"
                          onClick={() => remover(r)}
                        >
                          Remover
                        </Botao>
                      ),
                    },
                  ]
                : []),
            ]}
            linhas={escolhidos}
          />
        </Painel>
      )}

      {buscando && <p className="muted">Perguntando ao {servico.title}…</p>}

      {achado && !achado.ok && <Falhou achado={achado} />}

      {achado?.ok && (
        <>
          <div className="toolbar">
            <strong>{tipoPlural(folha)} disponíveis</strong>
            {achado.resources.length > 6 && (
              <input
                className="input grow"
                type="search"
                placeholder="Filtrar por nome"
                aria-label={`Filtrar ${tipoPlural(folha).toLowerCase()}`}
                value={busca}
                onChange={(e) => setBusca(e.target.value)}
              />
            )}
            <span className="spacer" />
            <Botao variante="btn-sm" icone="atualizar" onClick={onRebuscar}>
              Atualizar
            </Botao>
          </div>

          {!itens.length ? (
            <Vazio
              icone="buscar"
              titulo={
                filtro
                  ? "Nada com esse nome"
                  : `Esta conta não tem ${tipoPlural(folha).toLowerCase()}`
              }
              texto={filtro ? "Ajuste o filtro." : "Nada a escolher aqui."}
            />
          ) : (
            <>
              <Painel flush>
                <Lista
                  itens={itens}
                  marcados={marcados}
                  pode={pode}
                  aoMarcar={(id, ligado) => {
                    const proximo = new Set(marcados);
                    if (ligado) proximo.add(id);
                    else proximo.delete(id);
                    setMarcados(proximo);
                  }}
                />
              </Painel>

              {pode && marcaveis.length > 0 && (
                <div className="toolbar">
                  <Botao
                    variante="btn-sm"
                    onClick={() =>
                      setMarcados(new Set(marcaveis.map((i) => i.id)))
                    }
                  >
                    Marcar {marcaveis.length}
                  </Botao>
                  {marcados.size > 0 && (
                    <Botao
                      variante="btn-sm"
                      onClick={() => setMarcados(new Set())}
                    >
                      Limpar
                    </Botao>
                  )}
                  <span className="spacer" />
                  <Botao
                    variante="btn-primary"
                    icone="mais"
                    disabled={!marcados.size}
                    onClick={adicionar}
                  >
                    Adicionar ao workspace
                    {marcados.size ? ` (${marcados.size})` : ""}
                  </Botao>
                </div>
              )}
            </>
          )}
        </>
      )}
    </>
  );
}

/**
 * A busca não respondeu.
 *
 * Uma lista vazia seria a mentira mais cara desta tela: quem a lesse concluiria
 * que a conta esvaziou, e removeria a seleção. Então o que aparece é a falha, o
 * que fazer a respeito, e o lembrete de que nada mudou.
 */
function Falhou({ achado }) {
  const dito = FALHA_DA_BUSCA[achado.failure] || {
    titulo: "A busca não respondeu",
    saida: "Tente de novo em alguns instantes.",
  };
  return (
    <>
      <Alerta
        tone={achado.failure === "PROVEDOR_INDISPONIVEL" ? "warn" : "danger"}
        titulo={dito.titulo}
        detalhe={`${dito.saida} O que este workspace já usa continua valendo — nada foi removido.`}
      />
      <Tecnico>
        <code>{`${achado.failure} · ${achado.detail}`}</code>
      </Tecnico>
    </>
  );
}

function Lista({ itens, marcados, pode, aoMarcar }) {
  const situacaoDe = (i) =>
    SITUACAO[i.status] || { texto: i.status, tone: "muted" };

  return (
    <Tabela
      chave={(i) => i.ref}
      colunas={[
        ...(pode
          ? [
              {
                rot: "Escolher",
                oculto: true,
                classe: "col-marca",
                corpo: (i) =>
                  i.selectable && i.status !== "SELECIONADO" ? (
                    <label className="check-inline">
                      <input
                        type="checkbox"
                        checked={marcados.has(i.id)}
                        onChange={(e) => aoMarcar(i.id, e.target.checked)}
                        aria-label={`Escolher ${i.label}`}
                      />
                    </label>
                  ) : null,
              },
            ]
          : []),
        {
          rot: "Nome",
          corpo: (i) => (
            <>
              <strong>{i.label}</strong>
              {!i.selectable && i.note && <div className="muted">{i.note}</div>}
            </>
          ),
        },
        {
          rot: "Situação",
          corpo: (i) =>
            // "Disponível" quer dizer "existe e você não escolheu". Num item
            // que não se escolhe isso não significa nada, e a etiqueta
            // convidaria a procurar a caixa que não está lá.
            i.selectable ? (
              <Badge tone={situacaoDe(i).tone} texto={situacaoDe(i).texto} />
            ) : (
              <span className="dim">—</span>
            ),
        },
      ]}
      linhas={itens}
    />
  );
}

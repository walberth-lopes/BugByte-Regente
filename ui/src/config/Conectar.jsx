// Conectar um serviço, dentro do cartão dele.
//
// Isto já foi uma aba separada chamada "Integrações", e a separação era um erro
// meu: para quem usa, "Conexões" e "Integrações" são a mesma palavra. A pessoa
// clicava na aba que PROMETIA conectar, encontrava um estado vazio, e era
// mandada para a outra — onde a palavra era a mesma.
//
// Agora é um lugar só. Escolheu GitHub no cartão de Repositório? O botão de
// conectar está ali. Conectou? A lista do que ele traz abre numa gaveta, que é
// onde há espaço — um cartão de 330px não é lugar para 28 repositórios.
//
// O que está por baixo não mudou: conectar grava pelo mesmo serviço governado,
// e a escolha continua sendo humana, gravada com nome e data.

import { useEffect, useRef, useState } from "react";
import { del, get, post } from "../api.js";
import { useRegente } from "../estado.jsx";
import { Alerta, Badge, Botao, Painel, Tabela, Tecnico, Vazio } from "../ui.jsx";
import { useFeedback } from "../components/Formulario.jsx";
import Gaveta from "../components/Gaveta.jsx";
import {
  AVISO_DA_CONEXAO,
  FALHA_DA_BUSCA,
  SITUACAO,
  digaOErro,
  tipoPlural,
} from "../present.js";

/**
 * O bloco que aparece DENTRO do cartão do papel.
 *
 * Três estados, e cada um com um movimento só:
 *
 *   falta a ferramenta  → o comando para instalá-la
 *   falta autorizar     → o botão que abre o navegador
 *   pronto              → conectar, ou escolher o que ele traz
 */
export default function Conectar({ servico, arvore, escolhidos, pode }) {
  const { recarregar } = useRegente();
  const [aberta, setAberta] = useState(false);
  const [aviso, diga] = useFeedback();
  const [passo, setPasso] = useState(servico.step);
  const [indo, setIndo] = useState(false);
  const { api } = useRegente();

  async function autorizar() {
    setIndo(true);
    const r = await post(`${api("/connectors")}/${servico.connector}/autorizar`, {});
    setIndo(false);
    if (!r.ok) {
      diga.erro(digaOErro(r));
      return;
    }
    setPasso(r.payload);
  }

  if (passo.code === "instalar") {
    return (
      <>
        <Alerta tone="warn" titulo={passo.title} detalhe={passo.detail} />
        {passo.command && (
          <Tecnico titulo="O comando">
            <code>{passo.command}</code>
          </Tecnico>
        )}
      </>
    );
  }

  if (passo.code === "autorizar") {
    return (
      <>
        {aviso}
        <div className="row">
          {pode && (
            <Botao
              variante="btn-sm btn-primary"
              icone="conexao"
              disabled={indo}
              onClick={autorizar}
            >
              {indo ? "Abrindo o navegador…" : `Conectar ${servico.title}`}
            </Botao>
          )}
          <Botao variante="btn-sm" icone="atualizar" onClick={recarregar}>
            Já autorizei
          </Botao>
        </div>
        {passo.detail && <p className="hint">{passo.detail}</p>}
      </>
    );
  }

  return (
    <>
      {aviso}

      {servico.connected && servico.authorized && (
        <p className="hint">
          {escolhidos.length
            ? `${escolhidos.length} ${
                escolhidos.length === 1 ? "item em uso" : "itens em uso"
              } neste workspace.`
            : "Nada escolhido ainda — o Regente não alcança nada deste serviço."}
        </p>
      )}

      {pode && (
        <div className="row">
          <Botao
            variante="btn-sm btn-primary"
            icone={servico.authorized ? "buscar" : "conexao"}
            onClick={() => setAberta(true)}
          >
            {servico.authorized
              ? `Escolher ${tipoPlural(arvore[arvore.length - 1] || "").toLowerCase()}`
              : `Conectar ${servico.title}`}
          </Botao>
        </div>
      )}

      {aberta && (
        <GavetaDoServico
          servico={servico}
          arvore={arvore}
          escolhidos={escolhidos}
          pode={pode}
          aoFechar={() => setAberta(false)}
        />
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// A gaveta: escolher a conta, e escolher o que ela traz
// ---------------------------------------------------------------------------

function GavetaDoServico({ servico, arvore, escolhidos, pode, aoFechar }) {
  const { api, recarregar } = useRegente();
  const [aviso, diga] = useFeedback();
  const [contas, setContas] = useState(null);
  const [achado, setAchado] = useState(null);
  const [indo, setIndo] = useState("");
  const [marcados, setMarcados] = useState(() => new Set());
  const [busca, setBusca] = useState("");
  const [conflito, setConflito] = useState(null);
  // "Pronto para escolher" e ter credencial, e nao ter configuracao.
  const [conectado, setConectado] = useState(
    servico.connected && servico.authorized);
  const [conta, setConta] = useState(servico.current);

  const folha = arvore[arvore.length - 1] || "";

  // Ao abrir já conectado, a lista vem sozinha. Quem já conectou quer ver, e
  // não apertar mais um botão para pedir o que ele veio pedir.
  const partiu = useRef(false);
  useEffect(() => {
    if (partiu.current) return;
    partiu.current = true;
    if (conectado) buscar();
    else verContas();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function verContas() {
    setIndo("contas");
    try {
      const r = await get(`${api("/connectors")}/${servico.connector}/accounts`);
      setContas(r.accounts || []);
    } catch {
      diga.erro("Não deu para ler as contas deste serviço.");
    }
    setIndo("");
  }

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

  async function conectar(qual, substituir = false) {
    setIndo(qual);
    const r = await post(`${api("/connectors")}/${servico.connector}/conectar`, {
      account: qual,
      ...(substituir ? { replace: true } : {}),
    });
    setIndo("");
    if (!r.ok) {
      if (r.payload?.credential_id) {
        setConflito({ conta: qual, detalhe: r.payload.detail });
        return;
      }
      diga.erro(digaOErro(r));
      return;
    }
    setConflito(null);
    setContas(null);
    setConectado(true);
    setConta(qual);
    if (r.payload.warning) {
      diga.erro(
        AVISO_DA_CONEXAO[r.payload.warning] ||
          "Conectado, mas algo ficou faltando.",
      );
    }
    recarregar();
    buscar();
  }

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
    buscar();
  }

  async function remover(r) {
    const resposta = await del(`${api("/resources")}/${encodeURIComponent(r.ref)}`);
    if (!resposta.ok) {
      diga.erro(digaOErro(resposta));
      return;
    }
    diga.ok(`O Regente deixa de alcançar ${r.name}.`);
    recarregar();
    buscar();
  }

  const filtro = busca.trim().toLowerCase();
  const itens = (achado?.resources || []).filter((i) =>
    filtro ? `${i.label} ${i.id}`.toLowerCase().includes(filtro) : true,
  );
  const marcaveis = itens.filter((i) => i.selectable && i.status !== "SELECIONADO");

  return (
    <Gaveta
      aberta
      larga
      titulo={servico.title}
      sub={
        conectado
          ? `${conta} — escolha o que este workspace usa`
          : "Escolha a conta ou organização"
      }
      aoFechar={aoFechar}
      rodape={
        <>
          <Botao variante="btn" onClick={aoFechar}>
            Fechar
          </Botao>
          {conectado && pode && marcaveis.length > 0 && (
            <Botao
              variante="btn-primary"
              icone="mais"
              disabled={!marcados.size}
              onClick={adicionar}
            >
              Adicionar ao workspace
              {marcados.size ? ` (${marcados.size})` : ""}
            </Botao>
          )}
        </>
      }
    >
      {aviso}

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

      {contas && (
        <Contas
          contas={contas}
          // "Em uso" so vale quando ESTA conectado. Enquanto falta credencial,
          // o nome escrito na configuracao nao e uma conexao -- e desabilitar
          // a conta por causa dele deixava a pessoa sem nada para clicar.
          atual={conectado ? conta : ""}
          indo={indo}
          onEscolher={(c) => conectar(c)}
        />
      )}

      {conectado && !contas && (
        <>
          <div className="toolbar">
            <Botao variante="btn-sm" icone="editar" onClick={verContas}>
              Trocar conta
            </Botao>
            <span className="spacer" />
            <Botao variante="btn-sm" icone="atualizar" onClick={buscar}>
              Atualizar
            </Botao>
          </div>

          {escolhidos.length > 0 && (
            <>
              <h3 className="gaveta-secao">Em uso</h3>
              <Painel flush>
                <Tabela
                  chave={(r) => r.ref}
                  colunas={[
                    { rot: "Recurso", corpo: (r) => <strong>{r.name}</strong> },
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
            </>
          )}

          {indo === "buscando" && (
            <p className="muted">Perguntando ao {servico.title}…</p>
          )}

          {achado && !achado.ok && <Falhou achado={achado} />}

          {achado?.ok && (
            <>
              <h3 className="gaveta-secao">{tipoPlural(folha)} disponíveis</h3>
              {achado.resources.length > 6 && (
                <input
                  className="input"
                  type="search"
                  placeholder="Filtrar por nome"
                  aria-label={`Filtrar ${tipoPlural(folha).toLowerCase()}`}
                  value={busca}
                  onChange={(e) => setBusca(e.target.value)}
                />
              )}
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
                    </div>
                  )}
                </>
              )}
            </>
          )}
        </>
      )}
    </Gaveta>
  );
}

function Contas({ contas, atual, indo, onEscolher }) {
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
            // que não se escolhe isso não significa nada.
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

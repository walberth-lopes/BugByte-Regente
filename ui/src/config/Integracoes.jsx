// Integrações: o que existe lá fora, e o que este workspace usa.
//
// A tela inteira gira em torno de uma fronteira. Uma credencial que alcança 47
// repositórios NÃO autoriza o Regente a trabalhar em 47 — ela autoriza
// PERGUNTAR. Entre "o serviço tem" e "o Regente pode tocar" existe uma escolha
// sua, e é ela que esta página coleta.
//
// Por isso buscar é um botão, e não algo que acontece ao abrir a página:
// perguntar custa uma ida ao serviço e fica registrado na atividade. E por isso
// a lista do que está em uso aparece em cima, antes de qualquer busca — o que o
// Regente usa hoje não depende de o serviço estar no ar agora.
//
// A tela não conhece fornecedor. A árvore de cada um — conta › repositório,
// projeto › board — vem do servidor; aqui só existem níveis com nomes.

import { useState } from "react";
import { del, post } from "../api.js";
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
  FALHA_DA_BUSCA,
  PAPEL_DO_RECURSO,
  SITUACAO,
  digaOErro,
  tipoPlural,
} from "../present.js";

export default function Integracoes() {
  const { api } = useRegente();
  const leitura = useLeitura([api("/resources"), api("/resources/providers")]);

  return (
    <Leitura estado={leitura} oQue="as integrações">
      {(escolhidos, provedores) => (
        <Conteudo
          escolhidos={escolhidos.resources}
          editavel={escolhidos.editable}
          provedores={provedores.providers}
        />
      )}
    </Leitura>
  );
}

function Conteudo({ escolhidos, editavel, provedores }) {
  const { podeAqui } = useRegente();
  const pode = editavel && podeAqui("workspace.resource.select");

  return (
    <>
      <p className="muted">
        Conectar um serviço dá ao Regente permissão para <strong>perguntar</strong>{" "}
        o que existe nele. Só o que você escolher aqui passa a ser alcançável — o
        resto continua invisível para o Regente, mesmo estando na mesma conta.
      </p>

      <EmUso escolhidos={escolhidos} pode={pode} />

      {!provedores.length ? (
        <Secao titulo="Serviços">
          <Vazio
            icone="conexao"
            titulo="Nenhum serviço configurado sabe listar recursos"
            texto={
              "Integrações aparecem aqui depois que você conecta um serviço " +
              "capaz de dizer o que a sua conta alcança."
            }
            acao={
              <a className="btn btn-primary" href="#/config/providers">
                Ir para Conexões
              </a>
            }
          />
        </Secao>
      ) : (
        provedores.map((p) => (
          <Provedor key={p.provider} provedor={p} pode={pode} />
        ))
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// O que este workspace usa hoje
// ---------------------------------------------------------------------------

function EmUso({ escolhidos, pode }) {
  const { api, recarregar } = useRegente();
  const [aviso, diga] = useFeedback();
  const [saindo, setSaindo] = useState("");

  async function remover(r) {
    setSaindo(r.ref);
    const resposta = await del(`${api("/resources")}/${encodeURIComponent(r.ref)}`);
    setSaindo("");
    if (!resposta.ok) {
      diga.erro(digaOErro(resposta));
      return;
    }
    diga.ok(`O Regente deixa de alcançar ${r.name} a partir de agora.`);
    recarregar();
  }

  return (
    <Secao
      titulo="Em uso neste workspace"
      hint={
        escolhidos.length
          ? `${escolhidos.length} ${escolhidos.length === 1 ? "recurso" : "recursos"}`
          : ""
      }
    >
      {aviso}

      {!escolhidos.length ? (
        <Vazio
          icone="vazio"
          titulo="O Regente ainda não usa nenhum recurso"
          texto={
            "Busque abaixo o que cada serviço alcança e escolha o que este " +
            "workspace deve usar."
          }
        />
      ) : (
        <Painel flush>
          <Tabela
            chave={(r) => r.ref}
            colunas={[
              { rot: "Recurso", corpo: (r) => <strong>{r.name}</strong> },
              { rot: "Serviço", corpo: (r) => r.provider },
              {
                rot: "Para quê",
                corpo: (r) => PAPEL_DO_RECURSO[r.role] || r.role,
              },
              {
                rot: "Escolhido por",
                corpo: (r) => (
                  <span className="muted">
                    {r.selected_by || "—"}
                    {r.selected_at
                      ? ` · ${new Date(r.selected_at).toLocaleDateString("pt-BR")}`
                      : ""}
                  </span>
                ),
              },
              ...(pode
                ? [
                    {
                      // `oculto` mantém o título para leitor de tela e o tira
                      // da tela. Um `<th />` vazio some para os dois.
                      rot: "Tirar do workspace",
                      oculto: true,
                      corpo: (r) => (
                        <Botao
                          variante="btn-danger btn-sm"
                          icone="remover"
                          disabled={saindo === r.ref}
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
    </Secao>
  );
}

// ---------------------------------------------------------------------------
// Um serviço, e a árvore dele
// ---------------------------------------------------------------------------

function Provedor({ provedor, pode }) {
  const { api, recarregar } = useRegente();
  const arvore = provedor.tree || [];
  // Começa na FOLHA da árvore: é onde estão as coisas que se escolhem. Abrir no
  // nível de navegação faria a primeira busca de todo mundo devolver uma linha
  // que nem dá para selecionar.
  const [nivel, setNivel] = useState(arvore[arvore.length - 1] || "");
  const [pai, setPai] = useState(null);
  const [achado, setAchado] = useState(null);
  const [buscando, setBuscando] = useState(false);
  const [busca, setBusca] = useState("");
  const [marcados, setMarcados] = useState(() => new Set());
  const [aviso, diga] = useFeedback();

  async function buscar(tipo, novoPai) {
    setBuscando(true);
    setMarcados(new Set());
    const resposta = await post(`${api("/resources")}/discover`, {
      provider: provedor.provider,
      kind: tipo,
      parent: novoPai ? novoPai.ref : "",
    });
    setBuscando(false);
    if (!resposta.ok) {
      diga.erro(digaOErro(resposta));
      setAchado(null);
      return;
    }
    setAchado(resposta.payload);
  }

  function irPara(tipo, novoPai) {
    setNivel(tipo);
    setPai(novoPai || null);
    setAchado(null);
    setBusca("");
    setMarcados(new Set());
  }

  async function escolher() {
    const ids = [...marcados];
    if (!ids.length) return;
    const resposta = await post(`${api("/resources")}/select`, {
      provider: provedor.provider,
      kind: nivel,
      parent: pai ? pai.ref : "",
      ids,
    });
    if (!resposta.ok) {
      diga.erro(digaOErro(resposta));
      return;
    }
    diga.ok(
      `${ids.length} ${ids.length === 1 ? "recurso passou" : "recursos passaram"}` +
        " a fazer parte deste workspace.",
    );
    setMarcados(new Set());
    recarregar();
    // Relê do serviço para as situações da lista pararem de mentir: o que
    // acabou de ser escolhido precisa aparecer como "em uso" agora, e não na
    // próxima vez que alguém clicar em buscar.
    buscar(nivel, pai);
  }

  const filtro = busca.trim().toLowerCase();
  const itens = (achado?.resources || []).filter((i) =>
    filtro ? `${i.label} ${i.id}`.toLowerCase().includes(filtro) : true,
  );
  const marcaveis = itens.filter((i) => i.selectable && i.status !== "SELECIONADO");

  return (
    <Secao titulo={provedor.provider} hint={arvore.map(tipoPlural).join(" › ")}>
      {aviso}

      {arvore.length > 1 && (
        <nav className="tabs" aria-label={`Níveis de ${provedor.provider}`}>
          {arvore.map((t) => (
            <a
              key={t}
              href="#"
              aria-current={t === nivel ? "page" : undefined}
              onClick={(e) => {
                e.preventDefault();
                irPara(t, null);
              }}
            >
              {tipoPlural(t)}
            </a>
          ))}
        </nav>
      )}

      {pai && (
        <p className="muted">
          Dentro de <strong>{pai.label}</strong>.{" "}
          <a
            href="#"
            onClick={(e) => {
              e.preventDefault();
              irPara(nivel, null);
            }}
          >
            Ver todos
          </a>
        </p>
      )}

      <div className="toolbar">
        <Botao
          variante="btn-primary"
          icone="buscar"
          disabled={buscando}
          onClick={() => buscar(nivel, pai)}
        >
          {buscando
            ? "Perguntando…"
            : achado
              ? "Buscar de novo"
              : `Buscar ${tipoPlural(nivel).toLowerCase()}`}
        </Botao>
        {achado?.resources?.length > 0 && (
          <input
            className="input grow"
            type="search"
            placeholder="Filtrar por nome"
            aria-label={`Filtrar ${tipoPlural(nivel).toLowerCase()}`}
            value={busca}
            onChange={(e) => setBusca(e.target.value)}
          />
        )}
      </div>

      {!achado && !buscando && (
        <p className="muted">
          Buscar pergunta ao {provedor.provider} o que a credencial deste
          workspace alcança. Nada é escolhido por esta ação.
        </p>
      )}

      {achado && !achado.ok && <Falhou achado={achado} />}

      {achado?.ok && !itens.length && (
        <Vazio
          icone="buscar"
          titulo={
            filtro
              ? "Nada com esse nome"
              : `Este serviço não mostrou nenhum ${tipoPlural(nivel).toLowerCase()}`
          }
          texto={
            filtro
              ? "Ajuste o filtro."
              : "A credencial deste workspace pode não alcançar nada aqui."
          }
        />
      )}

      {itens.length > 0 && (
        <>
          <Painel flush>
            <Lista
              itens={itens}
              arvore={arvore}
              nivel={nivel}
              marcados={marcados}
              pode={pode}
              aoMarcar={(id, ligado) => {
                const proximo = new Set(marcados);
                if (ligado) proximo.add(id);
                else proximo.delete(id);
                setMarcados(proximo);
              }}
              aoEntrar={(item) => {
                const abaixo = arvore[arvore.indexOf(nivel) + 1];
                if (abaixo) irPara(abaixo, item);
              }}
            />
          </Painel>

          {pode && marcaveis.length > 0 && (
            <div className="toolbar">
              <Botao
                variante="btn-sm"
                onClick={() => setMarcados(new Set(marcaveis.map((i) => i.id)))}
              >
                Marcar {marcaveis.length}
              </Botao>
              {marcados.size > 0 && (
                <Botao variante="btn-sm" onClick={() => setMarcados(new Set())}>
                  Limpar
                </Botao>
              )}
              <span className="spacer" />
              <Botao
                variante="btn-primary"
                icone="mais"
                disabled={!marcados.size}
                onClick={escolher}
              >
                Adicionar ao workspace
                {marcados.size ? ` (${marcados.size})` : ""}
              </Botao>
            </div>
          )}
        </>
      )}
    </Secao>
  );
}

// ---------------------------------------------------------------------------

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

function Lista({ itens, arvore, nivel, marcados, pode, aoMarcar, aoEntrar }) {
  const temFilho = arvore.indexOf(nivel) < arvore.length - 1;
  const situacaoDe = (i) => SITUACAO[i.status] || { texto: i.status, tone: "muted" };

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
              {i.label !== i.id && <div className="muted">{i.id}</div>}
              {!i.selectable && (
                <div className="muted">
                  {i.note || "Só para navegar — escolha o que está dentro."}
                </div>
              )}
            </>
          ),
        },
        {
          rot: "Situação",
          corpo: (i) =>
            // "Disponível" quer dizer "existe e você não escolheu". Num nó que
            // não se escolhe isso não significa nada, e a etiqueta convidaria a
            // procurar a caixa que não está lá.
            i.selectable ? (
              <Badge tone={situacaoDe(i).tone} texto={situacaoDe(i).texto} />
            ) : (
              <span className="dim">—</span>
            ),
        },
        ...(temFilho
          ? [
              {
                rot: "Abrir",
                oculto: true,
                corpo: (i) => (
                  <Botao variante="btn-sm" icone="seta" onClick={() => aoEntrar(i)}>
                    Abrir
                  </Botao>
                ),
              },
            ]
          : []),
      ]}
      linhas={itens}
    />
  );
}

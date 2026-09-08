// Os primitivos do design system.
//
// Duas regras governam este arquivo.
//
// 1. ESTADO NUNCA É SÓ COR. Todo `Badge` carrega a palavra junto do ponto
//    colorido; todo `Alerta` carrega um ícone e um título escrito. Quem não
//    distingue verde de vermelho — e quem imprime a tela — continua lendo tudo.
//
// 2. AUSÊNCIA É ESCRITA. Um campo vazio numa tela lê-se como "está tudo bem".
//    Por isso `Dito` existe: ausência aqui vira "Sem pull request", "CI não
//    observado", "Nenhum motivo registrado" — nunca um espaço em branco.

import { Fragment } from "react";
import { rotulo, tom, when, PORTA } from "./present.js";
import { humanizar } from "./eventos.js";
import { useRegente } from "./estado.jsx";
import { Icon, COR } from "./Icon.jsx";

// ---------------------------------------------------------------------------
// átomos
// ---------------------------------------------------------------------------

export const Badge = ({ termo, tone, texto }) => (
  <span className="badge" data-tone={tone ?? tom(termo)}>
    {texto ?? rotulo(termo)}
  </span>
);

export const Dito = ({ valor, ausente = "Não registrado" }) =>
  valor === null || valor === undefined || valor === "" ? (
    <span className="dim">{ausente}</span>
  ) : (
    <>{valor}</>
  );

export const Botao = ({ children, variante = "", icone, ...resto }) => (
  <button className={`btn ${variante}`.trim()} {...resto}>
    {icone && <Icon nome={icone} tamanho={15} />}
    {children}
  </button>
);

export const Link = ({ children, href, variante = "btn", icone, ...resto }) => (
  <a className={variante} href={href} {...resto}>
    {icone && <Icon nome={icone} tamanho={15} />}
    {children}
  </a>
);

// ---------------------------------------------------------------------------
// estrutura
// ---------------------------------------------------------------------------

/** O cabeçalho de uma página: a categoria, o nome, e a frase que explica. */
export const Cabecalho = ({ eyebrow, titulo, sub, acoes }) => (
  <header className="page-head">
    <div>
      {eyebrow && <span className="eyebrow">{eyebrow}</span>}
      <h1>{titulo}</h1>
      {sub && <p className="sub">{sub}</p>}
    </div>
    {acoes && <div className="acoes">{acoes}</div>}
  </header>
);

/** Um painel. `cabeca` desenha a faixa de título, com sobrancelha à direita. */
export const Painel = ({
  children,
  cabeca,
  eyebrow,
  acao,
  tight,
  flush,
  className = "",
  ...resto
}) => (
  <section
    className={`panel${flush ? " recorte" : ""} ${className}`.trim()}
    {...resto}
  >
    {cabeca && (
      <div className="panel-cabeca">
        <h3>{cabeca}</h3>
        {eyebrow && <span className="eyebrow spacer">{eyebrow}</span>}
        {acao && <span className={eyebrow ? "" : "spacer"}>{acao}</span>}
      </div>
    )}
    {flush ? (
      children
    ) : (
      <div className={`panel-corpo${tight ? " tight" : ""}`}>{children}</div>
    )}
  </section>
);

export const Secao = ({ titulo, hint, acao, children, pagina, eyebrow, sub }) => (
  <section className="secao">
    {/* Toda tela abre do mesmo jeito: categoria, nome, e a frase que explica.
        Sem isso, uma pagina comeca com um titulo grande e a seguinte com um
        subtitulo no meio de um painel, e a interface deixa de parecer
        construida por um sistema so. */}
    {pagina && <Cabecalho eyebrow={eyebrow} titulo={titulo} sub={sub} acoes={acao} />}
    {!pagina && (titulo || acao) && (
      <div className="secao-cabeca">
        {titulo && <h2>{titulo}</h2>}
        {hint && <span className="hint">{hint}</span>}
        {acao && (
          <>
            <span className="spacer" />
            {acao}
          </>
        )}
      </div>
    )}
    {pagina && hint && <p className="hint">{hint}</p>}
    {children}
  </section>
);

const ICONE_DO_TOM = {
  danger: "perigo",
  warn: "perigo",
  info: "info",
  ok: "ok",
};

export const Alerta = ({ tone = "warn", icone, titulo, detalhe, acao }) => (
  <div className="alert" data-tone={tone}>
    <span className="ico">
      <Icon nome={icone || ICONE_DO_TOM[tone] || "info"} tamanho={20} />
    </span>
    <div className="t">{titulo}</div>
    <div className="d">{detalhe}</div>
    {acao && <div className="go">{acao}</div>}
  </div>
);

/**
 * Um estado vazio que ensina o próximo passo.
 *
 * Uma lista vazia sem texto lê-se como defeito. Com texto e sem ação lê-se como
 * beco sem saída. As três partes — o que não há, por quê, e o que fazer — são o
 * componente.
 */
export const Vazio = ({ icone = "vazio", titulo, texto, acao }) => (
  <div className="empty">
    <span className="bolha">
      <Icon nome={icone} tamanho={26} />
    </span>
    <h3>{titulo}</h3>
    <p>{texto}</p>
    {acao}
  </div>
);

export const Esqueleto = ({ alto = false }) => (
  <div className="skeleton" aria-label="Carregando" role="status">
    <div className="bar w-40" />
    {alto && <div className="bar alta" />}
    <div className="bar w-70" />
    <div className="bar" />
  </div>
);

/** O detalhe técnico: inteiro, e atrás de um clique. */
export const Tecnico = ({ titulo = "Ver detalhes técnicos", pares, children }) => (
  <details className="tech">
    <summary>{titulo}</summary>
    <div className="tech-body">
      {pares ? (
        <dl>
          {pares
            .filter(([, v]) => v !== undefined && v !== null && v !== "")
            .map(([k, v]) => (
              <Fragment key={k}>
                <dt>{k}</dt>
                <dd>{String(v)}</dd>
              </Fragment>
            ))}
        </dl>
      ) : (
        children
      )}
    </div>
  </details>
);

// ---------------------------------------------------------------------------
// números
// ---------------------------------------------------------------------------

/**
 * A faixa de métricas.
 *
 * Cada segmento é ícone, rótulo, número e a frase que diz o que o número quer
 * dizer. "3" sozinho não informa nada; "3 · Bloqueadas · algo de fora impede"
 * informa.
 */
export const Faixa = ({ itens }) => (
  <div className="faixa">
    {itens.map((it) => {
      const corpo = (
        <>
          <span className="bolha" style={{ "--tone": COR[it.tone] || COR.info }}>
            <Icon nome={it.icone} tamanho={24} />
          </span>
          <span className="txt">
            <span className="l">{it.rotulo}</span>
            <span className="n">{it.n}</span>
            {it.why && <span className="why">{it.why}</span>}
          </span>
        </>
      );
      return it.href ? (
        <a key={it.rotulo} href={it.href}>
          {corpo}
        </a>
      ) : (
        <div key={it.rotulo}>{corpo}</div>
      );
    })}
  </div>
);

export const Dado = ({ rotuloTexto, valor, porque }) => (
  <div className="dado">
    <span className="l">{rotuloTexto}</span>
    <span className="v">{valor}</span>
    {porque && <span className="why">{porque}</span>}
  </div>
);

// ---------------------------------------------------------------------------
// listas
// ---------------------------------------------------------------------------

/**
 * Uma tabela que vira lista em telefone.
 *
 * Cada célula carrega `data-label`: abaixo de 640px o CSS esconde o cabeçalho e
 * usa esse rótulo. Rolagem horizontal em telefone esconde colunas inteiras sem
 * avisar, e uma coluna que ninguém vê é uma informação que não existe.
 */
export const Tabela = ({ colunas, linhas, chave, vazio }) => {
  if (!linhas.length) return vazio ?? null;
  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            {colunas.map((c) => (
              <th key={c.rot} scope="col" className={c.num ? "num" : undefined}>
                {c.oculto ? <span className="sr-only">{c.rot}</span> : c.rot}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {linhas.map((l) => (
            <tr key={chave(l)}>
              {colunas.map((c) => (
                <td
                  key={c.rot}
                  data-label={c.oculto ? "" : c.rot}
                  className={[c.num ? "num" : "", c.classe || ""]
                    .filter(Boolean)
                    .join(" ")}
                >
                  {c.corpo(l)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

export const Titulo = ({ chave, sub, href }) => (
  <div className="cell-title">
    {href ? <a href={href}>{chave}</a> : <span>{chave}</span>}
    {sub && <span className="sub">{sub}</span>}
  </div>
);

/**
 * A trilha, contada em português.
 *
 * `alto` liga rolagem interna: uma lista longa dentro de uma coluna obriga quem
 * lê a atravessar a página inteira para voltar ao contexto de onde saiu. Ela
 * não é ligada em toda lista — só onde o bloco ACOMPANHA outra coisa, e não é a
 * própria página.
 */
export const LinhaDoTempo = ({ eventos, alto = false }) => {
  const { identidade } = useRegente();
  return (
    <ul className={`timeline${alto ? " rolagem" : ""}`}>
      {eventos.map((e, i) => {
        const h = humanizar(e, identidade);
        return (
          <li key={`${e.at}-${i}`}>
            <span className="when" title={when(e.at)}>
              {new Date(e.at).toLocaleTimeString("pt-BR", {
                hour: "2-digit",
                minute: "2-digit",
              })}
            </span>
            <span className="what">
              <strong>{h.frase}</strong>
              {h.contexto && <div className="who">{h.contexto}</div>}
              <details className="tech inline">
                <summary>Ver detalhes</summary>
                <div className="tech-body">
                  <code>{h.tecnico}</code>
                  <div className="dim">{when(e.at)}</div>
                </div>
              </details>
            </span>
          </li>
        );
      })}
    </ul>
  );
};

// ---------------------------------------------------------------------------
// estados de leitura
// ---------------------------------------------------------------------------

/**
 * Carregando, falhou, ou o conteúdo — nunca os três confundidos.
 *
 * A tela não inventa estado entre duas leituras. Quando a leitura falha, ela
 * diz que falhou em vez de deixar na tela números que podem ter mudado.
 */
export const Leitura = ({
  estado,
  children,
  oQue = "o estado do Regente",
  alto,
}) => {
  if (estado.erro) {
    return (
      <Painel>
        <Alerta
          tone="danger"
          titulo={`Não foi possível ler ${oQue}`}
          detalhe={PORTA[estado.erro.status] || "O servidor não respondeu."}
        />
        <p className="muted" style={{ marginTop: "var(--s-4)" }}>
          O que estava nesta tela pode ter mudado desde a última leitura, e por
          isso não é mostrado.
        </p>
        <Tecnico>
          <code>{`HTTP ${estado.erro.status || "?"} · ${estado.erro.message}`}</code>
        </Tecnico>
      </Painel>
    );
  }
  if (estado.carregando || !estado.dados) return <Esqueleto alto={alto} />;
  return children(...estado.dados);
};

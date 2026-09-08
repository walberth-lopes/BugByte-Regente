// A casca: barra lateral, navegação, cabeçalho e a página da rota.
//
// A ordem da navegação é uma afirmação sobre o produto. "Painel" primeiro
// porque é onde se descobre o que está acontecendo; "Precisa de você" logo
// depois porque é a única coisa que só anda com uma pessoa. Configuração, saúde
// e atividade ficam num segundo grupo: são onde se vai quando há um motivo, e
// não onde se começa.

import { useEffect, useMemo, useState } from "react";
import { useRegente, useRota } from "./estado.jsx";
import { Alerta, Esqueleto, Link, Painel, Tecnico, Vazio } from "./ui.jsx";
import { PORTA } from "./present.js";
import { Icon } from "./Icon.jsx";

import Overview from "./pages/Overview.jsx";
import Atencao from "./pages/Atencao.jsx";
import Tasks from "./pages/Tasks.jsx";
import Task from "./pages/Task.jsx";
import Execucoes from "./pages/Execucoes.jsx";
import Execucao from "./pages/Execucao.jsx";
import Entregas from "./pages/Entregas.jsx";
import Config from "./pages/Config.jsx";
import Saude from "./pages/Saude.jsx";
import Eventos from "./pages/Eventos.jsx";

const PAGINAS = {
  overview: Overview,
  atencao: Atencao,
  tasks: Tasks,
  task: Task,
  execucoes: Execucoes,
  execucao: Execucao,
  entregas: Entregas,
  config: Config,
  saude: Saude,
  atividade: Eventos,
};

const NAV = [
  {
    grupo: "Operação",
    itens: [
      { id: "overview", nome: "Painel", href: "#/", icone: "painel" },
      {
        id: "atencao",
        nome: "Precisa de você",
        href: "#/atencao",
        icone: "atencao",
        conta: "atencao",
        tone: "warn",
      },
      { id: "tasks", nome: "Tasks", href: "#/tasks", icone: "tasks", conta: "tasks" },
      { id: "execucoes", nome: "Execuções", href: "#/execucoes", icone: "execucoes" },
      {
        id: "entregas",
        nome: "Entregas",
        href: "#/entregas",
        icone: "entregas",
        conta: "entregas",
      },
    ],
  },
  {
    grupo: "Sistema",
    itens: [
      { id: "config", nome: "Configuração", href: "#/config", icone: "config" },
      { id: "saude", nome: "Saúde", href: "#/saude", icone: "saude" },
      { id: "atividade", nome: "Atividade", href: "#/atividade", icone: "atividade" },
    ],
  },
];

/** Qual item da navegação acende para cada rota. */
const PAI = { task: "tasks", execucao: "execucoes" };

const TITULO = {
  overview: "Painel",
  atencao: "Precisa de você",
  tasks: "Tasks",
  task: "Task",
  execucoes: "Execuções",
  execucao: "Execução",
  entregas: "Entregas",
  config: "Configuração",
  saude: "Saúde",
  atividade: "Atividade",
};

/**
 * O tema, lembrado entre visitas.
 *
 * O padrão segue o sistema: quem trabalha no claro não deve receber uma tela
 * escura na cara, e vice-versa. A escolha explícita vence, e fica guardada
 * só neste navegador — ela não é configuração do workspace.
 */
function useTema() {
  const [tema, setTema] = useState(() => {
    try {
      const salvo = localStorage.getItem("regente:tema");
      if (salvo === "light" || salvo === "dark") return salvo;
    } catch {
      /* navegador sem armazenamento: o padrão do sistema resolve */
    }
    return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  });

  useEffect(() => {
    document.documentElement.dataset.theme = tema;
    try {
      localStorage.setItem("regente:tema", tema);
    } catch {
      /* sem armazenamento, o tema vale só nesta sessão */
    }
  }, [tema]);

  return [tema, () => setTema((t) => (t === "dark" ? "light" : "dark"))];
}

export default function App() {
  const { workspaces, workspace, trocarWorkspace, identidade, boot, frescor } =
    useRegente();
  const rota = useRota();
  const [tema, trocarTema] = useTema();
  // Os contadores da navegação vêm do painel, que já lê tudo. Um segundo pedido
  // só para eles seria uma leitura a mais por ciclo, e um número velho ao lado
  // de "Precisa de você" é pior que número nenhum.
  const [contagens, setContagens] = useState({});

  const atual = PAI[rota.pagina] || rota.pagina;
  const cliente = useMemo(
    () => workspaces.find((w) => w.id === workspace),
    [workspaces, workspace],
  );

  // Trocar de workspace zera o que era daquele: a rota de um item que só
  // existia lá não existe aqui.
  useEffect(() => {
    if (["task", "execucao"].includes(rota.pagina)) location.hash = "#/";
    setContagens({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace]);

  const Pagina = PAGINAS[rota.pagina];
  const nome = identidade?.display || identidade?.subject || "";
  const iniciais = nome
    .replace(/^[^:]+:/, "")
    .split(/[\s._-]+/)
    .map((p) => p[0])
    .join("")
    .toUpperCase()
    .slice(0, 2);

  return (
    <>
      {/* Primeiro elemento focável da página: quem navega por teclado não
          precisa atravessar a barra lateral inteira a cada troca de rota. */}
      <a className="sr-only" href="#conteudo">
        Pular para o conteúdo
      </a>

      <div className="shell">
        <aside className="sidebar">
          <div className="brand">
            <span className="mark" aria-hidden="true">
              R
            </span>
            <a href="#/">
              <div className="name">Regente</div>
              <div className="tag">Automação de engenharia</div>
            </a>
          </div>

          {/* Um seletor com uma opção só não é uma escolha; é ruído no lugar
              mais nobre da barra. Ele aparece quando há o que escolher. */}
          <div className="ws-picker" hidden={workspaces.length < 2}>
            <label htmlFor="workspace-picker">Workspace</label>
            <select
              id="workspace-picker"
              className="input"
              value={workspace ?? ""}
              onChange={(e) => trocarWorkspace(e.target.value)}
            >
              {workspaces.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.client} / {w.name}
                </option>
              ))}
            </select>
          </div>

          <nav className="nav" aria-label="Navegação principal">
            {NAV.map((g) => (
              <div className="nav-group" key={g.grupo}>
                <div className="nav-group-title">{g.grupo}</div>
                {g.itens.map((i) => {
                  const n = i.conta ? contagens[i.conta] : 0;
                  return (
                    <a
                      key={i.id}
                      href={i.href}
                      aria-current={i.id === atual ? "page" : undefined}
                      data-tone={n && i.tone ? i.tone : undefined}
                    >
                      <span className="ico">
                        <Icon nome={i.icone} tamanho={16} />
                      </span>
                      <span>{i.nome}</span>
                      {n ? <span className="count">{n}</span> : null}
                    </a>
                  );
                })}
              </div>
            ))}
          </nav>

          <div className="sidebar-foot">
            <span className="avatar" aria-hidden="true">
              {iniciais || "?"}
            </span>
            <div className="identity">
              <Identidade identidade={identidade} workspace={workspace} />
            </div>
          </div>
        </aside>

        <div className="main">
          <header className="topbar">
            <div className="crumb">
              {cliente && (
                <>
                  <span>
                    {cliente.client} / {cliente.name}
                  </span>
                  <span className="sep" aria-hidden="true">
                    ›
                  </span>
                </>
              )}
              <strong>{TITULO[rota.pagina] || rota.pagina}</strong>
            </div>
            <div className="spacer" />
            <div className="freshness" data-stale={String(!frescor.ok)}>
              <span className="dot" aria-hidden="true" />
              <span>{frescor.texto}</span>
            </div>
            <button
              className="icon-btn"
              onClick={trocarTema}
              aria-label={
                tema === "dark" ? "Usar o tema claro" : "Usar o tema escuro"
              }
              title={tema === "dark" ? "Usar o tema claro" : "Usar o tema escuro"}
            >
              <Icon nome={tema === "dark" ? "claro" : "escuro"} tamanho={17} />
            </button>
          </header>

          <main className="page" id="conteudo">
            <Corpo
              boot={boot}
              Pagina={Pagina}
              rota={rota}
              setContagens={setContagens}
            />
          </main>
        </div>
      </div>
    </>
  );
}

function Corpo({ boot, Pagina, rota, setContagens }) {
  if (boot.estado === "lendo") return <Esqueleto alto />;

  if (boot.estado === "erro") {
    return (
      <Painel>
        <Alerta
          tone="danger"
          titulo="Não foi possível falar com o Regente"
          detalhe={PORTA[boot.erro.status] || "O servidor não respondeu."}
        />
        <Tecnico>
          <code>{boot.erro.message}</code>
        </Tecnico>
      </Painel>
    );
  }

  if (boot.estado === "vazio") {
    return (
      <Painel>
        <Vazio
          icone="workspace"
          titulo="Nenhum workspace visível para você"
          texto="Esta sessão não recebeu acesso a nenhum workspace. A primeira concessão é feita no terminal, com `regente access inicial`."
        />
      </Painel>
    );
  }

  if (!Pagina) {
    return (
      <Painel>
        <Vazio
          icone="duvida"
          titulo="Página não encontrada"
          texto={`Não existe uma seção chamada “${rota.pagina}” nesta interface.`}
          acao={
            <Link href="#/" variante="btn btn-primary" icone="painel">
              Voltar ao painel
            </Link>
          }
        />
      </Painel>
    );
  }

  return <Pagina args={rota.args} setContagens={setContagens} />;
}

function Identidade({ identidade, workspace }) {
  if (!identidade) return null;
  const aqui = identidade.abilities?.[workspace] || [];
  return (
    <>
      <strong>
        {identidade.display || identidade.subject || "Não autenticado"}
      </strong>
      <div>
        {aqui.length
          ? `${aqui.length} permissão(ões) aqui`
          : "Sem permissões neste workspace"}
      </div>
      {identidade.development_only && (
        <div style={{ color: "var(--c-amber)" }}>Identidade de desenvolvimento</div>
      )}
    </>
  );
}

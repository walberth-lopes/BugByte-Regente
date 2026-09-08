// O estado que toda a tela compartilha: workspace, identidade, e o relogio de
// releitura.
//
// Duas decisoes moram aqui.
//
// A primeira: a identidade e RELIDA a cada ciclo, junto com o resto. Uma
// concessao revogada enquanto a pagina estava aberta precisa apagar os botoes
// que ela abria; guardar as capacidades no boot faria a tela oferecer, por
// horas, o que a API ja recusa.
//
// A segunda: `podeAqui` e UX, e nao seguranca. A barreira continua sendo a API
// -- se um botao aparecer por engano e a pessoa clicar, a resposta e uma
// recusa, e esta certo assim. O que se ganha e nao oferecer o que nao adianta.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { get, rota } from "./api.js";

const RELEITURA_MS = 5000;

const Ctx = createContext(null);

export function Provedor({ children }) {
  const [workspaces, setWorkspaces] = useState([]);
  const [workspace, setWorkspace] = useState(null);
  const [identidade, setIdentidade] = useState(null);
  const [boot, setBoot] = useState({ estado: "lendo", erro: null });
  // Sobe a cada ciclo. Toda leitura da tela depende dele: um numero so, e nao
  // um temporizador por componente que dispara em momentos diferentes.
  const [tique, setTique] = useState(0);
  const [frescor, setFrescor] = useState({ ok: true, texto: "Lendo…" });

  useEffect(() => {
    let vivo = true;
    (async () => {
      try {
        const d = await get("/api/workspaces");
        if (!vivo) return;
        const lista = d.workspaces || [];
        setWorkspaces(lista);
        setWorkspace(lista[0]?.id ?? null);
        setBoot({ estado: lista.length ? "pronto" : "vazio", erro: null });
      } catch (e) {
        if (vivo) setBoot({ estado: "erro", erro: e });
      }
    })();
    return () => {
      vivo = false;
    };
  }, []);

  // O relogio. Aba escondida nao le: um painel de fundo cutucando a API a cada
  // cinco segundos gasta o motor sem ninguem estar olhando.
  useEffect(() => {
    const t = setInterval(() => {
      if (!document.hidden) setTique((n) => n + 1);
    }, RELEITURA_MS);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if (!workspace) return;
    let vivo = true;
    get("/api/health")
      .then((d) => vivo && setIdentidade(d.identity))
      .catch(() => {
        /* a leitura da pagina dira o que houve */
      });
    return () => {
      vivo = false;
    };
  }, [workspace, tique]);

  const podeAqui = useCallback(
    (ability) =>
      (identidade?.abilities?.[workspace] || []).includes(ability),
    [identidade, workspace],
  );

  const api = useCallback((sufixo) => rota(workspace, sufixo), [workspace]);

  const recarregar = useCallback(() => setTique((n) => n + 1), []);

  const valor = useMemo(
    () => ({
      workspaces,
      workspace,
      trocarWorkspace: setWorkspace,
      identidade,
      podeAqui,
      api,
      tique,
      recarregar,
      boot,
      frescor,
      setFrescor,
    }),
    [
      workspaces,
      workspace,
      identidade,
      podeAqui,
      api,
      tique,
      recarregar,
      boot,
      frescor,
    ],
  );

  return <Ctx.Provider value={valor}>{children}</Ctx.Provider>;
}

export const useRegente = () => useContext(Ctx);

/** A identidade desta janela na forma `provedor:sujeito`, ou vazio. */
export function chaveDaSessao(identidade) {
  const id = identidade || {};
  return id.authenticated && id.method && id.method !== "nao autenticado"
    ? `${id.method}:${id.subject}`
    : "";
}

/**
 * Uma leitura da API, refeita a cada ciclo.
 *
 * `dados` NAO e limpo entre ciclos: piscar a tela inteira a cada cinco segundos
 * tornaria a pagina inutilizavel. `erro` limpa `dados` de proposito -- a tela
 * nao inventa estado entre duas leituras, e mostrar numeros velhos como se
 * fossem de agora e pior que dizer que a leitura falhou.
 */
export function useLeitura(caminhos, opcoes = {}) {
  const { tique, setFrescor } = useRegente();
  const { pular = false } = opcoes;
  const lista = Array.isArray(caminhos) ? caminhos : [caminhos];
  const chave = lista.join("|");

  const [estado, setEstado] = useState({
    carregando: !pular,
    dados: null,
    erro: null,
  });
  // Primeira leitura desta chave: a que merece esqueleto. As seguintes trocam o
  // conteudo sem apagar o que ja esta na tela.
  const primeira = useRef(true);

  useEffect(() => {
    primeira.current = true;
  }, [chave]);

  useEffect(() => {
    if (pular || !lista.length || lista.some((c) => !c)) {
      setEstado({ carregando: false, dados: null, erro: null });
      return;
    }
    let vivo = true;
    if (primeira.current) setEstado((e) => ({ ...e, carregando: true }));

    Promise.all(lista.map((c) => get(c)))
      .then((r) => {
        if (!vivo) return;
        primeira.current = false;
        setEstado({ carregando: false, dados: r, erro: null });
        setFrescor({
          ok: true,
          texto: `Atualizado às ${new Date().toLocaleTimeString("pt-BR", {
            hour: "2-digit",
            minute: "2-digit",
          })}`,
        });
      })
      .catch((e) => {
        if (!vivo) return;
        primeira.current = false;
        setEstado({ carregando: false, dados: null, erro: e });
        setFrescor({ ok: false, texto: "Falha ao atualizar" });
      });

    return () => {
      vivo = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chave, tique, pular]);

  return estado;
}

/** A rota, lida do hash. Sem biblioteca: sao dez linhas e nenhuma dependencia. */
export function useRota() {
  const ler = () => {
    const bruto = (location.hash || "#/").replace(/^#\/?/, "");
    const partes = bruto.split("/").filter(Boolean).map(decodeURIComponent);
    return { pagina: partes[0] || "overview", args: partes.slice(1) };
  };
  const [rotaAtual, setRota] = useState(ler);
  useEffect(() => {
    const ao = () => setRota(ler());
    window.addEventListener("hashchange", ao);
    return () => window.removeEventListener("hashchange", ao);
  }, []);
  return rotaAtual;
}

// O painel lateral onde os formulários acontecem.
//
// Antes, clicar em "Configurar" fazia o formulário BROTAR ABAIXO dos cartões.
// Isso tem três problemas, e nenhum deles é estético: a página muda de altura
// debaixo do cursor, o formulário fica fora da tela em qualquer lista que já
// role, e a pessoa perde de vista exatamente o cartão que estava configurando.
//
// A gaveta resolve os três: ela entra pela direita, tem rolagem própria, e o
// rodapé com as ações fica fixo — num formulário longo, "Salvar" não some.
//
// A confirmação continua sendo um MODAL, e não uma gaveta. São coisas
// diferentes: uma gaveta é onde se trabalha, e um modal é onde se responde uma
// pergunta de sim ou não. Por isso o modal é desenhado por cima da gaveta.

import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../Icon.jsx";

/**
 * Um formulário fora do fluxo da página.
 *
 * Acessibilidade não é enfeite aqui: uma gaveta que abre e deixa o foco na
 * página atrás é invisível para quem navega por teclado, e o leitor de tela
 * continua lendo o conteúdo que a gaveta cobriu. Por isso ela declara
 * `aria-modal`, leva o foco para dentro ao abrir, prende o Tab, e devolve o
 * foco para o botão que a abriu.
 */
export default function Gaveta({
  aberta,
  titulo,
  sub,
  rodape,
  children,
  aoFechar,
  larga = false,
}) {
  const caixa = useRef(null);
  const anterior = useRef(null);

  useEffect(() => {
    if (!aberta) return;

    anterior.current = document.activeElement;
    // A página atrás não rola junto: duas barras de rolagem fazem a pessoa
    // rolar a errada.
    const rolagem = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const primeiro = caixa.current?.querySelector(
      'input, select, textarea, button, [href], [tabindex]:not([tabindex="-1"])',
    );
    (primeiro || caixa.current)?.focus({ preventScroll: true });

    function tecla(e) {
      if (e.key === "Escape") {
        e.preventDefault();
        aoFechar();
        return;
      }
      if (e.key !== "Tab") return;
      const focaveis = [
        ...(caixa.current?.querySelectorAll(
          'input:not([disabled]), select:not([disabled]), textarea:not([disabled]), '
            + 'button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
        ) || []),
      ].filter((el) => el.offsetParent !== null);
      if (!focaveis.length) return;
      const inicio = focaveis[0];
      const fim = focaveis[focaveis.length - 1];
      if (e.shiftKey && document.activeElement === inicio) {
        e.preventDefault();
        fim.focus();
      } else if (!e.shiftKey && document.activeElement === fim) {
        e.preventDefault();
        inicio.focus();
      }
    }

    window.addEventListener("keydown", tecla, true);
    return () => {
      window.removeEventListener("keydown", tecla, true);
      document.body.style.overflow = rolagem;
      // Devolver o foco de onde ele veio: sem isso, fechar a gaveta joga quem
      // navega por teclado de volta para o topo do documento.
      anterior.current?.focus?.({ preventScroll: true });
    };
  }, [aberta, aoFechar]);

  if (!aberta) return null;

  // Direto no `body`, e nao onde o JSX foi escrito.
  //
  // Isto nao e preferencia de estilo: um ancestral com `transform`, `filter` ou
  // uma animacao que deixe matriz identidade passa a ser o bloco de contencao
  // de todo `position: fixed` abaixo dele -- e a gaveta some para fora da tela,
  // sem erro no console e sem nada que aponte a causa. Foi exatamente o que a
  // animacao de entrada da pagina fez com este painel no telefone.
  return createPortal(
    <div className="gaveta-fundo" onMouseDown={aoFechar}>
      <aside
        className={`gaveta${larga ? " larga" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={titulo}
        tabIndex={-1}
        ref={caixa}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <header className="gaveta-cabeca">
          <div>
            <h2>{titulo}</h2>
            {sub && <p className="sub">{sub}</p>}
          </div>
          <button
            className="icon-btn"
            onClick={aoFechar}
            aria-label="Fechar"
            title="Fechar"
          >
            <Icon nome="fechar" tamanho={17} />
          </button>
        </header>

        <div className="gaveta-corpo">{children}</div>

        {rodape && <footer className="gaveta-rodape">{rodape}</footer>}
      </aside>
    </div>,
    document.body,
  );
}

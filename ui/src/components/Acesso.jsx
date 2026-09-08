// O que dizer a quem esta autenticado e nao tem autoridade.
//
// O motor responde `NOT_FOUND` a quem nao tem a capacidade -- de proposito,
// para nao confirmar a existencia de um workspace alheio a quem tentou
// adivinhar. O efeito colateral e que a frase que chega aqui e "recurso nao
// encontrado neste escopo", que na tela le-se como defeito.
//
// Traduzir isso NAO afrouxa nada: a barreira continua na API. O que muda e a
// pessoa saber o que fazer, em vez de achar que o Regente quebrou.

import { useRegente, chaveDaSessao } from "../estado.jsx";
import { Painel, Vazio } from "../ui.jsx";
import { ACAO } from "../present.js";

/**
 * Os dois comandos, e por que sao dois.
 *
 * O terminal e a tela se autenticam por caminhos diferentes: o terminal e a
 * conta do sistema operacional, e a Mission Control desta versao e um token de
 * desenvolvimento nomeado pelo cliente. `regente access inicial` -- que so
 * parte do terminal, de proposito -- concede a PRIMEIRA identidade, e nao a
 * segunda. Sem dizer isto, a pessoa faz tudo certo pelo tutorial e a tela
 * continua recusando, sem nada ligando uma coisa a outra.
 */
export function ComoConceder({ chave }) {
  if (!chave) {
    return (
      <p className="muted">
        Esta janela não está autenticada, e por isso não há identidade a quem
        conceder. Reabra a Mission Control.
      </p>
    );
  }
  return (
    <div className="panel tight" style={{ marginTop: "var(--s-4)" }}>
      <p className="muted" style={{ marginBottom: "var(--s-3)" }}>
        No terminal, na pasta deste workspace — o primeiro comando dá a posse à
        sua conta do sistema, e o segundo passa a mesma autoridade para esta
        tela:
      </p>
      <div>
        <code>regente access inicial</code>
      </div>
      <div style={{ marginTop: "var(--s-2)" }}>
        <code>regente access conceder {chave} --papel owner</code>
      </div>
      <p className="hint" style={{ marginTop: "var(--s-3)" }}>
        Se o workspace já tem dono, o primeiro comando é recusado e só o segundo
        é necessário — e ele precisa ser executado por quem já tem autoridade
        para conceder.
      </p>
    </div>
  );
}

export function SemAcesso({ area, capacidade }) {
  const { identidade } = useRegente();
  const chave = chaveDaSessao(identidade);
  return (
    <Painel>
      <Vazio
        icone="credencial"
        titulo={`Você não tem acesso a ${area}`}
        texto={
          `Esta janela está autenticada${chave ? ` como ${chave}` : ""}, e essa ` +
          `identidade não recebeu a permissão “${ACAO[capacidade] || capacidade}” ` +
          `neste workspace.`
        }
      />
      <ComoConceder chave={chave} />
    </Painel>
  );
}

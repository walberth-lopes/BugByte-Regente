// Os eventos, ditos como quem conta o que aconteceu.
//
// O motor grava `kind`, `actor` e `summary`. O `actor` e uma CHAVE DE
// IDENTIDADE -- `os-account:S-1-5-21-3131206616-3848205496-107930634-1001` --
// e o `summary` frequentemente a repete. Despejar isso numa lista de atividade
// recente entrega, no lugar mais visivel da tela, a unica informacao que
// ninguem precisa ler.
//
// O que esta camada faz e COMPOR uma frase a partir do que ja foi gravado: o
// tipo do evento diz o verbo, o ator diz o sujeito. Ela nao inventa fato
// nenhum, e o texto original do motor continua inteiro em "Ver detalhes" --
// porque a chave crua e exatamente o que serve numa investigacao.

import { Frase } from "./present.js";

/**
 * Quem agiu, em portugues.
 *
 * `identidade` e a desta janela: quando o ator e a propria pessoa que esta
 * olhando, dizer "Voce" e mais claro que repetir a chave dela.
 */
export function quem(actor, identidade) {
  const bruto = String(actor || "");
  if (!bruto) return "Alguém";
  if (bruto === "engine" || bruto === "motor") return "O Regente";

  const eu = identidade?.subject
    ? `${identidade.method}:${identidade.subject}`
    : "";
  if (eu && bruto === eu) return "Você";

  const [provedor, ...resto] = bruto.split(":");
  const sujeito = resto.join(":");
  if (provedor === "os-account") return "Uma conta desta máquina";
  if (provedor === "dev-token") return `A Mission Control (${sujeito})`;
  // Um provedor que ninguem mapeou aparece com o sujeito, e nao com a chave
  // inteira: o sujeito e a parte que uma pessoa reconhece.
  return sujeito || bruto;
}

/**
 * O verbo de cada tipo de evento.
 *
 * `null` significa "nao sei dizer isto melhor que o motor" -- e nesse caso a
 * frase do motor e usada como esta, o que e sempre honesto.
 */
const VERBO = {
  acesso_inicial: () => "assumiu este workspace",
  acesso_concedido: () => "concedeu acesso a alguém",
  acesso_revogado: () => "revogou o acesso de alguém",
  credencial_concedida: () => "registrou uma credencial",
  credencial_revogada: () => "revogou uma credencial",
  configuracao: () => "alterou a configuração",
  operacao: () => "mudou o processamento",
  decisao_humana_autenticada: () => "decidiu uma escalação",
  decisao_aplicada: () => "aplicou uma decisão à task",
  baseline: () => "registrou o que existe no board",
  descoberta: () => "encontrou uma task nova",
  descoberta_concorrente: () => "encontrou a mesma task duas vezes",
  mudou_na_origem: () => "viu a task mudar no board",
  despachada: () => "entregou uma task ao agente",
  implementada: () => "terminou de executar uma task",
  falhou: () => "não conseguiu concluir uma task",
  adiada: () => "adiou uma task para o próximo ciclo",
  recuperada: () => "recuperou uma task",
  resgatada: () => "resgatou trabalho de um worker que parou",
  lease_orfao: () => "encontrou uma reserva sem dono",
  posse_perdida: () => "perdeu a posse de uma task",
  corrida_perdida: () => "perdeu a disputa por uma task",
  commit_written: () => "gravou um commit",
  delivery: () => "publicou uma entrega",
  delivery_skipped: () => "não publicou a entrega",
  ci_observada: () => "leu o resultado do CI",
  chamada_provedor: () => "falou com um serviço externo",
  terminus: () => "chegou ao fim do caminho de uma task",
  tick_inicio: () => "começou um ciclo",
  tick_fim: () => "terminou um ciclo",
  mission_started: () => "iniciou uma missão",
  mission_finished: () => "concluiu uma missão",
  error: () => "registrou um erro",
};

/**
 * Um evento em duas camadas: a frase, e o que o motor escreveu.
 *
 * `detalhe` sempre existe. Uma frase composta que perdesse a chave da
 * identidade ou o texto original tornaria a trilha inutil justamente para o uso
 * em que ela e insubstituivel -- descobrir o que houve depois que deu errado.
 */
export function humanizar(evento, identidade) {
  const verbo = VERBO[evento.kind];
  const sujeito = quem(evento.actor, identidade);
  const frase = verbo
    ? `${sujeito} ${verbo(evento)}`
    : // Sem verbo mapeado, o resumo do motor e a melhor frase disponivel.
      Frase(evento.summary) ||
      Frase(String(evento.kind || "").replace(/_/g, " "));

  return {
    frase,
    // O resumo do motor vira o subtitulo quando ele acrescenta algo -- e some
    // quando ele so repete a chave que a frase acabou de traduzir.
    contexto: verbo && evento.summary ? evento.summary : "",
    tecnico: `${evento.kind} · ${evento.actor}${
      evento.run_id ? " · run " + evento.run_id : ""
    }`,
  };
}

/** Exportado para o teste que confere que todo tipo gravado tem tradução. */
export const TIPOS_CONHECIDOS = Object.keys(VERBO);

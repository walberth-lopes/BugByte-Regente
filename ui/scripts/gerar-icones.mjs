// Extrai do conjunto Solar apenas os ícones que a Mission Control usa.
//
// Por que gerar um arquivo em vez de importar a biblioteca em tempo de
// execução: o `@iconify/react` busca os desenhos numa API pela rede, e o
// Regente roda em loopback, muitas vezes sem internet — os ícones sumiriam
// justamente na máquina de quem instalou o produto. E importar o `icons.json`
// inteiro colocaria alguns megabytes de SVG no pacote Python para usar trinta
// desenhos.
//
// O resultado é `src/icones.gerado.js`, versionado. Rode `npm run icons`
// depois de acrescentar um nome em USADOS.

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const aqui = dirname(fileURLToPath(import.meta.url));
const raiz = resolve(aqui, "..");

/** Nome no Regente -> nome no conjunto Solar. */
const USADOS = {
  painel: "widget-5-bold-duotone",
  atencao: "bell-bing-bold-duotone",
  tasks: "clipboard-list-bold-duotone",
  execucoes: "play-circle-bold-duotone",
  entregas: "rocket-2-bold-duotone",
  config: "settings-bold-duotone",
  saude: "heart-pulse-bold-duotone",
  atividade: "history-bold-duotone",

  iniciar: "play-bold-duotone",
  pausar: "pause-bold-duotone",
  parar: "stop-bold-duotone",
  retomar: "play-bold-duotone",

  pronta: "check-circle-bold-duotone",
  rodando: "bolt-circle-bold-duotone",
  bloqueada: "shield-cross-bold-duotone",
  humano: "hand-stars-bold-duotone",
  relogio: "clock-circle-bold-duotone",
  perigo: "danger-triangle-bold-duotone",
  info: "info-circle-bold-duotone",
  ok: "check-circle-bold-duotone",
  duvida: "question-circle-bold-duotone",

  conexao: "plug-circle-bold-duotone",
  credencial: "key-bold-duotone",
  acesso: "users-group-rounded-bold-duotone",
  regras: "filter-bold-duotone",
  status: "sort-horizontal-bold-duotone",
  workspace: "folder-with-files-bold-duotone",
  avancado: "code-square-bold-duotone",

  buscar: "magnifer-bold-duotone",
  mais: "add-circle-bold-duotone",
  editar: "pen-new-square-bold-duotone",
  duplicar: "copy-bold-duotone",
  remover: "trash-bin-trash-bold-duotone",
  acima: "alt-arrow-up-bold-duotone",
  abaixo: "alt-arrow-down-bold-duotone",
  seta: "alt-arrow-right-bold-duotone",
  externo: "square-top-down-bold-duotone",
  atualizar: "refresh-bold-duotone",
  fechar: "close-circle-bold-duotone",
  claro: "sun-2-bold-duotone",
  escuro: "moon-bold-duotone",
  identidade: "user-circle-bold-duotone",
  commit: "code-circle-bold-duotone",
  ramo: "branching-paths-up-bold-duotone",
  teste: "test-tube-bold-duotone",
  banco: "database-bold-duotone",
  agente: "cpu-bolt-bold-duotone",
  jira: "layers-bold-duotone",
  vazio: "inbox-line-bold-duotone",
};

const conjunto = JSON.parse(
  readFileSync(
    resolve(raiz, "node_modules/@iconify-json/solar/icons.json"),
    "utf8",
  ),
);

const largura = conjunto.width ?? 24;
const altura = conjunto.height ?? 24;
const saida = {};
const faltando = [];

for (const [nosso, deles] of Object.entries(USADOS)) {
  const icone = conjunto.icons[deles];
  if (!icone) {
    faltando.push(`${nosso} -> ${deles}`);
    continue;
  }
  saida[nosso] = icone.body;
}

if (faltando.length) {
  console.error("Ícones que não existem no conjunto:\n  " + faltando.join("\n  "));
  process.exit(1);
}

const texto = `// GERADO por scripts/gerar-icones.mjs — não edite à mão.
//
// Os desenhos vêm do conjunto Solar (Iconify), extraídos em tempo de
// desenvolvimento para o produto não depender de rede para desenhar um ícone.
// Para acrescentar um, edite USADOS no script e rode \`npm run icons\`.

export const VIEWBOX = "0 0 ${largura} ${altura}";

export const ICONES = ${JSON.stringify(saida, null, 2)};
`;

mkdirSync(resolve(raiz, "src"), { recursive: true });
writeFileSync(resolve(raiz, "src/icones.gerado.js"), texto, "utf8");
console.log(
  `${Object.keys(saida).length} ícones gravados em src/icones.gerado.js`,
);

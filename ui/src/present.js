// A camada de apresentacao.
//
// O motor fala em enums: `WAITING_HUMAN`, `SEM_CREDENCIAL`, `stalled_work`,
// `POLICY_DENIED`. Uma pessoa nao. Traduzir aqui -- e num lugar so -- evita que
// cada tela invente o proprio vocabulario, que e como duas partes da interface
// passam a chamar a mesma coisa de dois nomes.
//
// O que este arquivo NAO faz e inventar semantica. O significado de cada estado
// vem de `core/states.py` e chega em `state.meaning`; o que esta aqui e o
// ROTULO CURTO que cabe num badge, e o tom da cor. Um termo que ninguem mapeou
// aparece como veio -- e assim quem ler descobre que falta mapear, em vez de
// ver uma traducao inventada.

/** Primeira letra maiuscula, sem mexer no resto. */
export const Frase = (t) => {
  const s = String(t ?? "").trim();
  return s ? s[0].toUpperCase() + s.slice(1) : "";
};

export const when = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString("pt-BR");
};

export const hora = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? String(iso)
    : d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
};

// ---------------------------------------------------------------------------
// Estados
// ---------------------------------------------------------------------------

const LABEL = {
  // Estados de task (core/states.py)
  DISCOVERED: ["Descoberta", "idle"],
  ANALYZING: ["Em análise", "info"],
  READY: ["Pronta", "info"],
  ASSIGNED: ["Reservada", "info"],
  IMPLEMENTING: ["Em execução", "info"],
  TESTING: ["Em testes", "info"],
  PR_CREATED: ["Pull request aberto", "info"],
  CI_RUNNING: ["Aguardando CI", "info"],
  AI_REVIEW: ["Em revisão", "info"],
  WAITING_HUMAN: ["Precisa de você", "warn"],
  APPROVED: ["Aprovada", "ok"],
  MERGING: ["Integrando", "info"],
  DEPLOYING: ["Publicando", "info"],
  QA_STAGING: ["Em verificação", "info"],
  DONE: ["Concluída", "ok"],
  BLOCKED: ["Bloqueada", "danger"],
  FAILED: ["Falhou", "danger"],
  CANCELLED: ["Cancelada", "idle"],

  // Estados de execucao (core/model.py::RunState). `RUNNING` e `FAILED` valem
  // para task e para run com o mesmo sentido; os tres abaixo so para run.
  RUNNING: ["Em execução", "info"],
  SUCCEEDED: ["Concluída", "ok"],
  INTERRUPTED: ["Interrompida", "warn"],
  ABORTED: ["Abortada pelo motor", "warn"],

  // Saude (engine/health.py::Level)
  OK: ["Saudável", "ok"],
  ATTENTION: ["Precisa de atenção", "warn"],
  STUCK: ["Parado sem progresso", "danger"],
  UNKNOWN: ["Não deu para saber", "warn"],

  // Prontidao de conexao (app/api.py::_connection_state)
  PRONTO: ["Pronto", "ok"],
  SEM_CREDENCIAL: ["Falta credencial", "warn"],
  REVOGADA: ["Credencial revogada", "danger"],
  EXPIRADA: ["Credencial expirada", "danger"],
  NAO_CONFIGURADO: ["Não configurado", "idle"],

  // Situacao de credencial (core/credential.py::Status)
  ACTIVE: ["Válida", "ok"],
  EXPIRED: ["Expirada", "danger"],
  REVOKED: ["Revogada", "idle"],
};

export const rotulo = (k) =>
  (LABEL[k] || [Frase(String(k ?? "").replace(/_/g, " ")), "idle"])[0];
export const tom = (k) => (LABEL[k] || ["", "idle"])[1];

/**
 * As fases do processamento, em minuscula.
 *
 * Elas moram fora de `LABEL` porque a frase que as usa e "Regente pausado", e
 * porque `RUNNING` significa duas coisas diferentes no motor: a fase do
 * processo e o estado de uma execucao. Um mapa so obrigaria uma das duas a usar
 * a palavra da outra.
 */
export const FASE = {
  RUNNING: ["em execução", "ok"],
  PAUSED: ["pausado", "warn"],
  STOPPED: ["parado", "idle"],
  STARTING: ["iniciando", "warn"],
  STOPPING: ["parando", "warn"],
  DEGRADED: ["sem processo", "danger"],
};

export const fase = (f) => FASE[f] || [String(f ?? "").toLowerCase(), "idle"];

/** A frase que explica cada fase, do ponto de vista de quem opera. */
export const EXPLICA_FASE = {
  RUNNING: "O Regente está processando tasks automaticamente.",
  PAUSED: "O Regente está ligado e não está pegando trabalho novo.",
  STOPPED: "Nenhuma task será processada até você iniciar o Regente.",
  STARTING: "O pedido foi registrado. Aguardando o processo responder.",
  STOPPING: "A parada foi pedida. Um ciclo ainda está terminando.",
  DEGRADED: "Você pediu para rodar, e nenhum processo respondeu.",
};

/** Quem move isto daqui (StateView.owner), em portugues. */
export const DONO = {
  engine: "O Regente segue sozinho",
  human: "Espera uma decisão sua",
  external: "Espera um sistema de fora",
  nobody: "Ninguém — precisa de configuração",
};

// ---------------------------------------------------------------------------
// Integracoes: o que o provedor mostra, e o que este workspace usa
// ---------------------------------------------------------------------------

/**
 * Conectou, e algo ficou faltando.
 *
 * Não é erro: a conexão está gravada. É o que a pessoa precisa saber para não
 * descobrir sozinha, de madrugada, que o Regente parou de trabalhar.
 */
export const AVISO_DA_CONEXAO = {
  motor_sem_acesso:
    "Conectado. Mas o Regente ainda não pode usar esta conexão sozinho nos " +
    "ciclos automáticos — peça a quem administra o acesso.",
};

/** Onde um recurso esta entre existir e ser usado (core/resource.py::Situacao). */
export const SITUACAO = {
  DISPONIVEL: { texto: "Disponível", tone: "muted" },
  SELECIONADO: { texto: "Em uso", tone: "ok" },
  // Nunca "sumiu". Pode ter sumido, pode ter perdido acesso, pode ser que a
  // busca nem tenha chegado a rodar — e a frase precisa dizer o que se
  // observou, e não uma causa que ninguém apurou.
  NAO_ENCONTRADO: { texto: "Não veio na última busca", tone: "warn" },
};

/** Que tipo de coisa o Regente faz com um recurso (core/resource.py::Kind). */
export const PAPEL_DO_RECURSO = {
  task_source: "De onde vem o trabalho",
  code: "Onde o código é escrito",
  container: "Só para navegar",
};

/**
 * Por que uma busca não respondeu, e o que fazer a respeito.
 *
 * Cada uma manda a pessoa a um lugar diferente. Reduzir todas a "erro ao
 * buscar" apagaria justamente a diferença — e a pessoa iria procurar o
 * problema na rede quando o que faltava era uma credencial.
 */
export const FALHA_DA_BUSCA = {
  SEM_CREDENCIAL: {
    titulo: "Falta uma credencial para este serviço",
    saida: "Registre uma em Configuração › Credenciais.",
  },
  CREDENCIAL_EXPIRADA: {
    titulo: "A credencial deste serviço venceu",
    saida: "Registre uma nova em Configuração › Credenciais.",
  },
  CREDENCIAL_REVOGADA: {
    titulo: "A credencial deste serviço foi revogada",
    saida: "Registre uma nova em Configuração › Credenciais.",
  },
  SEM_CAPACIDADE: {
    titulo: "Esta credencial não foi autorizada a listar",
    saida:
      "Listar e ler são permissões separadas. Registre a credencial " +
      "novamente incluindo a permissão de listar.",
  },
  POLICY_RECUSOU: {
    titulo: "A política deste workspace recusou a busca",
    saida: "Fale com quem administra as políticas.",
  },
  PROVEDOR_INDISPONIVEL: {
    titulo: "Não deu para falar com o serviço agora",
    saida: "Tente de novo em alguns instantes. Nada foi alterado.",
  },
  NAO_SUPORTADO: {
    titulo: "Este serviço não lista recursos",
    saida: "Nada a fazer aqui — configure-o em Conexões.",
  },
};

/** Como cada nível da árvore de cada provedor se chama em português. */
export const TIPO_DE_RECURSO = {
  account: { um: "conta", muitos: "Contas" },
  repository: { um: "repositório", muitos: "Repositórios" },
  project: { um: "projeto", muitos: "Projetos" },
  board: { um: "board", muitos: "Boards" },
  workspace: { um: "workspace", muitos: "Workspaces" },
  space: { um: "espaço", muitos: "Espaços" },
  list: { um: "lista", muitos: "Listas" },
};

/** O nome de um tipo que a tela ainda não conhece continua legível. */
export const tipoPlural = (t) => TIPO_DE_RECURSO[t]?.muitos || rotulo(t);

// ---------------------------------------------------------------------------
// Saude
// ---------------------------------------------------------------------------

/**
 * As verificacoes de saude, ditas como pergunta.
 *
 * O motor nomeia cada sinal por uma chave (`stalled_work`, `database_growth`),
 * e sem isto a tela mostrava "Stalled_work". A traducao e de ROTULO: a resposta
 * e a evidencia continuam sendo as que o motor escreveu, palavra por palavra.
 */
const SINAL = {
  running: "Há trabalho executando?",
  stalled_work: "Alguma task parou no meio?",
  expired_leases: "Alguma reserva de trabalho venceu?",
  interrupted_runs: "Alguma execução foi interrompida?",
  orphan_areas: "Sobrou área de trabalho sem dono?",
  dead_end_tasks: "Alguma task ficou sem saída?",
  budget: "O orçamento do dia aguenta?",
  escalations: "Há decisão esperando?",
  provider_failures: "Algum serviço externo falhou?",
  database_growth: "O banco está crescendo demais?",
  deliveries_in_flight: "Há entrega sem desfecho?",
  waiting_on_a_person: "Alguém precisa decidir alguma coisa?",
};

export const pergunta = (k) =>
  SINAL[k] || Frase(String(k ?? "").replace(/_/g, " "));

/** Exportado so para o teste que confere que nenhum sinal do motor ficou sem
 *  pergunta -- um identificador em ingles na tela e um mapa desatualizado. */
export const SINAIS_CONHECIDOS = Object.keys(SINAL);

// ---------------------------------------------------------------------------
// Providers, capacidades e permissoes
// ---------------------------------------------------------------------------

/** Cada papel de provider, dito em produto e nao em arquitetura. */
export const PAPEL = {
  tasks: ["Board de tasks", "De onde o Regente lê o trabalho a fazer."],
  repository: ["Repositório", "Para o Regente enxergar o código e as branches."],
  repository_write: [
    "Publicação de mudanças",
    "Para enviar commits e abrir pull requests.",
  ],
  cicd: ["Integração contínua", "Para acompanhar os checks de cada commit."],
  runner: ["Agente", "O modelo que escreve as mudanças."],
  workspace_provider: [
    "Área de trabalho",
    "Onde cada execução fica isolada, nesta máquina.",
  ],
  observer: ["Observação", "Para onde vão os registros do que aconteceu."],
};

/** Capacidade de credencial (core/credential.py::Use), em portugues. */
export const PERMISSAO = {
  // Descobrir e ler sao capacidades separadas de proposito: uma credencial de
  // descoberta serve para uma pessoa escolher o que o workspace vai usar, e nao
  // serve para o Regente trabalhar. A frase precisa deixar isso visivel a quem
  // marca a caixinha -- e por isso ela diz "listar", e nunca "ler".
  "task.discover": "Listar projetos e boards",
  "repo.discover": "Listar repositórios",
  "task.read": "Ler tasks",
  "task.write": "Atualizar tasks",
  "repo.read": "Ler repositórios",
  "repo.push": "Enviar commits",
  "repo.pr": "Abrir pull requests",
  "ci.read": "Ler resultados de CI",
  "agent.run": "Executar o agente",
};

/** Qual uso provar ao testar a conexao de cada papel. */
export const USO_POR_PAPEL = {
  tasks: "task.read",
  repository: "repo.read",
  repository_write: "repo.pr",
  cicd: "ci.read",
  runner: "agent.run",
};

/** Capacidades de acesso (core/access.py::Ability), em portugues. */
export const ACAO = {
  "approval.decide": "Decidir escalações",
  "workspace.resource.select": "Escolher o que o workspace usa",
  "workspace.access.grant": "Conceder acesso",
  "workspace.access.revoke": "Revogar acesso",
  "workspace.access.list": "Ver quem tem acesso",
  "workspace.credential.grant": "Registrar credenciais",
  "workspace.credential.revoke": "Revogar credenciais",
  "workspace.credential.list": "Ver credenciais",
  "workspace.credential.use": "Usar credenciais",
  "workspace.engine.control": "Ligar e desligar o Regente",
  "workspace.settings.write": "Alterar a configuração",
};

/** O que a sonda de conexao encontrou do outro lado. */
export const ALCANCE = {
  AUTHENTICATED: "Aceitou a credencial",
  REJECTED: "Recusou a credencial",
  UNAVAILABLE: "Não respondeu",
  UNKNOWN: "Não chegou a ser perguntado",
};

// ---------------------------------------------------------------------------
// Recusas
// ---------------------------------------------------------------------------

/**
 * Uma recusa do servidor, dita para gente.
 *
 * O codigo tecnico continua disponivel em "Detalhes tecnicos". O que muda e
 * qual dos dois ocupa a frase principal: `POLICY_DENIED` na cara de alguem que
 * nao conhece a arquitetura nao informa nada, e ainda parece defeito.
 */
export const PORTA = {
  400: "Os dados enviados não são válidos.",
  401: "Esta sessão não está autenticada. Reabra a Mission Control.",
  403: "Esta identidade não tem autoridade para isto aqui.",
  404: "Isto não existe, ou você não tem acesso a ele neste workspace.",
  405: "Este endereço não aceita esta operação.",
  409: "Alguém alterou isto antes de você. Nada do que você fez foi perdido.",
  422: "O que você escolheu não está entre as opções oferecidas.",
  500: "O Regente falhou ao responder.",
  501: "Esta instalação do Regente não oferece esta operação.",
};

/**
 * O que dizer sobre uma escrita recusada.
 *
 * O detalhe do motor vem primeiro quando existe: ele nomeia o campo, o balde ou
 * a capacidade que faltou, e isso e mais util que qualquer frase generica que a
 * tela pudesse inventar.
 */
export function digaOErro(r) {
  const detalhe = r?.payload?.detail;
  return detalhe || PORTA[r?.status] || "A operação foi recusada.";
}

export const tecnicoDaRecusa = (r) =>
  `HTTP ${r?.status ?? "?"}${r?.payload?.error ? " · " + r.payload.error : ""}` +
  `${r?.payload?.detail ? " · " + r.payload.detail : ""}`;

// ---------------------------------------------------------------------------
// Regras de selecao
// ---------------------------------------------------------------------------

const OPERADOR = {
  contains: "contém",
  equals: "é igual a",
  starts_with: "começa com",
  ends_with: "termina com",
  in: "está entre",
  lt: "é menor que",
  gt: "é maior que",
  regex: "casa com",
};

const CAMPO = {
  title: "o título",
  key: "a chave",
  project: "o projeto",
  status: "o status",
  labels: "os rótulos",
  priority: "a prioridade",
  assignee: "o responsável",
  type: "o tipo",
  components: "os componentes",
};

/** Uma regra de selecao dita em portugues. Se o formato crescer, este e o
 *  unico lugar que muda. */
export function fraseDaRegra(r) {
  const op = OPERADOR[r.match] || r.match;
  const campo = CAMPO[r.field] || r.field;
  const efeito =
    r.effect === "exclude"
      ? "a task fica fora da fila"
      : r.effect === "require"
        ? "só tasks assim entram na fila"
        : `a prioridade muda em ${r.delta} (${
            r.delta < 0 ? "roda antes" : "roda depois"
          })`;
  return `quando ${campo} ${op} “${r.value}”, ${efeito}`;
}

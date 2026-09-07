# Arquitetura

Regente é um **sistema operacional para agentes de engenharia de software**. O
agente trabalha, o Orchestrator coordena, as ferramentas executam, as policies
protegem, e a fila chama o humano só quando a resposta não existe dentro do
sistema.

## As camadas

```
                    cli / ui          superfície
                       │
                   app/              raiz de composição — o único lugar
                       │             que conhece config + adapters + motor
        ┌──────────────┴──────────────┐
     engine/                      adapters/
   orchestrator                jira, github, gcloud…
   scheduler                   (todo nome de ferramenta vive aqui)
   store, gate                        │
   supervisor                         │
        └──────────────┬──────────────┘
                    ports/            contratos de capacidade
                       │
                    core/             domínio puro: sem I/O, sem fornecedor
```

A dependência aponta sempre para dentro. `core/` não importa nada; `engine/` fala
só com `ports/`; adapters implementam portas; `app/` amarra tudo.

**Isso não é convenção — é testado.** `tests/test_fronteiras.py` lê o código-fonte
e falha se `core/` importar I/O, se `engine/` importar adapter, ou se um nome de
ferramenta aparecer em código (não em docstring) dentro de `core/`, `engine/` ou
`ports/`. A regra quebra em CI, não em revisão de código.

## As três invariantes

**1. Estado por diferença, no disco.** O motor nunca depende do contexto de
conversa de um agente para saber onde o trabalho parou. Task, Run, Event,
Approval e Lease vivem em SQLite. Matar o processo no meio de um despacho é
operação suportada: o lease vence, o run vira `INTERRUPTED`, a task volta para a
fila e o próximo tick continua. O tick recorrente **é** o mecanismo de retry — não
existe código de retry no meio do fluxo.

**2. Nenhuma ação crítica depende do julgamento do LLM.** Todo pedido de
ferramenta atravessa o portão:

```
Agent → ToolRequest → [risco] → [policy] → ALLOW / DENY / HUMAN_APPROVAL → Tool
```

O portão refaz o julgamento do zero. Não aceita risco pré-calculado, nem
justificativa, nem veredito de quem chama. Um agente convencido por um comentário
de PR ainda esbarra nele — porque quem decide não lê opinião.

**3. Conteúdo externo é dado, nunca ordem.** Título de task, descrição,
comentário, diff: tudo é texto escrito por terceiros. Tentativa de manipulação
vira achado, não instrução.

## Risco e policy decidem coisas diferentes

Esta é a distinção que define o produto, e é a que faz o dono não virar gargalo.

| | pergunta que responde | efeito de "alto" |
|---|---|---|
| **Policy** | *quem pode fazer isso?* | `HUMAN_APPROVAL` — a organização decidiu que essa assinatura tem dono |
| **Risco** | *quanta prova essa ação exige?* | segunda passada adversarial — compra **trabalho**, não espera |

Se risco alto virasse fila de espera, todo PR bom ficaria parado aguardando
assinatura — exatamente o custo que o motor existe para eliminar. Quem para o
trabalho é a regra escrita, não a hesitação do modelo.

Ortogonal aos dois: o **teto de autonomia** (L0 leitura → L4 produção) por
workspace e projeto. Estourar o teto vira `HUMAN_APPROVAL`, nunca `DENY` — o
humano continua podendo autorizar. Quem nega de vez é a regra.

O Policy Engine é **default deny**: ação sem regra que a permita não acontece. E
**vence o mais restritivo**: um `DENY` não é anulado por nenhum `ALLOW`, porque
ordem de arquivo não pode decidir segurança.

## Paralelismo sem atropelo

O Orchestrator monta um grafo de dependências e o scheduler escolhe o que roda
agora. Antes de abrir dois slots ele verifica quatro coisas: dependências
concluídas, conflito de recurso, ciclo, e limites (slots, teto diário).

Conflito é **declarado**, não adivinhado. Cada task diz quais chaves de recurso
toca em exclusividade — `repo:api`, `migration:worker`, `file:src/auth.py` — e
qualquer interseção é exclusão mútua. Detectar conflito semântico de verdade
exige ler o código: isso é trabalho de um agente de análise, que alimenta essa
lista. O scheduler nunca conclui sozinho que dois diffs "provavelmente" convivem.

Reserva acontece dentro do próprio plano: duas candidatas do mesmo tick que
compartilham recurso não saem juntas. Esquecer isso é o jeito clássico de
despachar dois workers para a mesma migration.

## A máquina de estados

Dezoito estados, tabela explícita, transição fora dela levanta erro. Três
propriedades que não são óbvias:

- **`WAITING_HUMAN` guarda de onde pausou.** Não é destino, é pausa. O humano
  manda seguir, refazer ou encerrar — mas não teletransporta a task: aprovar um
  deploy não é declarar o trabalho pronto.
- **Escalar é sempre possível.** De qualquer estado não-terminal existe caminho
  para `WAITING_HUMAN`. O motor nunca fica sem a opção de parar e perguntar.
- **Estado ativo devolve à fila.** Worker morto → `READY`. Manter a task no
  estado ativo "para preservar o progresso" não preserva nada: o progresso vive
  na área de trabalho e na branch, não no rótulo. A task ficaria viva no papel e
  parada de verdade — o pior modo de falha possível, porque nada acusa.

O que preserva o trabalho parcial é a **área ser endereçada pela task, não pela
tentativa**: a retomada reabre a mesma árvore, com os commits WIP.

## Escada de recuperação

```
falhou → retenta → troca de estratégia → escala ao humano
```

Finita de propósito, e cada degrau precisa ser *diferente* do anterior. Retentar
idêntico depois de um erro determinístico é gastar dinheiro mais devagar. Além
disso, todo agente tem teto de iterações, tool calls, custo e tempo, e um detector
de não-progresso que acusa quatro padrões: mesmo erro, mesmo arquivo, mesmo teste,
mesma decisão.

## O que entra na fila NEEDS ME

Critério estreito, porque encher a fila é o jeito garantido de fazer o dono parar
de lê-la. Entra só o que **não tem resposta dentro do sistema**: autoridade humana
exigida por policy, contrato ambíguo que leitura extra não resolve, escada de
recuperação esgotada, ou backlog em ciclo.

**Não entra:** risco alto (compra segunda passada), teste vermelho (é trabalho),
erro transitório (é retentativa).

Cada item carrega decisão, não diagnóstico: o que aconteceu, por que importa, o
que o agente já tentou, opções, recomendação, risco. Log fica no evento, sob
demanda.

## Operacao continua: tempo, falha e volta

O motor existe para trabalhar sem alguem olhando. Ate o marco 8 isso era uma
premissa, nao um fato -- toda prova anterior era uma invocacao supervisionada.

### Um relogio, nao dois

Toda a recuperacao deste motor e comparacao de timestamp: lease vencido prova
que o worker morreu, `updated_at` diz ha quanto tempo a task nao anda, o dia
decide quando o orcamento reinicia. Se quem carimba e quem pergunta usam
relogios diferentes, nada disso funciona -- e nao falha ruidosamente, falha em
silencio.

Foi o que aconteceu: lease carimbado por um relogio, verificado contra outro,
run carimbado por um terceiro. Um run ficou `RUNNING` com lease "vivo" por 103
ticks seguidos -- quatro dias simulados -- e o health respondeu OK o tempo todo.

Agora `SqliteStore` e `Orchestrator` recebem o mesmo `clock`. Producao nao passa
nada e recebe o relogio real, como antes.

### O motor nunca estaciona uma task

Transicao legal e transicao que alguem executa sao coisas diferentes, e a
diferenca e invisivel: a task fica num estado que parece ocupado, o scheduler a
ignora porque parece ocupada, e os ticks seguintes voltam limpos para sempre.

`ENGINE_ADVANCES` diz de quais estados este motor tem codigo para sair.
`is_terminus()` acusa estado ativo do qual ele nao sai. Ao chegar num terminus, a
task vai para uma pessoa -- com motivo escrito -- em vez de ficar parecendo
ocupada. Quando um marco futuro adicionar a etapa, adiciona o estado la.

### A porta de volta

Escalar so serve se der para voltar. `regente decide` gravava a escolha e
imprimia que o proximo tick retomaria a task; nenhum tick lia de volta. Todo
escalonamento era porta de mao unica.

O tick agora consome decisoes. O destino sai da propria maquina de estados --
`resumable_from(paused_at)` -- nunca de uma lista escrita de memoria. Decisao que
o motor nao reconhece tambem move a task: escolha nao interpretavel que nao move
nada e a fila parando em silencio outra vez.

### `regente health`

Doze perguntas respondidas **do disco**. Se o motor morrer as tres da manha,
`regente health` as nove ainda responde -- relatorio montado da memoria de um
processo vivo estaria vazio exatamente quando importa, e relatorio vazio parece
saudavel.

Quatro niveis, e a ordem importa: `OK < ATTENTION < UNKNOWN < STUCK`. `UNKNOWN`
fica acima de `ATTENTION` de proposito -- o que o motor nao consegue ver e mais
perigoso do que o que ele ve e nao gosta. `UNKNOWN` nunca e saudavel.

Codigo de saida: 0 saudavel, 1 atencao ou nao examinado, 2 travado. Cron le sem
interpretar prosa.

### Crescimento: medido, com o log separado do dado

O write-ahead log e churn, nao crescimento: um checkpoint o dobra para dentro do
arquivo e ele encolhe. Reportar os dois juntos mediria ruido -- uma corrida
mostrou "3,4MB de banco" para 108 eventos, sendo 200KB de dado e o resto log.

`checkpoint()` e politica explicita de manutencao e MOVE paginas ja
comprometidas; nada e apagado. Nao existe retencao que delete linha.

Medido em mil ticks: crescimento linear, 4,20 eventos/tick na primeira metade e
4,18 na segunda; runs, leases e aprovacoes limitados, sem acumular.

### Injecao de falha

Os faults **embrulham** providers reais em vez de substitui-los. Mock devolvendo
falha enlatada testa a ideia que o mock tem de falha; wrapper que deixa o adapter
real rodar e entao interrompe testa o motor. E o cronograma e deterministico:
corrida que falha diferente a cada vez nao serve para provar conserto.

Morte de worker nao levanta excecao. Excecao e um relatorio, e worker morto nao
relata nada -- o run fica RUNNING, o lease fica preso ate vencer, e a
recuperacao tem que descobrir sozinha, pelo disco.

## O agente e um executor, nunca uma autoridade

O contrato responde quatro perguntas e recusa cinco.

```
Responde:                          Nao responde:
  Posso executar este agente?        Este agente e confiavel?
  Como executo?                      Ele pode commitar?
  Que capacidades ele expoe?         Ele pode dar push?
  O que aconteceu quando rodou?      Ele pode abrir PR?
                                     Ele pode fazer deploy?
```

As cinco da direita continuam sendo do Engine e da Policy. Nenhum campo do
adapter fala sobre elas, e um teste estrutural garante isso -- campo com nome de
autoridade num tipo do adapter seria o fornecedor votando na propria permissao.

### Prontidao: seis eixos, duas autoridades

```
adapter -> executable | protocol | authentication | agent
engine  -> policy | budget
```

Os quatro primeiros so o adapter sabe. Os dois ultimos so o motor pode decidir --
adapter que preenchesse o proprio `policy=ALLOW` seria fornecedor se autorizando.
Consequencia: **adapter sozinho nunca fica READY**.

`UNKNOWN` bloqueia. "Nao deu para checar" nunca vira "esta tudo bem", e o
primeiro eixo que falha e o reportado: consertar um eixo posterior enquanto um
anterior esta quebrado nao resolve nada.

### Autenticacao e assunto do adapter

Cinco formatos de troca, nenhum preferido:

| Modo | Quem guarda a credencial |
|---|---|
| `SESSION` | a propria ferramenta (assinatura corporativa, login de CLI, SSO) |
| `RESOLVED_SECRET` | o motor, escopado ao workspace |
| `GATEWAY` | o motor, para um intermediario |
| `DELEGATED` | um processo hospedeiro; nunca presumido, reportado `UNKNOWN` |
| `NONE` | ninguem |

Sao formatos, nao produtos -- e por isso podem viver na port. Diagnostico que
dissesse "falta a variavel X" daria conselho errado para todo cliente que
autentica de outra forma, que e a maioria deles.

### Vendors nao se conhecem

```
core/ | ports/ | engine/
        v
  agent contract
        v
adapters/runner/            <- base e infraestrutura compartilhada
adapters/runner/vendors/    <- um modulo por fornecedor
```

Modulo em `vendors/` nunca importa outro modulo em `vendors/`. Regra estrutural,
verificada por AST, e existe porque a violacao aconteceu aqui: o segundo perfil
importou um helper do primeiro, nada quebrou, a suite ficou verde, e a
propriedade que este marco afirma -- trocar de agente e um arquivo -- tinha
deixado de ser verdade em silencio.

Trabalho compartilhado sobe um nivel. O que um segundo fornecedor ia querer nao
e, por definicao, especifico de fornecedor.

### A restricao mora fora do modelo

O agente recebe leitura e edicao. Nao recebe nenhuma ferramenta que execute
comando. `git push`, `gh pr create`, `gcloud`, `terraform` e toda rota de
escalonamento que ninguem pensou ainda sao variacoes de uma capacidade so, e
negar essa capacidade fecha todas de uma vez.

Duas rotas indiretas ficaram, e ambas estao fechadas:

- **`.git/config`.** O agente so edita arquivos, mas reescrever o remote
  converte um push recusado em permitido. `git status` nao ve nada dentro de
  `.git/`, entao a guarda e impressao digital, nao diff.
- **A suite de testes.** O agente escreve arquivos, testes sao arquivos, e o
  motor executa a suite para chegar a um veredito -- entao um agente que nao
  executa nada podia fazer o MOTOR executar por ele. Escrever teste e trabalho
  que queremos; dar carteira a esse codigo nao e. O ambiente da verificacao e
  composto do zero, igual ao do agente.

### Capacidade nao e permissao

`AgentCapabilities.runs_commands` diz o que a ferramenta CONSEGUE fazer.
`Permissions.run_commands` diz o que o motor permite. De fora parecem iguais e
pedem respostas opostas: a primeira e uma configuracao com a qual conviver, a
segunda e uma fronteira a fazer valer.

## Multi-tenancy

`Organization → Client → Workspace → Project → Repository`, presente desde a
primeira tabela. Cada Task, Run e Event carrega `workspace_id`, e a identidade
externa é única *por workspace* — o mesmo `FAXINA-183` em dois clientes são duas
tasks, e nunca colidem. Enfiar tenancy depois exigiria migrar todas as tabelas e
revisar toda consulta; a consulta esquecida é justamente a que vaza dado do
cliente A para o cliente B.

Credencial e autonomia moram no **workspace**, não no projeto — é ali que a
fronteira entre clientes precisa ser inviolável.

## Decisões tomadas, e por quê

| decisão | escolha | motivo |
|---|---|---|
| linguagem | Python 3.13 | ecossistema de agentes e de ferramentas do ambiente |
| persistência | SQLite + WAL atrás da porta `Store` | zero-ops, transacional, legível à mão; Postgres entra sem tocar no Core |
| atomicidade | `BEGIN IMMEDIATE` em transição e lease | são os dois pontos onde um despacho duplicado nasce |
| isolamento | porta `WorkspaceProvider`; diretório agora, worktree para código | worktree é nativo do git; container é peso desnecessário hoje |
| execução do agente | porta `AgentRunner` | mantém o Core agnóstico a harness; runner é trocável sem tocar no motor |
| config | YAML + policies em arquivo separado | policy precisa ser revisável e diferente sem mexer no resto |
| sombra | `true` por padrão | modo vivo é decisão explícita do dono, nunca default |

## O que ainda não existe

Deliberadamente: UI, adapters de fornecedor real, agente que escreve código,
LLMProvider implementado, detecção semântica de conflito, deploy. As **portas**
desses existem e são estáveis; as implementações vêm nos marcos 2–9 do
[ROADMAP](ROADMAP.md).

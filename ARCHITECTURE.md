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
- **Estado ativo não é uma coisa só.** `OWNED_ACTIVE` tem um worker dentro e
  sempre implica um run vivo; `AWAITING_EXTERNAL` — `PR_CREATED`, `CI_RUNNING`,
  `AI_REVIEW` — espera um sistema de fora e por definição não tem ninguém
  dentro. Tratar os dois como um só custa nos dois sentidos: exigir run vivo
  devolve à fila uma task que só aguarda o CI, e não exigir deixa passar a task
  que ficou órfã. O que `AWAITING_EXTERNAL` precisa ter não é um run, é um
  registro de entrega — sem ele não há a que voltar.

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

## Dois clientes, um motor: identidade nunca vem do nome local

A forma fraca da prova de tenancy da nomes diferentes a cada cliente e mostra que
sao diferentes. Isso prova que os nomes sao diferentes.

A prova de verdade da aos dois clientes **os mesmos nomes locais** -- mesma chave
de task, mesmo repositorio, mesmo recurso, mesma branch, ate o mesmo nome de
workspace -- e mostra que continuam entidades distintas:

```
Organization: acme
|-- client-a / workspace "main" : TASK-1, repo "worker", repo:database
`-- client-b / workspace "main" : TASK-1, repo "worker", repo:database
```

Um banco SQLite so. Bancos separados nao provariam nada: a pergunta e se a
fronteira aguenta com as linhas lado a lado.

Se em qualquer ponto da cadeia

```
task -> target -> mission -> workspace -> lease -> run -> delivery
```

a identidade global for derivada de um nome local, esses dois colidem.

### Escopo e obrigatorio, nunca opcional

Duas operacoes aceitavam `workspace_id` opcional e caiam para uma busca global
quando ele faltava -- com um comentario argumentando que era seguro porque um id
de run e unico. "Seguro porque os ids nao colidem" e uma esperanca, nao uma
fronteira, e escopo opcional fica a uma chamada distraida de um delete entre
clientes. Hoje `renew_lease` e `release_lease` exigem o workspace: ausencia de
tenancy torna a operacao impossivel, nunca global.

### Ler escopado e escrever escopado sao coisas diferentes

`store.transition` nao tinha escopo nenhum. A leitura ja era escopada, entao um
cliente **nao conseguia ver** a task do outro -- e conseguia **move-la**,
bastando ter o id. Dez linhas separavam as duas operacoes.

Achado por um teste adversarial de substituicao de id, nao por leitura. Hoje
toda transicao do tick passa por um helper que informa o tenant, porque
convencao e comentario e helper e codigo.

### Id que vem de fora nao carrega autoridade

Ids sao globalmente unicos, entao um id vindo de outro cliente *funcionaria*. A
autoridade tem que vir do contexto de quem chama, e nao do identificador que lhe
entregaram:

- `task(id, workspace_id)` responde como se nao existisse
- `run(id, workspace_id)` idem
- `approval(id, workspace_id)` idem
- `decide_approval(..., workspace_id)` recusa
- `transition(..., workspace_id)` recusa

O caso mais exposto e `regente decide <id>`: o id vem de um teclado. O workspace
vem do motor.

### Telemetria global e dado de tenant

```
global operational telemetry     tenant-scoped operational data
  quantos clientes existem         chave de task
  quantos runs ativos              repositorio
  tamanho do banco                 referencia de segredo
                                   escalonamento, evento, run
```

A primeira coluna e contagem e nao carrega conteudo. A segunda pertence a um
tenant e nunca aparece numa visao global. `regente health` responde por um
workspace: um operador olhando o cliente A nao ve nada de B.

### Estado global por design, ou nenhum

Uma auditoria varre todo estado mutavel de modulo. Cada nome precisa estar numa
lista explicita de "global por design, sem dado de tenant" -- hoje sao tabelas de
consulta constantes e o registro de fabricas de adapter. Cache algum, keyed por
nome local, sobrevive a essa lista: funcionaria perfeitamente com um cliente e
vazaria com dois.

## Concorrencia real: posse, e o direito de agir

`max_workers > 1` e uma configuracao. Concorrencia e um fato sobre processos, e
ate o marco 9 o projeto so tinha a configuracao: uma corrida de mil ticks com
`max_workers=3` nunca segurou mais de um lease ao mesmo tempo, entao lease,
`BEGIN IMMEDIATE` e a chave `(workspace_id, resource)` eram codigo nao testado
com aparencia de testado.

### Lease prova quem comecou; posse prova quem pode terminar

Um lease impede dois workers de pegarem o mesmo recurso no mesmo instante. Ele
nao impede o primeiro de **continuar** depois de deixar de ser dono, e sao
problemas diferentes.

Observado na primeira corrida real de tres processos: um worker terminou a
missao e escreveu o resultado de uma task que ja nao era dele. Foi recusado
apenas porque a transicao que tentou era ilegal a partir do estado em que a task
por acaso estava. Se estivesse um estado adiante -- o caso comum -- a escrita
seria legal e teria sobrescrito o trabalho de outro worker.

Entao a posse e verificada duas vezes: uma para comecar, outra imediatamente
antes de qualquer escrita feita em nome do run. Entre os dois momentos o mundo
tem permissao de mudar, e a segunda verificacao e a unica coisa que percebe.

### Renovar, e perceber quando a renovacao e recusada

O comentario de `lease_seconds` prometia "o worker renova" muito antes de
qualquer coisa renovar. Agora um heartbeat renova enquanto a missao roda -- sem
isso, missao mais longa que a janela perde os recursos fazendo tudo certo.

O `WHERE ... AND owner=?` da renovacao e o que torna tudo seguro: worker cujo
lease venceu e foi tomado nao atualiza nada e fica sabendo. Sem essa clausula
ele estenderia em silencio um lease que ja nao e dele, e dois workers acreditariam
ser donos do mesmo recurso.

E recusar o resultado nao basta. Uma campanha de contencao mostrou dois
processos rodando a mesma task ao mesmo tempo na mesma area: nenhuma escrita
sobreviveu, mas ambos ja tinham executado, e dois agentes editando um diretorio
produzem uma bagunca que veredito nenhum desfaz. Perder a posse agora **cancela**
a missao. Runner que dirige subprocesso e morto de verdade; runner em processo
so pode cooperar, e isso esta dito onde importa em vez de assumido.

### Uma transacao, nao duas

Despachar escreve o run, toma todos os leases e move a task -- tudo dentro de um
`BEGIN IMMEDIATE`. Ou existe um run segurando todos os recursos com a task
movida, ou nada foi escrito.

Eram tres escritas separadas, e cada emenda tinha uma janela:

- entre os leases e o run: um `SIGKILL` deixava lease vivo com dono que nao
  existia como run. A recuperacao encontra worker morto cruzando lease vencido
  com run ativo, entao orfao nao casava com nada e travava o recurso ate vencer,
  sem relatorio nenhum capaz de explicar. Aparecia em 2 de 25 rodadas.
- entre o lease e a transicao: outra worker escalava a task no meio, e sobrava um
  run vivo segurando recursos de uma task que ele nao podia ter.

Janela mais estreita teria virado 1 em 250 e continuaria o mesmo defeito.

### Perder uma corrida e normal, nao e erro

Dois workers lendo o mesmo board chegam a mesma conclusao no mesmo instante. Um
chega primeiro; a transicao do outro e recusada. Isso e a maquina de estados
funcionando -- e derrubava o tick inteiro do perdedor com `InvalidTransition`,
entao uma recusa correta custava um ciclo completo de trabalho.

Agora o motor distingue tres casos e so o ultimo e erro:

- a task ja esta onde eu queria por-la -> outro fez meu trabalho
- a task nao esta mais de onde eu planejei sair -> perdi a corrida
- a transicao e invalida e nada mudou -> isso e um defeito, e sobe

O mesmo vale para descoberta: o indice unico de `(workspace_id, provider,
external_key)` fez o que existe para fazer, e perder essa corrida virava
`IntegrityError` matando o tick de quem chegou segundo. Hoje e `AlreadyExists`,
que a store levanta -- a store e dona da restricao, entao e dona do conflito.

### Estado so muda por transicao

Salvar a linha de uma task grava metadados. **Nunca o estado.**

Gravava. Sob tres processos, um worker leu uma task, outro a avancou dois
estados, e o refresh de metadados do primeiro escreveu o estado ANTIGO de volta:
sem validacao, sem evento, sem rastro. A task ficou num estado ativo que ninguem
possuia e nada voltaria a pegar, e o unico sinal era um `updated_at` seis
segundos mais novo que a ultima transicao. Update perdido e dificil de ver
depois exatamente porque a escrita que perde e uma escrita perfeitamente comum.

### `database is locked`

O retry fica **antes** do bloco, na aquisicao do lock. Nada de dentro rodou
ainda, entao repetir equivale a ter comecado mais tarde -- nenhuma instrucao e
repetida e nenhuma semantica muda. Retry mais para dentro significaria reexecutar
instrucoes cujos efeitos quem chamou ja pode ter observado.

Aumentar o `busy_timeout` ate os testes passarem teria funcionado e deixado o
motor a um momento ocupado do mesmo crash.

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

**`authentication` pergunta pela autoridade, nao pelo ambiente.** Ja significou
"algum valor foi lido do ambiente quando este objeto foi construido" -- o que
respondia SIM para segredo que ninguem autorizou, e continuaria respondendo SIM
depois da revogacao. Hoje pergunta ao caminho governado: existe credencial viva,
neste escopo, com a capacidade `agent.run`?

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

## Entrega: o caminho depois do veredito

`TESTING` era onde a estrada acabava. A task chegava lá, o scheduler a pulava
por parecer ocupada, e todo tick seguinte lia limpo.

```
veredito do motor -> push -> PR_CREATED -> CI_RUNNING -> observado -> humano
```

### Cada etapa revalida

Policy, identidade e SHA são conferidos antes de **cada** mutação remota, não uma
vez no começo. Validação e mutação estão separadas no tempo, e o mundo anda no
meio: a branch move, o lease vence, alguém abre um PR na mesma branch.

### Cada etapa é idempotente, porque o remoto é a fonte

Um processo pode morrer entre mutar o remoto e gravar a linha que registra isso.
Depois disso os dois discordam, e o local discorda em silêncio — ele apenas diz
que nada aconteceu.

```
push entra no remoto  ->  [MORTE]  ->  linha nunca escrita
```

Supor que deu certo perde trabalho. Supor que falhou duplica. Os dois são
palpite, e um deles escreve. Então o motor **pergunta ao remoto** antes de
repetir qualquer coisa: a branch já está neste commit? existe PR com o marcador
desta run *e* com este head? Falha de leitura não é resposta — não saber não é
permissão para empurrar de novo.

### Onde a estrada para, e por quê

Num humano. Resultado de CI não é veredito de revisão, e veredito de revisão não
é resolução da task. Este motor não tem revisor nem autoridade de merge: a cor
do CI muda **o que a pessoa recebe**, nunca **quem decide**.

```
Agent Outcome != Validation != CI Result != Review Verdict != Task Resolution
```

O tick observa entregas em voo — e observar tem custo, então é limitado: depois
de N leituras sem conclusão a entrega vai para a fila do humano. Um pipeline que
nunca conclui é um resultado real, e ficar perguntando para sempre é como ele
fica invisível.

### Quem entrega, e quem não

Entrega quem validou a mudança e escreveu o commit. O tick não-atendido roda o
agente e registra o que ele alega; entregar dali seria entregar com base no
relato do agente sobre o próprio trabalho, que é a única coisa que este motor
existe para recusar.

## A janela: read model, API e Mission Control

```
             ┌───────────────┐
             │ Mission       │  nao decide nada
             │ Control       │
             └───────┬───────┘
                     │  HTTP, somente GET
             ┌───────▼───────┐
             │ API           │  aplica escopo, traduz para HTTP
             └───────┬───────┘
                     │
             ┌───────▼───────┐
             │ Read Model    │  le e traduz; nunca escreve
             └───────┬───────┘
                     │
             ┌───────▼───────┐
             │ Regente Core  │  decide
             └───────────────┘
```

Cada camada so consome a anterior. A tela nunca fala com SQLite, adapter,
provedor ou Core.

### A UI nao duplica decisao

Nao existe, em nenhum arquivo da tela ou da API, codigo que conclua que uma task
esta bloqueada, que um CI passou, que um lease venceu ou que um estado esta
preso. Se o motor sabe por que algo parou, a API expoe esse motivo e a tela
apresenta o fato.

Dois campos carregam a decisao pronta, e sao os que mais tentariam a duplicacao:

- **`state.owner`** — `engine`, `human`, `external` ou `nobody` — sai de
  `ENGINE_ADVANCES`, `AWAITING_EXTERNAL`, `TERMINAL` e `is_terminus`. E a
  pergunta que o operador faz antes de qualquer outra: *isto anda sozinho, ou
  esta esperando por mim?*
- **`ci_green`** exige `CONCLUDED` **e** resultado bom. `NO_CHECKS` e o caso
  perigoso: nada rodou, e a ausencia de vermelho parece verde para um `if not
  red`.

### Ausencia e escrita, nunca deixada em branco

Um campo vazio numa tela le-se como "nada de errado". Entao: sem PR, o campo diz
que nao houve PR; sem CI observado, diz `NOT_OBSERVED`; sem motivo gravado, diz
que nao foi gravado; sem evento, a linha do tempo diz que nao ha evento.

A linha do tempo mostra **somente eventos persistidos**. Se o fluxo parou depois
de "agente iniciado", e isso que aparece -- nao uma cadeia completa com etapas
que nunca aconteceram.

### Modelo externo separado do interno

A linha do banco e o payload sao coisas diferentes. Se a linha vazasse inteira,
renomear uma coluna quebraria quem consome, e um campo interno novo viraria
publico so por existir -- alem de convidar a tela a interpretar significado que
so faz sentido dentro do motor.

### Escopo antes de leitura

Toda rota escopada verifica o principal e existe o workspace **antes** de
qualquer chamada ao read model. Um `may_read` avaliado depois da leitura protege
o log, nao o dado.

"Nao existe" e "existe e nao e seu" respondem identico. Distinguir os dois
confirmaria a existencia de um workspace alheio a quem tentou adivinhar.

### Identidade, acesso e policy: tres evidencias diferentes

```
Autenticacao  quem e voce?             IdentityProvider, prova um segredo ou um fato
Identidade    qual chave estavel?      provedor + sujeito, nunca o nome
Acesso        voce recebeu concessao?  AccessGrant, com autor, data e revogacao
Policy        esta acao e permitida?   o arquivo de regras, independente
Transicao     este estado permite?     a maquina de estados
```

Ate o marco 13, acesso era um fato de **configuracao**: quem editava o arquivo
concedia a si mesmo autoridade de escrita, e nada guardava esse fato -- nem quem
concedeu, nem quando, nem como revogar. O proprio provedor de identidade
devolvia a autoridade, fundindo duas perguntas que precisam ficar separadas.

Agora nenhum provedor concede nada. Ele responde *quem e voce*; o
`AccessService` responde *o que voce recebeu*, lendo concessoes vivas **a cada
requisicao**. E por isso que revogar fecha a porta na chamada seguinte, sem
depender de a tela esconder um botao.

**O provedor faz parte da chave.** `provedor:sujeito`. Sem isso, duas fontes de
identidade que usem o mesmo sujeito viram a mesma pessoa dentro do motor.

**O sujeito e o identificador estavel.** SID, uid, `sub` -- nunca o nome de
exibicao: nomes mudam, e uma concessao amarrada a algo que muda se transfere
sozinha para quem herdar o nome.

**Revogar nao apaga.** "nunca teve acesso" e "teve e perdeu" sao fatos
diferentes para quem investiga.

**Papel e entrada da policy, nunca substituto dela.** Nao existe
`if principal.is_admin: allow()`: ter a capacidade e uma condicao, e a policy
continua sendo consultada depois. Um teste estrutural procura essa forma no
codigo-fonte.

**Ator e alvo sao colunas diferentes.** `alice concede a bob` e `bob concede a
bob` sao fatos diferentes; o segundo e recusado, e guardar so "quem foi afetado"
apagaria a pergunta que a auditoria de acesso existe para responder.

### Credencial e segredo sao coisas diferentes

```
Credential      quem pode usar o que, onde, ate quando   -- vive no motor
SecretRef       onde o material mora                     -- um endereco
SecretMaterial  o valor                                  -- nunca sobe
```

O dominio nao tem campo para guardar um token. Se um segredo couber num objeto
de `core/`, o desenho esta errado -- um banco vai para backup, para anexo de bug
e para captura de tela.

**A ordem, e nenhuma linha e pulavel:**

```
identidade -> acesso no workspace -> credencial deste escopo
           -> estado (revogada vence expirada) -> capacidade -> policy
           -----------------------------------------------------------
           so entao: resolve o material
```

Antes disto existia `adapter -> secret`: a composicao montava o adapter e o
valor era resolvido ali mesmo, antes de haver identidade, antes de a policy ser
consultada. Funcionava perfeitamente e nao passava por lugar nenhum. Esse
caminho foi **removido**, nao marcado como obsoleto -- ver a secao seguinte.

**Tres capacidades, nao uma.** `provider capability` e o que a ferramenta sabe
fazer; `credential capability` e o que ESTA credencial foi autorizada a fazer;
`policy authority` e o que a organizacao permite. Uma credencial de leitura nao
vale para push so porque o provider oferece push.

**`Status` nunca e coluna.** Expirar nao e um evento que alguem escreve, e o
tempo passando. Uma coluna criaria duas verdades, e a errada seria sempre ela.

**`UNKNOWN` nunca vira `ALLOW`.** Fonte de segredo indisponivel e uma recusa com
nome proprio -- distinta de expirada e de revogada, porque as tres mandam a
pessoa fazer coisas diferentes.

**Quatro fatos no teste de conexao.** `authorized` (o Regente), `reach` (o
provedor), `capability_supported` (a ferramenta) e `usable` (os tres juntos).
Autenticar nao e autorizar; indisponivel nao e recusado.

### Uma unica porta ate material secreto

Enquanto existir um segundo jeito de um adapter chegar ao segredo, o caminho
governado e uma recomendacao. O antigo foi **removido**, nao marcado como
obsoleto: nenhuma fabrica aceita um resolvedor de segredo, e o resolvedor nao e
construivel pelo nome.

```
adapter --> broker.material(use)
              --> identidade --> concessao --> escopo --> estado
              --> capacidade --> policy --> fonte --> material
```

O adapter recebe uma **porta**, ja presa a quem age e a que workspace. Diz para
que precisa da credencial e recebe material ou uma recusa com motivo. Nao
escolhe principal, nao escolhe escopo, nao resolve endereco e nao consulta
policy. Se recebesse o servico de credenciais teria `register` e `revoke` junto
-- e um adapter que registra credencial concede autoridade a si mesmo.

**Cada chamada refaz a autorizacao inteira.** Nao ha material guardado num
atributo esperando reuso, e e por isso que revogar fecha a porta na chamada
seguinte, sem reiniciar o motor.

**Construir nao e autorizar.** O agente recebe NOMES de variaveis e preenche os
valores no momento de rodar. Um objeto construido que ja carrega o segredo o
mantem em memoria pelo resto do processo, sem que ninguem tenha autorizado nada.

**O motor tambem e um principal.** Um tick roda de madrugada, sem ninguem
olhando, e a autoridade dele era implicita -- agia por ter sido construido.
Abrir uma excecao para o processo automatico seria a segunda autoridade de
volta, e a mais facil de justificar. Em vez disso ele tem identidade
(`engine:<workspace_id>`), concessao gravada e um papel de uma capacidade so.
Sem concessao, o motor nao usa credencial nenhuma.

### Como o material atravessa a fronteira do processo

Autorizar nao entrega. Uma fechadura numa porta que ninguem usa nao tranca
nada: enquanto a ferramenta se autenticava sozinha pelo chaveiro do sistema, o
broker podia recusar sem mudar o que acontecia no mundo.

```
broker.material(use) --> ambiente do filho --> subprocesso
```

**Ambiente, nunca argv.** `argv` e legivel por QUALQUER usuario da maquina --
`ps -ef`, o Gerenciador de Tarefas -- e nao ha permissao a pedir. O ambiente e
legivel pelo dono do processo. Ambiente e melhor, e nao e invisivel: a promessa
e "nao vaza para outro usuario, nem para log, estado, evento, excecao ou
remote", nunca "e inextraivel". Onde a ferramenta aceita stdin, stdin e melhor
ainda, e a sonda de credencial usa stdin por isso.

**Montado do vazio.** O ambiente do filho comeca em `{}` e recebe so o que foi
nomeado. Herdar era o que deixava um subprocesso encontrar credencial que
ninguem lhe deu -- inclusive de outro provedor.

**A ferramenta nao alcanca a propria configuracao.** Sem isso ela se autentica
por conta propria e o caminho governado vira decoracao. Se o diretorio de
isolamento voltar a conter a configuracao dela, `verify()` recusa em vez de
seguir funcionando.

**Uma porta para todo subprocesso.** O agente e os adapters de repositorio usam
o mesmo objeto: `material()` e chamado em um unico arquivo da camada de
adapters, e um teste estrutural recusa um segundo. Uma porta a mais e uma porta
que esquece do allowlist.

**`verify()` pergunta se a ferramenta RODA, nao se ela esta autenticada.** Eram
a mesma checagem e nao sao a mesma pergunta -- e a segunda exigiria credencial
de um comando de saude, que e o caminho mais curto para extrair material.

### Configuracao: o arquivo e a base, a tela sobrepoe

Uma pagina web nao reescreve um `regente.yaml` -- comentarios se perdem, edicoes
simultaneas se atropelam, e um erro de escrita deixa o motor sem subir. Um
arquivo de configuracao versionado tem dono, e nao e um processo HTTP.

```
regente.yaml  ->  base           banco  ->  sobreposicao
                     efetiva = base + sobreposicao
```

**A procedencia e obrigatoria.** Duas fontes sem ela produzem o pior modo de
falha possivel: alguem edita o arquivo, nada muda, e conclui que o Regente esta
quebrado. Toda leitura diz, campo a campo, de onde o valor veio -- e o caso de
conflito diz literalmente que editar o arquivo nao adianta enquanto a
sobreposicao existir.

**O que pode ser sobreposto e lista fechada.** Aceitar qualquer chave viraria um
segundo formato de configuracao, sem validacao e sem revisao, e o primeiro uso
seria sobrepor `policies` pela tela.

**Toda escrita e validada com o codigo que o motor usa para LER.** Uma segunda
validacao divergiria da leitura, e a que diverge aceita o que quebra depois --
longe de quem escreveu.

**Configurar nao e operar.** `workspace.settings.write` e capacidade propria:
quem pausa o motor numa emergencia nao precisa, por tabela, do direito de
aponta-lo para outro board.

### Intencao e execucao sao coisas diferentes

O motor sempre soube dizer onde cada task esta. Nunca soube dizer se ELE esta
trabalhando -- e sem isso a tela nao tinha o que mostrar nem o que controlar.

```
UI / CLI  --grava-->  intencao (RUNNING | PAUSED | STOPPED)
                           |
                     [identidade -> concessao -> capacidade -> policy -> auditoria]
                           |
`regente run`  --le a cada volta-->  obedece, e publica sinal de vida
```

**A tela nao inicia processo nenhum.** Um botao que subisse um processo daria a
uma pagina web o poder de criar processos na maquina de alguem -- a autoridade
paralela que os marcos 13 a 16 existiram para eliminar. Ela grava uma INTENCAO,
pelo mesmo caminho que a decisao humana ja percorria.

Isso da de graca a distincao que importa: `RUNNING` pedido sem sinal de vida
recente e **`DEGRADED`**, nunca `RUNNING`. "O servidor HTTP esta vivo" e "o motor
esta processando" sao fatos independentes, e a fase nunca os confunde.

**A fase e derivada, nunca gravada.** Uma coluna criaria duas verdades sobre a
mesma coisa, e a errada seria sempre a coluna -- a mesma decisao de `Status` de
credencial no marco 15.

**A intencao e relida a cada volta.** E o que faz `Pausar` ter efeito em segundos
sem matar processo, e `Parar` ser obedecido por um processo que ja rodava.

### Elegibilidade e prioridade nao sao a mesma pergunta

```
eligibility   esta task PODE ser pega?         booleana, um filtro
priority      entre as que podem, qual antes?  um numero, uma ordem
```

Colapsar as duas faria "despriorizar" virar "esconder", e e assim que trabalho
some de um board sem ninguem perceber. Uma task excluida por regra **continua
aparecendo**, como adiada, com o nome da regra.

A ordem final e a que sempre foi: `(prioridade, chave)`, menor primeiro, chave
como desempate estavel. As regras do workspace mudam o NUMERO, nunca o criterio.
O piso e negativo de proposito -- com piso em zero, uma task critica e uma comum
somariam o mesmo delta e encostariam empatadas, e a regra teria apagado a
informacao do board em vez de somar a ela.

**O board declara o que os status dele significam.** O mapeamento morava numa
constante do adapter; um board com outros nomes caia inteiro em `UNKNOWN`. Agora
a declaracao do workspace vence o mapa embutido sem substitui-lo -- e um status
que ninguem mapeou continua `UNKNOWN`, porque coagi-lo para o vizinho mais
conveniente faria o motor pegar trabalho que a equipe tirou da fila.

### O ambiente do `git`, classificado uma variavel por vez

Compor do vazio quebra o `git`: ele precisa de `PATH`, de `HOME`, de proxy.
Herdar tudo devolve o `credential.helper` global, que autentica em nome do motor
sem passar por lugar nenhum. A saida nao e escolher entre os dois -- e
classificar:

```
SAFE_FIXED        o que o Regente escolhe: config global e do sistema apontadas
                  para o nada, prompt desligado, askpass vazio
SAFE_ALLOWLISTED  o que vem do pai, por nome: PATH, HOME, TEMP, proxy
CREDENTIAL        uma variavel, e o material vem do broker
FORBIDDEN         autoridade ambiente: SSH_AUTH_SOCK, GIT_SSH_COMMAND, tokens
```

A quarta linha e a decisao desconfortavel. Um agente ssh autentica sem o Regente
saber, e mante-lo por conveniencia seria manter o segundo caminho de autoridade
com outro nome. Entao **um alvo `ssh://` e recusado**, com motivo: nao ha
mecanismo governado para chave ssh, e usar o agente de alguem e agir com
autoridade que ninguem concedeu.

O material entra por `http.<url>.extraheader`, escopado a origem do alvo -- sem
escopo, um redirecionamento entregaria o token a quem respondeu. Nao vai em
argv, nao altera o remote, nao vira arquivo e nao sobrevive ao processo.

**Codificado nao e protegido.** O cabecalho carrega o token em base64, e limpar
so o token deixaria passar o base64 que o contem. Os dois entram na lista do
que precisa sumir antes de virar registro.

### Uma escrita, e as mesmas barreiras

A tela ganhou exatamente uma acao: **decidir uma escalada**. Ela nao ganhou
autoridade -- passou a percorrer o caminho que o terminal ja percorria.

```
navegador                       terminal
    |                               |
    +--------- DecisionService -----+
                    |
    autenticacao  quem e voce?           `Principal.method` vazio = ninguem provou
    autorizacao   voce manda AQUI?       `decides` comeca vazio, sempre explicito
    escopo        a aprovacao e daqui?   lida JA escopada, nunca globalmente
    policy        e permitido?           autoridade independente; pode proibir
    estado        ainda esta aberta?     decisao e transicao, nao update de coluna
    transicao     a escolha foi ofertada?
    auditoria     quem, onde, o que, de que estado
```

Nao existe `ui_decide_approval`, e a razao nao e estilo: duas funcoes de decisao
divergem, e a que diverge e sempre a que tem menos verificacoes.

**A ordem importa.** O escopo e verificado ANTES de a aprovacao ser lida. Buscar
globalmente e conferir depois ja teria lido o dado de outro cliente -- e o codigo
continuaria parecendo certo, porque a resposta ao cliente seria a mesma.

**O cliente apresenta um segredo; nao declara um nome.** `principal`, `subject`,
`decided_by`, `actor`, `method` e `workspace_id` no corpo de um POST sao
recusados: identidade e escopo nao vem da requisicao.

**Ler nao concede decidir.** Derivar escrita de leitura faria de todo observador
um decisor -- que e precisamente o que a fila de escalada existe para nao ser.

Todo o resto continua sem porta: mergear, aprovar PR, empurrar, publicar,
disparar CI, alterar policy, orcamento ou segredo. A ausencia nao e lacuna a
preencher quando der.

Detalhes dos modelos, das rotas e do mecanismo de identidade em [API.md](API.md).

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
| API da UI | `http.server` da biblioteca padrão | a superfície é um punhado de GETs sem escrita; o que um framework traria não tem uso aqui, e `resolve()` puro deixa o teste de tenancy exercitar o código exato que o servidor roda |
| identidade da UI | porta `IdentityProvider`; `dev-token` em memória, loopback obrigatório | a fronteira é o que fica difícil de acrescentar depois; trocar por OIDC/SSO é implementar a porta, não reescrever rotas. O mecanismo se anuncia como de desenvolvimento porque um que não se anuncia cria a sensação de que há autenticação |
| escrita pela UI | uma só ação, pelo mesmo `DecisionService` do terminal | duas funções de decisão divergem, e a que diverge é sempre a que tem menos verificações |
| atualização da UI | polling de 5s | o motor não tem barramento de eventos ao vivo; tempo real sobre fonte que muda a cada tick é infraestrutura sem informação nova |

## O que ainda não existe

Deliberadamente: UI, adapters de fornecedor real, agente que escreve código,
LLMProvider implementado, detecção semântica de conflito, deploy. As **portas**
desses existem e são estáveis; as implementações vêm nos marcos 2–9 do
[ROADMAP](ROADMAP.md).

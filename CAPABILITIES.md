# Capabilities — what is proven, and by what

Four states, kept apart on purpose. Collapsing them is how a project convinces
itself it has done something it has not.

| State | What it means | What it does **not** mean |
|---|---|---|
| **IMPLEMENTED** | The code exists and imports. | That it has ever run. |
| **CONTRACT_TESTED** | Tests exercise it against fakes, fixtures or real local processes. Guards proven by mutation: break the guard, the test goes red. | That any real external service ever answered. |
| **EXERCISED_REAL** | It ran against the real external system, and the world was inspected afterwards. | — |
| **BLOCKED_EXTERNAL** | Ready, refusing to run, for a stated external reason. | That it is broken. |

A capability that is only CONTRACT_TESTED must never be cited as evidence that
the flow works. Fakes are obedient: every refusal in `tests/test_remote.py`
comes from the engine, never from a fake declining to cooperate — which makes
them good at proving guards and worthless at proving integration.

---

## Milestone 14 — identidade real e administracao de acesso

A pergunta do marco: **quem e a pessoa usando a Mission Control, como o Regente
sabe disso, e quem autorizou essa pessoa a agir neste workspace?**

```
IDENTIDADE REAL -> Principal -> concessao de uma autoridade -> workspace
                -> policy -> acao -> auditoria atribuivel
```

### O que o levantamento encontrou, antes de qualquer codigo

Registrado em `MILESTONE-14-RECON.md`. Nenhum provedor corporativo neste
ambiente: sem OIDC, sem SSO, sem dominio (`PartOfDomain: False`,
`AzureAdJoined: NO`). As unicas variaveis com cara de OAuth pertencem a sessao
interativa do Claude Code, que e proibido tocar desde o M7.

Existe **uma** identidade real: a conta do sistema operacional -- identificador
estavel (SID), emissor nomeado, autenticada pelo proprio sistema.

```
terminal   -> identidade real da conta do SO       -> EXERCITADO
navegador  -> nenhum provedor real disponivel      -> BLOCKED_REAL_IDENTITY
```

### Os dois defeitos que o levantamento revelou

**1. Acesso era um fato de configuracao, nao uma concessao atribuivel.**
`Principal.decides` -- a autoridade de escrita inteira -- era preenchido pelo
proprio provedor de identidade, a partir de campos que a composicao escrevia.
Quem editava o arquivo concedia a si mesmo autoridade, e nada guardava esse
fato: nem quem concedeu, nem quando, nem como revogar.

Pior: `IdentityProvider.principal()` fazia as duas coisas que precisam ficar
separadas. A docstring dele ja dizia o contrario do que o codigo fazia.

**2. A identidade do terminal era afirmada, nao provada.**
`getpass.getuser()` consulta `LOGNAME`, `USER`, `LNAME` e `USERNAME` **antes** do
sistema -- todas editaveis por quem roda o processo. O sujeito gravado na
auditoria era, na pratica, texto escolhido por quem decide: o mesmo defeito do
`--por` removido no M13, uma camada abaixo.

### O que mudou

| antes | agora |
|---|---|
| `decides` vindo do provedor | `abilities` vindas de `AccessGrant` gravado |
| sem registro de quem concedeu | `granted_by`, `granted_at` em cada concessao |
| sem revogacao | `revoked_by`, `revoked_at`; a linha **nao** e apagada |
| `getpass.getuser()` | SID/uid do sistema, via chamada direta a biblioteca |
| sujeito sem provedor | chave `provedor:sujeito` |

**Revogar nao apaga.** "nunca teve acesso" e "teve e perdeu" sao fatos
diferentes para quem investiga.

**O provedor faz parte da chave.** Sem isso, `walberth` numa conta de sistema e
`walberth` num diretorio corporativo seriam a mesma pessoa dentro do motor, e a
concessao de uma valeria para a outra.

### Exercitado de verdade

Workspace real, identidade real da maquina, escalada gerada pelo proprio motor
depois de duas execucoes que falharam de verdade:

```
$ regente access quem-sou-eu
identidade : os-account:S-1-5-21-3131206616-3848205496-107930634-1001
emissor    : DESKTOP-09OD9Q8
pode aqui  : nada                     <- autenticado, e sem autoridade nenhuma

$ regente access conceder dev-token:squad-tech --papel operator
NOT_FOUND: recurso nao encontrado neste escopo    <- sem concessao, sem poder

$ regente access inicial                          <- a porta estreita, uma vez
acesso concedido a os-account:S-1-5-21-...

$ regente access inicial
CONFLICT: este workspace ja tem concessoes        <- e ela fecha

$ regente access conceder os-account:S-1-5-21-... --papel owner
FORBIDDEN: conceder acesso a si mesmo nao e concessao

$ regente access conceder dev-token:squad-tech --papel operator
acesso concedido a dev-token:squad-tech
  ator  : os-account:S-1-5-21-...      <- duas pessoas na linha
  alvo  : dev-token:squad-tech

$ regente decide apv_2874a41ee70c investigar
SB-1: registrado 'investigar' por os-account:S-1-5-21-...

$ regente access revogar dev-token:squad-tech
acesso de dev-token:squad-tech revogado
```

E na Mission Control, com a identidade revogada, a pagina de acesso responde
`404 — recurso nao encontrado neste escopo`. Depois de uma nova concessao pelo
terminal, a mesma pagina mostra a historia inteira: quem tem, com o que, quem
concedeu, quando, e o que foi revogado por quem.

### Contraprovas

- identidade valida sem concessao -> negado;
- concessao de outro workspace -> negado; de outro cliente -> negado;
- concessao revogada -> negado, **inclusive com escalada aberta**;
- dois provedores com o mesmo sujeito -> duas pessoas;
- mesmo nome de exibicao -> duas pessoas;
- conceder a si mesmo -> recusado, mesmo com autoridade para conceder;
- sem `workspace.access.grant` -> recusado;
- papel desconhecido -> concede **nada**, nunca tudo;
- policy `DENY` sobre quem tem a capacidade -> recusado;
- policy ausente -> recusado (o default DENY vale para humano tambem);
- regra sobre acao parecida (`workspace.write`) -> nao governa esta;
- revogar B nao afeta A; conceder a B nao cria acesso noutro workspace;
- workspace alheio e workspace inexistente -> **mesma resposta, mesmo texto**;
- linha forjada direto no banco concede a capacidade e **nao** pula a policy;
- remover o provedor de identidade nao cai para `dev-token`: cai para anonimo.

### Estado das capacidades

| Capacidade | Estado | Evidencia |
|---|---|---|
| Identidade real com identificador estavel e emissor | **EXERCISED_REAL** | SID da conta, terminal |
| Concessao persistida com autor e data | **EXERCISED_REAL** | workspace de sandbox real |
| Bootstrap uma vez, e depois fechado | **EXERCISED_REAL** | segunda tentativa recusada |
| Revogacao com historia preservada | **EXERCISED_REAL** | listagem antes e depois |
| Revogado nao decide | CONTRACT_TESTED | escalada aberta, decisao recusada |
| Provedor nao concede autoridade | CONTRACT_TESTED | auditoria AST + teste por provedor |
| Substituicao de provedor sem tocar core/ports/engine | CONTRACT_TESTED | provedor inventado no teste |
| Isolamento entre workspaces e clientes | CONTRACT_TESTED | ids trocados de proposito |
| Auditoria separa ator de alvo | CONTRACT_TESTED | os dois campos, comparados |
| Exatamente uma concessao sob concorrencia | CONTRACT_TESTED | dois processos reais |
| **Identidade real no navegador** | **BLOCKED_REAL_IDENTITY** | nenhum provedor disponivel |

### Limitacoes

**Nao ha identidade real para o navegador.** A conta do sistema autentica o
terminal, onde o processo E a conta. Uma requisicao HTTP nao carrega essa conta,
e faze-la carregar exigiria autenticacao integrada -- que este ambiente nao tem.
O `dev-token` continua sendo o unico mecanismo da tela, continua se anunciando
como de desenvolvimento, e continua recusando escutar fora do loopback.

**Nao ha fallback, e isso e proposital.** Sem provedor, o principal e anonimo --
nunca o `dev-token`. Um fallback de autoridade e o modo mais silencioso de perder
uma fronteira.

**A concessao inicial e uma porta estreita, nao uma ausencia de porta.** Ela
exige identidade do sistema operacional, so funciona num workspace sem nenhuma
concessao, e fica registrada com um verbo proprio. Quem roda o processo ja
controla o arquivo do banco; o bootstrap nao concede nada que essa pessoa nao
pudesse escrever a mao -- mas deixa registro, que e a diferenca.

**Escrever direto no banco continua concedendo capacidade.** Nao ha defesa
contra isso num SQLite local, e o marco nao finge que ha. O que foi provado e
que uma linha forjada **nao pula a policy**.

**Papeis sao tres, fixos.** `operator`, `admin`, `owner`. Nao ha administracao
de papeis, nem grupos, nem herencia -- e nao havera enquanto nao houver uma
pergunta real que exija isso.

---

## Milestone 13 — a primeira escrita humana

Uma acao, e uma so: **decidir uma escalada**. O objetivo nunca foi dar poderes ao
navegador. Foi provar que ele pode participar sem alterar a arquitetura de
autoridade que ja existia.

```
navegador                          terminal
    |                                  |
    +---------- as MESMAS -------------+
                barreiras
                    |
    autenticacao  quem e voce?
    autorizacao   voce manda NESTE workspace?
    escopo        a aprovacao e deste workspace?
    policy        esta acao e permitida aqui?
    estado        esta aprovacao ainda esta aberta?
    transicao     a escolha esta entre as oferecidas?
    auditoria     quem, onde, o que, a partir de que estado
```

Nao existe `ui_decide_approval`. Existe `DecisionService`, e os dois passam por
ele. A razao nao e estilo: duas funcoes de decisao divergem, e a que diverge e
sempre a que tem menos verificacoes.

### O que separa autenticado de afirmado

`Principal.method`. Um principal sem ele nao foi provado por ninguem -- foi
declarado. E a diferenca entre "o servidor verificou um segredo que ele proprio
emitiu" e "o navegador digitou um nome".

O corpo do POST tambem nao pode dizer quem esta decidindo: `principal`,
`subject`, `decided_by`, `per`, `actor`, `method` e `workspace_id` sao recusados
com `400`. Identidade e escopo nao vem do corpo da requisicao.

Ler e decidir sao concessoes **separadas**, com defaults opostos: `workspaces`
pode ser aberto, `decides` comeca vazio. Derivar escrita de leitura faria de
todo observador um decisor -- que e precisamente o que a fila de escalada existe
para nao ser.

### Exercicio real

A escalada **nao foi fabricada**. Um workspace de sandbox real rodou ticks
reais; o agente falhou duas vezes de verdade; a escada de recuperacao do motor
se esgotou e ELE escalou:

```
SB-1 · risco LOW
2 tentativas falharam. Ultima: no outcome declared
a escada de recuperacao acabou; sem decisao sua a task nao sai do lugar
```

A decisao foi tomada **no navegador**, clicando a opcao que o motor recomendou.
A trilha que ficou:

```
decisao_humana_autenticada   dev-token:walberth   decidiu 'investigar' em SB-1
decisao_humana               dev-token:walberth   escolheu 'investigar'
```

E o tick seguinte agiu sobre ela: `1 despachada(s), 1 liberada(s)`.

Antes disso, um POST sem credencial na mesma URL -- o que um site aberto noutra
aba conseguiria montar -- respondeu `401`.

### Defeitos encontrados

| # | Defeito | Por que estava invisivel |
|---|---|---|
| 1 | **`regente decide` estava quebrado.** O parser recebia `opcao` e `--por`; o handler lia `args.option` e `args.per`. `AttributeError` antes de tocar no store. O unico caminho humano do sistema, inutil desde a renomeacao para ingles. | Todo teste chamava `store.decide_approval` diretamente. A fiacao de argumentos do CLI nao tinha teste nenhum -- e e ali que uma renomeacao deixa restos. |
| 2 | **A conexao SQLite era compartilhada entre threads sem trava.** `check_same_thread=False` desliga a checagem e nao poe nada no lugar. | Enquanto a segunda thread era so o batimento de lease, quase nao havia contencao. O navegador dispara tres leituras em paralelo a cada cinco segundos: a **mesma URL** passou a responder 200, depois 404, depois resposta vazia. Um 404 intermitente e a pior forma disto -- parece dado que sumiu. Encontrado olhando o log de rede do browser real. |
| 3 | **`Effect` parecia um enum e nao era.** `class Effect(str)` com atributos de classe. `decision.effect == Effect.DENY` respondia certo; `decision.effect is Effect.DENY` respondia **sempre False**, sem erro e sem aviso. Custou uma verificacao de policy que simplesmente nao acontecia. | Todo o codigo existente usava `==`. O primeiro `is` do projeto foi o meu, e so apareceu porque um teste de DENY passou quando deveria falhar. |
| 4 | **A policy do repositorio proibia decisao humana.** Sem regra para `approval.decide`, o default DENY valia: com o CLI consertado, a fila de escalada nao podia ser respondida por ninguem. | Enquanto o comando estava quebrado, ninguem chegava a policy. |
| 5 | **O token de sessao era gravado num arquivo que nada lia.** Sobreviveu a um `kill` -- o `finally` que o apagaria nao roda -- e ficou para tras sem nunca ter servido. | So aparece quando o processo morre sem encerrar; e um segredo escrito, nunca consultado. |
| 6 | **`--por` deixava quem decide escolher o proprio nome na auditoria.** | Ninguem nota que uma assinatura e digitada ate precisar confiar nela. |

O defeito 2 e o mais grave e nao tem nada a ver com escrita humana: ele afetava
toda leitura da Mission Control desde o marco anterior.

### Contraprovas

Cada barreira atacada sozinha, com as outras satisfeitas:

- principal sem `method`, com todas as concessoes → `UNAUTHENTICATED`;
- token errado → nao e identidade parcial: e anonimo;
- quem le o workspace mas nao recebeu `decides` → recusado;
- id de aprovacao de outro cliente, dentro do meu proprio workspace → recusado;
- workspace inexistente e workspace alheio → **mesma resposta, mesmo texto**;
- a recusa nao contem o id, o nome nem a chave do outro tenant;
- policy `DENY` para um operador ja autorizado → recusado;
- policy vazia → recusado (default DENY vale para humano tambem);
- regra sobre `deploy.*` nao governa `approval.decide`;
- `HUMAN_APPROVAL` nao pede aprovacao humana para uma aprovacao humana;
- segunda decisao → `CONFLICT`, e a primeira permanece;
- escolha fora das opcoes oferecidas → `INVALID_STATE`;
- dois **processos** decidindo ao mesmo tempo → exatamente uma aceita, uma `CONFLICT`;
- nota com texto tipo credencial → nunca persistida literal (so o comprimento);
- decisao recusada → nenhuma auditoria de decisao;
- `POST` em qualquer outra rota → `405 read_only`.

Estruturalmente, verificado no proprio codigo-fonte: a API nao escreve no store,
`decide_approval` so e chamado do Core, a tela nao contem logica de autorizacao,
nao alcanca o store, e o modulo da API nao importa `sqlite3` nem adapter.

### Sweep de mutacao

26 mutacoes sobre as barreiras. Nenhuma quebra o caminho feliz -- remover a
autenticacao deixa o operador local decidindo normalmente; aceitar a segunda
decisao produz um 200 satisfatorio.

### Estado das capacidades

| Capacidade | Estado | Evidencia |
|---|---|---|
| Decisao humana pelo navegador, ponta a ponta | **EXERCISED_REAL** | escalada gerada pelo motor, clique real, trilha real, tick agiu |
| Identidade autenticada antes da escrita | **EXERCISED_REAL** | `401` para POST sem credencial no servidor real |
| Terminal e navegador pelo mesmo caminho | **EXERCISED_REAL** | `regente decide` e `POST` chamam `DecisionService` |
| Auditoria atribuivel | **EXERCISED_REAL** | `dev-token:walberth`, com estado anterior e posterior |
| Tenancy na escrita | CONTRACT_TESTED | id valido do tenant errado, por HTTP e pelo Core |
| Policy como autoridade independente | CONTRACT_TESTED | `DENY`, vazia, acao vizinha, `HUMAN_APPROVAL` |
| Exatamente uma decisao sob concorrencia | CONTRACT_TESTED | dois processos reais |
| Leitura sob concorrencia | CONTRACT_TESTED | 60 leituras paralelas, 8 threads |
| Erros distintos e sem vazamento | CONTRACT_TESTED | 401/403/404/409/422 |
| Segredo de sessao fora do disco | CONTRACT_TESTED | varredura do diretorio |
| **Identidade real (OIDC/SSO)** | **BLOCKED** | nao ha provedor; ver limitacoes |

### Limitacoes

**Nao ha autenticacao para valer.** O `dev-token` prova que quem chama e quem
rodou `regente ui` nesta maquina, e nada mais: sem usuarios, sem expiracao, sem
revogacao, sem segundo fator, e vale para quem o tiver. Ele se anuncia -- na
tela, no `describe()` e em `/api/health` -- porque um mecanismo de
desenvolvimento indistinguivel de um real cria a sensacao de que ha
autenticacao.

**O servidor recusa escutar fora do loopback** com esse mecanismo, e o provedor
recusa autenticar mesmo se alguem forcar o bind. Nao e conselho no README; e uma
recusa no codigo.

**A concessao e por processo, nao por pessoa.** `regente ui` concede o workspace
da configuracao que abriu. Nao ha papeis, grupos nem administracao de acesso, e
`--read-only` e o unico controle: ele remove a autoridade de escrita sem remover
a identidade.

**Uma acao so.** Mergear, aprovar PR, disparar CI, publicar, alterar policy,
orcamento ou segredo continuam sem porta -- e a ausencia nao e lacuna a preencher
quando der.

---

## Milestone 12 — Mission Control, Read Model e API

O motor tinha estado operacional e nenhuma superficie de operacao humana. Este
marco constroi a janela.

```
Store / Engine  ->  Read Model  ->  API  ->  Mission Control
```

Cada camada so consome a anterior. A tela nunca fala com SQLite, adapter,
provedor ou Core; a API nunca decide nada sobre o trabalho; o read model nunca
escreve.

### A regra que define a camada

**A UI nao duplica a decisao do motor.** Nao existe, em nenhum arquivo da tela
ou da API, codigo que conclua que uma task esta bloqueada, que um CI passou, que
um lease venceu ou que um estado esta preso. Toda conclusao chega pronta de quem
tem autoridade para calcula-la.

Dois campos carregam isso explicitamente:

| campo | por que viaja pronto |
|---|---|
| `state.owner` | `engine` / `human` / `external` / `nobody` sai de `ENGINE_ADVANCES`, `AWAITING_EXTERNAL`, `TERMINAL` e `is_terminus`. Uma tela que deduzisse isso erraria em silencio na primeira mudanca da maquina de estados. |
| `ci_green` | exige `CONCLUDED` **e** resultado bom. `NO_CHECKS` e o caso perigoso: nada rodou, e a ausencia de vermelho parece verde para qualquer `if not red`. |

### Ausencia nunca vira sucesso

Provado com a Mission Control aberta sobre o workspace real, que esta bloqueado:

```
ALVO           nenhum alvo resolvido para esta task
ENTREGA        nada foi entregue por esta task
LINHA DO TEMPO nenhum evento gravado para isto
MOTIVO         nenhum motivo registrado
```

Nenhum desses campos fica em branco. Um campo vazio numa tela le-se como "nada
de errado".

### O que a tela mostra sobre um sistema bloqueado

Sobre o banco real de `sombra-sg`, sem nenhum ajuste:

```
STUCK · sg · cliente squad-tech · ultimo tick desconhecido

35 BLOCKED   57 READY   8 TESTING
0 runs ativos   0 leases vivos   0 entregas em voo   0 precisam de voce

SEM ROTA DE SAIDA
SG-1185  TESTING  nobody  este motor nao tem etapa que avance daqui
```

E na task:

```
TESTING
a mudanca existe e esta sendo verificada pelo motor
MOTIVO         nenhum motivo registrado
HA             1d00h
QUEM MOVE      nobody
PROXIMO PASSO  este motor nao tem etapa que avance daqui; precisa de uma pessoa

BLOQUEIOS
NO_ROUTE  TESTING nao tem etapa de saida neste motor
          -> precisa de uma pessoa
```

O painel nao fica verde. A saude diz `STUCK`, lista as oito tasks pelo nome e
mantem dois sinais em `UNKNOWN` -- porque nao examinado nao e o mesmo que
examinado e limpo.

### Quatro categorias que nao se misturam

Confundi-las faz a fila humana mentir. O agrupamento e por **dono do proximo
passo**, nao por aparencia:

| categoria | quem move | exemplo |
|---|---|---|
| precisa de voce | `human` | uma decisao na fila |
| bloqueado | — | algo impede, e nao e voce |
| sem rota | `nobody` | o motor nao tem etapa que avance daqui |
| aguardando | `external` | o CI ainda nao respondeu |

### Defeitos encontrados

| # | Defeito | Por que estava invisivel |
|---|---|---|
| 1 | **`runs.task_id` carregava dois significados.** O orquestrador gravava o id da linha; o caminho de missao avulsa gravava a chave do fornecedor. `task_runs` casa por id, entao metade dos runs ficava invisivel a partir da propria task -- sem erro, sem log, com a tela dizendo "nenhuma execucao". Pior: as guardas de recuperacao do M11 comparam `task.id` contra `run.task_id`, entao um run do caminho avulso nao seria reconhecido como vivo. | Nenhuma superficie precisava ligar as duas pontas. O terminal ja sabe qual task abriu. |
| 2 | **O cliente nao tinha nome guardado.** A hierarquia sempre foi Organizacao -> Cliente -> Workspace, mas so o workspace tinha linha; o nome do cliente vivia no arquivo de configuracao, fora do alcance de qualquer leitura. A primeira tela que lista varios clientes mostrou `cli_aeaa33dd838c`. | O terminal sabe qual configuracao abriu, entao nunca precisou perguntar. |
| 3 | **A saude da tela era estruturalmente menos informada que a do terminal.** O read model nao recebia os tetos de orcamento, entao a pergunta de orcamento respondia `UNKNOWN` na tela e um numero no terminal. A mesma pergunta, duas respostas, e a pior era a que o operador olha. | So aparece com as duas superficies lado a lado. |
| 4 | **`501` generico para metodo de escrita.** A recusa honesta -- "esta API e somente leitura" -- existia no roteador e nunca chegava ao HTTP, porque a biblioteca padrao responde antes. "Metodo nao suportado" le-se como "ainda nao implementado", e alguem acabaria implementando. | O teste de roteamento passava; so o teste que sobe o servidor de verdade viu. |

O defeito 1 e o mais grave e nao tem nada a ver com UI. Foi encontrado porque um
read model e a primeira coisa que precisa juntar task e run pelos dois lados.

### Contraprovas

- Um id valido do tenant errado le como ausente -- task, run e delivery.
- "Nao existe" e "existe e nao e seu" respondem identico: mesmo status, mesmo corpo. Distinguir confirmaria a existencia de um workspace alheio.
- Duas chaves `SAME-1` iguais, mesmo repositorio e mesma branch nos dois clientes continuam duas coisas.
- Filtro de estado inexistente devolve vazio, nunca o board inteiro.
- Um workspace inexistente nao empresta as tasks do primeiro que houver.
- `POST`/`PUT`/`PATCH`/`DELETE` respondem `405 read_only`, com motivo, tambem por HTTP.
- Uma auditoria AST prova que a API nao chama nenhum metodo de escrita do store.
- Uma auditoria AST prova que o read model nao faz nenhuma leitura sem tenant (38 chamadas conferidas).
- A saude da tela e byte a byte a do motor, incluindo os tetos.
- Um cliente sem nome gravado aparece como "sem nome gravado", nunca como o proprio id.

### Sweep de mutacao

31 mutacoes, todas da categoria que **nao quebra nada**: a pagina abre, o painel
fica verde, os campos ficam preenchidos, e a resposta esta errada.

```
capturadas   30
nao provada   1   (so alcancavel por symlink; este sistema nao permite criar um)
```

Quatro escaparam na primeira passada. Uma era mutacao mal escolhida -- trocava o
objeto de workspace sem trocar a consulta, e portanto nao mudava resposta
nenhuma. As outras tres eram lacunas reais, e uma delas revelou um teste que
**passava pela guarda errada**: a task chegava a `BLOCKED` por transicao, e
transicao grava evento com resumo, entao a fallback de "nenhum motivo registrado"
nunca era exercitada. Uma task que ja nasce bloqueada exercita.

A mutacao nao provada e a segunda guarda dos estaticos: com a checagem de
segmentos suspeitos no lugar, a conferencia do caminho resolvido so e alcancavel
por um link dentro da pasta apontando para fora. O teste existe e e pulado aqui
por falta de privilegio no Windows.

### Estado das capacidades

| Capacidade | Estado | Evidencia |
|---|---|---|
| Read model como camada explicita | **EXERCISED_REAL** | aberto sobre o banco real de `sombra-sg` |
| Mission Control respondendo as nove perguntas | **EXERCISED_REAL** | navegado no browser sobre dados reais |
| Estado com nome, significado, motivo, idade e dono | **EXERCISED_REAL** | task real `SG-1185` |
| Saude reexposta, nunca recalculada | CONTRACT_TESTED | comparada sinal a sinal com o motor |
| Escopo de tenancy em cada rota | CONTRACT_TESTED | 8 rotas x id valido do tenant errado |
| API somente leitura | CONTRACT_TESTED | 4 metodos + auditoria AST + HTTP real |
| Ausencia nunca vira sucesso | CONTRACT_TESTED | PR, CI, evento, motivo, alvo |
| `UNKNOWN` nunca vira saudavel | CONTRACT_TESTED | sweep + teste direto |
| Estatico sem leitura arbitraria de disco | CONTRACT_TESTED | travessia; symlink **nao provado aqui** |
| Identidade da API | **IMPLEMENTED** | `Principal` com escopo; **sem autenticacao** |
| Atualizacao ao vivo | CONTRACT_TESTED | polling de 5s, erro visivel, sem estado inventado |

### Limitacoes

**Nao ha autenticacao.** Existe um `Principal` com escopo de workspaces e nada
que prove quem e o portador. Por isso o servidor so escuta em loopback por
padrao. Expor na rede sem antes ligar uma identidade real entrega o estado de
todos os clientes visiveis a quem alcancar a porta. A fronteira existe vazia
porque e o que fica dificil de acrescentar depois.

**A tela e somente leitura, e isso e por decisao.** Decidir uma escalada
continua sendo `regente decide`, no terminal. A tela mostra a fila e o briefing;
nao decide.

**Um cliente so, um banco so, nesta execucao real.** A prova multi-tenant e de
contrato: dois clientes com nomes locais identicos no mesmo banco. O que nao foi
exercitado e a Mission Control sobre dois clientes reais simultaneos.

**Polling, nao tempo real.** O motor nao tem barramento de eventos ao vivo, e um
transporte em tempo real sobre uma fonte que so muda a cada tick seria
infraestrutura sem informacao nova.

---

## Milestone 11 — closing the cycle

The question: can the Regente actually deliver? Not "do the tests pass", but
does a task go from the board to a place where a person can act on it.

### The real-world path: `BLOCKED_BY_REAL_WORLD_INPUT`

The search came first, before any code was written. It is recorded in
`MILESTONE-11-CANDIDATES.md` and its verdict is unchanged: **no eligible task
exists**, and **no agent is authenticated**. Two independent gates.

```
eligible task    NO   20 concrete unassigned items; none has a target
                      resolvable by evidence rather than by inference
agent            NO   BLOCKED_AUTHENTICATION, from `regente doctor`
```

No task was invented, none was edited into eligibility, and no credential was
taken from the interactive session. A milestone that produced a green report by
loosening any of those would have measured the loosening, not the engine.

### What was built instead

`TESTING` was a dead end. Milestone 8 found it — every successful task parked
there, the scheduler skipped it because it looked busy, and every later tick
read clean. Milestone 8 made the engine *say so*. Milestone 11 is the road.

```
verdict -> push -> PR_CREATED -> CI_RUNNING -> observed -> a person
```

Two properties matter more than the sequence.

**Every stage revalidates.** Policy, identity and SHA are checked before each
remote mutation, not once at the start. Validation and mutation are separated in
time, and the world moves in between.

**Every stage is idempotent.** Before repeating anything, the engine asks the
remote what already exists. A process that dies between a remote mutation and
the row recording it leaves the two disagreeing: local says nothing happened,
the remote says something did. Assuming success loses work; assuming failure
duplicates it. Both are guesses, and one of them writes.

### Where it stops, and why that is the point

A conclusive CI result escalates to a person. It does not approve, merge, deploy
or resolve anything. This engine has no reviewer and no merge authority, so the
colour of the checks changes **what the human is told**, never **who decides**:

| CI | What the engine does | Recommendation |
|---|---|---|
| green | escalates with the pull request and the checks | block — a person reviews and merges |
| red | escalates with the failures | investigate — the change needs work |
| unavailable, N times | escalates | block — nothing is wrong with the change |
| never concludes, N times | escalates | block — a pipeline that never answers is a real outcome |

`AI_REVIEW` remains unimplemented. Its owner would be a reviewer agent, which
this milestone forbids adding. An unreachable state is honest; a state entered
with nobody in it is not.

### Defects found

| # | Defect | Why it was invisible |
|---|---|---|
| 1 | **A kill between "the run ended" and "the task moved" stranded the task for ever.** `_collect` releases the leases, saves the run as finished, then transitions. A process killed in that window leaves a task in an owned active state with no lease to expire and no active run to match. Recovery proves a worker died *by its expired lease*, so it finds nothing; the scheduler skips active states. The task is lost and every report stays clean. | A few milliseconds wide. Survived a 1000-tick soak, 40 contention rounds and the whole M10 suite; surfaced on one round out of three of the multi-tenant kill harness. |
| 2 | **The push target was recorded exactly as `git` reports it.** An HTTPS remote can carry the credential inside the URL, and that string was written to the delivery ledger and into refusal messages. The database outlives the process: backups, bug reports, screenshots. | Every test used a local clone, whose remote URL has no credential in it. The defect cannot appear without a real tokenised remote. |
| 3 | **A resumed delivery walked the task backwards.** Tolerating "already there" is not enough: on a resume the task is *further along*, and the stage tried to move it back to `PR_CREATED` from `CI_RUNNING`. | Only a second pass over the same delivery reaches it, which is exactly what the first idempotency test did. |

Defect 1 is the milestone's most valuable finding and has nothing to do with
delivery. It was found because closing the cycle meant asking, for the first
time, what a task is *waiting for* — which forced apart two things the engine
had been treating as one: a state with a worker inside it, and a state waiting
on somebody else's system.

```
OWNED_ACTIVE        ASSIGNED, IMPLEMENTING, TESTING, MERGING, DEPLOYING
                    a worker is inside; no active run means the task is orphaned
AWAITING_EXTERNAL   PR_CREATED, CI_RUNNING, AI_REVIEW
                    nobody is inside by definition; what must exist is a
                    delivery row to come back to
```

### Capability status

| Capability | State | Evidence |
|---|---|---|
| `TESTING` is no longer a dead end | CONTRACT_TESTED | the stage drives push → PR → CI against fakes |
| Every stage revalidates policy, identity and SHA | CONTRACT_TESTED | mutation sweep: removing any check goes red |
| Push is idempotent against the remote's own answer | CONTRACT_TESTED | resume, restart, and run-it-twice tests |
| Pull request creation is idempotent | CONTRACT_TESTED | this run's PR adopted; anyone else's refused |
| A remote that cannot be read stops the delivery | CONTRACT_TESTED | read failure and absent-capability both refuse |
| Delivery ledger answers task → run → commit → push → PR → CI | CONTRACT_TESTED | every identity column asserted non-empty |
| A task moved by a person mid-cycle is not dragged back | CONTRACT_TESTED | the PR is kept and recorded; the task is left alone |
| CI is asked about the commit, never the pull request | CONTRACT_TESTED | a PR's head moves; a SHA cannot |
| `UNKNOWN` / `UNAVAILABLE` / no checks never become success | CONTRACT_TESTED | each recorded as its own state, none advances |
| The tick returns to deliveries in flight | CONTRACT_TESTED | observation count persisted and bounded |
| A stranded task is rescued; a live one is never touched | CONTRACT_TESTED | deterministic reproduction of the kill window |
| No credential reaches the database | CONTRACT_TESTED | every row of every table read back and scanned |
| Delivery under two tenants with identical names | CONTRACT_TESTED | same task key, repo, branch and PR number stay distinct |
| **A real pull request against a real remote** | **BLOCKED_EXTERNAL** | no eligible task; no authenticated agent |
| Task resolution written back to the origin | NOT IMPLEMENTED | deliberately: the engine does not resolve tasks |

### Limitations

**The tick cannot deliver.** The road out of `TESTING` runs in `MissionRunner`,
which is the path that validates a change and writes the commit. The unattended
orchestrator has neither: it runs an agent and records what it claims. Wiring
delivery there would mean delivering on an agent's own account of its work,
which is the one thing this engine exists to refuse. The tick *watches*
deliveries already in flight; it does not start them.

**No fake is evidence of integration.** Everything above is CONTRACT_TESTED. The
fakes are deliberately obedient, so every refusal comes from the engine — good
for proving guards, worthless for proving that a real provider behaves as
assumed.

---

## Milestone 10 — multi-client isolation

The spine, tested: `Organization -> Client -> Workspace`, with two complete
client contexts coexisting in one engine and one database.

### The shape of the proof

Both clients were given **the same local identifiers on purpose**. The weak
version of this test gives them different names and shows they differ, which
proves the names differ.

| | Client-A | Client-B |
|---|---|---|
| workspace name | `main` | `main` |
| task key | `TASK-1` | `TASK-1` |
| repository | `worker` | `worker` |
| resource | `repo:database` | `repo:database` |
| branch | `feature/test` | `feature/test` |
| policy | `repo.push` DENIED | `repo.push` ALLOWED |
| daily budget | 2 dispatches | 50 dispatches |
| secret | `env:SECRET_A` | `env:SECRET_B` |

One SQLite file, because separate databases would prove nothing: the question is
whether the boundary holds with the rows side by side.

### Results

| Measure | Result |
|---|---|
| cross-client task access | **0** |
| cross-client secret access | **0** |
| cross-client lease collision | **0** |
| cross-client policy leakage | **0** |
| cross-client budget leakage | **0** |
| cross-client repository leak | **0** |
| cross-client state mutation | **0** |
| cross-client observability | **0** |
| shared task rows | **0** |
| invariant violations | **0** |
| worker crashes | **0** |

Real processes on both sides, interleaved, with `SIGKILL` on each side while the
other worked.

### Defects found

| # | Defect | Why one client never showed it |
|---|---|---|
| 1 | **`store.transition` had no tenant scope.** Reads were scoped, writes were not -- ten lines apart. A client could not *see* another's task and could still *move* it, given the id. | With one client there is no other id to hold. |
| 2 | **`renew_lease` and `release_lease` fell back to a global match** when no workspace was passed, with a comment arguing it was safe because run ids are unique. | The fallback was never taken; every production caller passed one. |
| 3 | **`busy_timeout` was set after `journal_mode=WAL`**, which takes a lock. A process opening the database while another was writing raised `database is locked` from its own constructor. | One process never contends with itself at startup. |
| 4 | **Losing possession stranded the task.** Abandoning released the leases and ended the run, so recovery -- which matches expired leases against active runs -- had nothing left to match. | Needs a mission that outlives its lease with nobody waiting; a 1000-tick soak and 40 contention rounds never produced it. |

Defects 3 and 4 are not tenancy defects. They surfaced here because two clients
mean twice the processes and twice the starts.

### Mutation sweep

Nineteen mutations aimed at tenancy -- the category that works perfectly with one
client and leaks with two. **Fourteen caught immediately. Five escaped, and each
exposed a guard nothing was exercising:**

| Mutation | What was missing |
|---|---|
| Derive the workspace id without the client | the tenancy tests computed ids in the harness, so the real composition root was never checked |
| Derive the client id without the organization | same |
| Move any task regardless of workspace | the orchestrator refuses earlier, so the store's own guard was unexercised |
| Claim into the caller's workspace instead of the run's | my mutation was a no-op; the real one needed writing |
| Transition without naming the tenant | `_moved_by_another` never reaches the store with a foreign id |

All caught after the gaps were closed, including both branches of the task
listing. Every one of these mutations passes a single-tenant suite.

### Counter-proofs

- `A-X` blocks `A-X`; `A-X` does **not** block `B-X`. Isolation did not disable contention.
- The same workspace *name* under two clients is two workspaces, because identity is derived from organization + client + workspace.
- A task id, run id or approval id from the other client reads as absent -- and the *unscoped* read still works, which is why the engine always scopes.
- Deciding another client's approval raises; their queue is untouched.
- A secret refusal does not carry the value it refused.
- Adding an ALLOW to A's policy does not reach B, and adding a DENY to B's does not reach A.
- Exhausting A's daily budget leaves B working; crossing midnight resets each separately; restarting preserves each.

### Capability status

| Capability | State | Evidence |
|---|---|---|
| Two full client contexts in one engine and store | **EXERCISED_REAL** | concurrent processes, both sides killed |
| Identical local identifiers stay distinct entities | **EXERCISED_REAL** | same task, repo, resource, branch, workspace name |
| Lease isolation by `(workspace_id, resource)` | **EXERCISED_REAL** | claims on the same resource name succeed for both |
| Task, run, approval reads scoped by tenant | CONTRACT_TESTED | id-substitution tests |
| State mutation scoped by tenant | CONTRACT_TESTED | cross-boundary transition refused |
| Policy per client | CONTRACT_TESTED | opposite `repo.push` verdicts, edits do not cross |
| Budget per client | CONTRACT_TESTED | exhaustion, midnight, restart |
| Secret scope per workspace | CONTRACT_TESTED | each resolves only its own |
| Observability scoped, aggregates safe | CONTRACT_TESTED | health and events carry no other tenant |
| No module-level state holds tenant data | CONTRACT_TESTED | AST audit with an explicit allowlist |
| Failure in one client does not freeze the other | **EXERCISED_REAL** | kills on both sides, both kept working |

### Limitations

Both clients use the filesystem task provider and the deterministic agent. The
tenancy boundary is proven; what is not proven is two clients against two
different *real* providers at once, which needs M6 and M7 unblocked.

The two clients share one process tree started by one harness. A deployment
where tenants are configured by different people, with different config files
and different credentials, is the next step in realism -- the boundary tested
here is the engine's, not the operator's.

---

## Milestone 9 — real concurrency between processes

`max_workers > 1` is a setting. Concurrency is a fact about processes, and until
this milestone the project only had the setting: a thousand-tick soak with
`max_workers=3` never held more than one lease at a time, so leases,
`BEGIN IMMEDIATE` and the `(workspace_id, resource)` key were untested code that
looked tested.

### What ran

Separate interpreters, separate memory, independent lifetimes, one SQLite file.
Threads would have shared a GIL and a heap and let a broken design pass.

| | |
|---|---|
| rounds | 60 |
| worker processes | 238 |
| `SIGKILL`s during contention | 60 |
| dispatches | 1193 |
| deferrals (resource already held) | 283 |
| recoveries after a death | 33 |
| **duplicate ownership** | **0** |
| **duplicate execution** | **0** |
| **stale writes accepted** | **0** |
| **deadlocks** | **0** |
| **unhandled `database is locked`** | **0** |
| **invariant violations** | **0** |
| **ticks crashed** | **0** |

### Defects found, and what each one looked like

| # | Defect | Why it was invisible |
|---|---|---|
| 1 | **Nothing ever renewed a lease.** The field comment promised "o worker renova"; no code renewed. | Every mission shorter than one lease window worked perfectly. |
| 2 | **Ownership was never rechecked before writing.** A worker finished a mission and wrote the result for a task it no longer owned. | Refused only because that transition happened to be illegal from the state the task was in. One state further along it would have been legal and would have overwritten another worker's run. |
| 3 | **A metadata save rewrote the task's state.** A stale snapshot wrote an old state back: no validation, no event, no trace. | The only sign was an `updated_at` six seconds newer than the last transition. The task sat in an active state nobody owned. |
| 4 | **Dispatch was three separate writes.** A `SIGKILL` between them orphaned a live lease whose owner had no run row. | Recovery matches expired leases against active runs, so an orphan matched nothing and blocked its resource silently. Appeared in 2 of 25 rounds. |
| 5 | **A claim and the task's transition were separate.** Another worker could escalate the task in between. | Left a live run holding resources for a task it could not have. |
| 6 | **Losing a benign race killed the whole tick.** Discovery, analysis and decision application each raised. | Two workers agreeing cost the loser a full cycle of work. Four distinct shapes, found one at a time. |
| 7 | **Refusing the stale result was not enough.** Two processes ran the same task in the same directory. | Both writes were correctly refused; both had already executed. |

### Counter-proofs

- A live lease cannot be taken, an expired one can, and **no instant exists where both owners are told yes** -- walked second by second across the boundary rather than checked once at the end.
- A stale owner's renewal is refused; a stale owner's release cannot free the new owner's lease.
- Two workspaces hold a resource of the same name simultaneously, without contending.
- A mission needing two resources takes both or neither.
- Losing the lease now **cancels** the mission. A process-driving runner is killed; an in-process runner can only cooperate, which is stated rather than assumed.

### A measurement that lied

The first overlap detector timed missions from a ledger file, and reported four
duplicate executions in forty rounds. It was measuring itself: every ledger line
opens and closes a file, and with four processes on one path a single write took
over a second, so a mission that slept for 150ms measured as a 1.9s window and
"overlapped" its own successor.

Re-measured from `runs.started_at` / `runs.ended_at` -- written by the engine on
the path that did the work -- the same campaign shows zero. The instrument had
been reporting its own latency as concurrency, which is worth recording because
the conclusion it produced was alarming and wrong.

### Mutation sweep

Twenty-one mutations aimed at concurrency, each applied, the suite and a short
real-process campaign run, the source restored.

**Sixteen caught immediately. Five escaped, and every one of them exposed a
guard that was present and unexercised:**

| Mutation | What was missing |
|---|---|
| Drop `workspace_id` from the claim's lease check | the tenancy test went through `acquire_lease`, so the scoping inside `claim` was never touched |
| Stop retrying a locked database | nothing forced contention past `busy_timeout`, so the retry sat idle |
| Never renew during a mission | the `Heartbeat` was tested; the tick's *use* of it was not |
| Stop clearing orphaned leases | the mutation was a no-op of mine (`[] or X`), and the real one was uncovered |
| Let a retry re-run the transaction body | added while fixing the above: nothing proved a retried write happens exactly once |

All five caught after the gaps were closed. The last is the sharpest: a retry
placed one line lower would re-execute statements the caller may already have
observed, and now that is a red test rather than a subtle corruption.

### Capability status

| Capability | State | Evidence |
|---|---|---|
| Several independent processes on one store | **EXERCISED_REAL** | 60 rounds, ~240 processes |
| Exclusive resource ownership under contention | **EXERCISED_REAL** | 0 duplicate ownership |
| Ownership revalidated at the moment of acting | **EXERCISED_REAL** | stale writes refused in campaign and tests |
| Lease renewal during a long mission | CONTRACT_TESTED | heartbeat tests |
| Atomic claim: run + leases + transition | **EXERCISED_REAL** | orphan-lease race disappeared |
| Killing a worker mid-contention | **EXERCISED_REAL** | 60 `SIGKILL`s |
| Workspace isolation under contention | CONTRACT_TESTED | store-level, exact interleaving |
| Deterministic acquisition order | CONTRACT_TESTED | structural, at the point of acquisition |
| `database is locked` retry preserving semantics | CONTRACT_TESTED | retry sits before the block; 0 unhandled in campaign |
| Cancelling a mission on ownership loss | IMPLEMENTED | process runners kill their child; in-process runners cooperate |

### Limitations

The contending workers run the deterministic agent, not a model, and the board
is the filesystem adapter. This milestone is about coordination, not agents.

Duplicate execution is zero **as measured**, and the mechanism that guarantees it
depends on the runner: a process-driving runner can be killed within
milliseconds of losing its lease; an in-process runner can only be asked. A
runner that ignores cancellation could still overlap, and that is a property of
that runner rather than of the engine.

---

## Milestone 8 — sustained unattended operation

The premise the whole project rests on, tested for the first time. Every earlier
milestone proved a capability in one supervised invocation. This one asks what
happens on the fourth day.

### What the soak actually ran

A thousand ticks in 63 seconds of wall time, covering about three simulated
weeks, against the real orchestrator, the real store, the real state machine and
the real scheduler. Faults were injected at the real boundaries by **wrapping**
providers rather than replacing them, so the engine cannot tell an injected
outage from a real one.

| | |
|---|---|
| ticks | 1000 |
| dispatches | 154 |
| worker deaths injected | 13 |
| recoveries | **13** (exact match) |
| provider outages injected | 142 |
| runner failures injected | 14 |
| escalations | 150 |
| restarts without a clean close | 43 |
| **invariant violations** | **0** |
| **ticks crashed** | **0** |

Invariants are checked after **every** tick, not at the end. A run that only
checks its final state cannot tell a system that stayed correct from one that
broke and healed, and the second will break and not heal on the day it matters.

### Growth, measured rather than assumed

| | tick 1 | tick 500 | tick 1000 |
|---|---|---|---|
| events | 10 | 2114 | 4199 |
| data | 4KB | 852KB | 1544KB |
| write-ahead log | 591KB | 4039KB | 4047KB |
| runs RUNNING | 0 | 0 | 0 (max 1) |
| live leases | 0 | 0 | 0 (max 1) |
| open approvals | 0 | 0 | 0 (max 8) |

Growth is **linear**: 4.20 events/tick over the first half, 4.18 over the
second. Runs, leases and approvals are bounded rather than accumulating. The
write-ahead log plateaus around 4MB, which is SQLite's own autocheckpoint
threshold and not data.

That last distinction cost a fix. `database_bytes` originally returned one
number, and a soak reported "3.4MB of database" for 108 events -- 200KB of data
and the rest an uncheckpointed log. Growth measured on the total is growth
measured on churn. Data and log are now reported separately, and `checkpoint()`
is an explicit maintenance policy that MOVES committed pages into the file. No
retention policy deletes anything.

### Defects found, all of them silent

Every one of these would have shipped. None could fail a test that ran once.

| # | Defect | How it looked |
|---|---|---|
| 1 | **Every dispatched task parked in `TESTING` forever.** Nothing in the project advances a task out of it; the scheduler skips active tasks. | The queue looked busy, ticks came back clean, the work was never touched again. Found on tick 4 of the first soak. |
| 2 | **Deciding an approval moved nothing.** `regente decide` recorded the choice and printed that the next tick would resume the task. No tick read it back. | Every escalation was a one-way door. The unit test passed because the test itself performed the transition the engine never did. |
| 3 | **`resumable_from` omitted the return-to-queue route** that `allowed_from` grants, so escalating a task gave a human FEWER options than the engine already had. | The only useful decision -- send it back -- died with `InvalidTransition`. |
| 4 | **Two clocks.** Leases were stamped from the module clock and checked against the injected one; runs from a third. | A run sat `RUNNING` with a "live" lease for 103 consecutive ticks, four simulated days, and health reported OK the whole way. |
| 5 | **Health excused an ancient run because its lease looked live.** | See above: OK for 103 ticks. |
| 6 | **The escape sentinel guarded all of `.git`,** which `git status` rewrites on every read. | A test meant to prove a CI-file edit was caught passed intermittently because the engine was tripping over its own footsteps. |

Defects 1, 2 and 3 are one story: the engine could stop and could not start
again. Defect 4 is why the recovery that did exist never fired.

### Capability status

| Capability | State | Evidence |
|---|---|---|
| Many ticks, faults injected at real boundaries | **EXERCISED_REAL** | 1000-tick run, 0 violations |
| Lease acquisition, expiry, recovery | **EXERCISED_REAL** | 13 deaths, 13 recoveries |
| Restart from disk after an unclean stop | **EXERCISED_REAL** | 43 restarts in one run |
| `kill -9` of a real process, state read by a fresh interpreter | **EXERCISED_REAL** | `test_state_survives_a_real_kill_and_the_next_process_finds_it` |
| Provider outage, rate limit, timeout | **EXERCISED_REAL** | 142 injected; board never read as empty |
| Day boundary and budget reset | CONTRACT_TESTED | deterministic clock; yesterday's spend proven untouched |
| `regente health` from persisted state | **EXERCISED_REAL** | answers after the engine is closed |
| Escalate -> decide -> resume | CONTRACT_TESTED | every option has a destination derived from the state machine |
| Orphan work areas | CONTRACT_TESTED | reported, never deleted |
| Growth measurement | **EXERCISED_REAL** | linear over 1000 ticks |
| **Unattended operation against real external providers** | **BLOCKED_EXTERNAL** | the soak runs the real engine against local providers; a real board and repository would need the M6/M7 blockers lifted |

### What the soak does NOT prove

The task provider is the filesystem adapter and the agent is deterministic.
That is deliberate -- this milestone is about time, faults and recovery, not
about agents -- but it means the numbers above say nothing about how the engine
behaves against a real board's latency or a real model's cost. Those remain
blocked where M6 and M7 left them.

---

## Milestone 7 — real AgentRunner

### The headless-agent inventory, re-taken

The Milestone 5 inventory is no longer true, in both directions.

| Probed | Result |
|---|---|
| `claude`, `codex`, `aider`, `cursor-agent`, `gemini`, `goose`, `amp`, `opencode`, `copilot`, `windsurf`, `cline` on PATH | none present, POSIX or Windows |
| npm globals | `@nestjs/cli`, `corepack`, `npm` only |
| pip packages resembling an agent | none |
| the CLI named by `CLAUDE_CODE_EXECPATH` | **present and headless-capable, v2.1.260** |

One headless coding-agent binary does exist. It runs, it accepts every flag the
adapter builds, and it returns a well-formed JSON envelope.

### The blocker, stated without presuming a mechanism

An earlier version of this document said the blocker was a missing
`ANTHROPIC_API_KEY`. That was wrong, and wrong in a way worth keeping on the
record: it presumed one implementation. Clients do not all authenticate a coding
agent with an API key. A corporate subscription, a CLI sign-in, SSO, a workspace
credential, an internal gateway and a key are all ordinary, and an engine whose
diagnosis names a variable is an engine that gives wrong advice to everyone
using another mechanism.

So the diagnosis is six independent axes, and authentication is the adapter's
business:

```
adapter        : claude-code
auth mode      : SESSION
executable     : YES  -- reports 2.1.260
protocol       : YES  -- structured output is requested by the invocation
authentication : NO   -- the tool reports it is not signed in
agent          : UNKNOWN -- not reachable until authentication holds
policy         : YES  -- ALLOW
budget         : YES  -- within ceiling
result         : BLOCKED_AUTHENTICATION
```

That is the real output of `regente doctor` against the real binary. What is
missing is stated by the adapter in the adapter's own terms; the engine reports
only `BLOCKED_AUTHENTICATION`, which is actionable wherever the reader works.

The engine does **not** borrow the interactive session's credential, and does not
look in the environment for one. Both would be authority no policy governs and no
workspace owns.

**What would unblock it:** authenticating this workspace's agent by whatever
mechanism the workspace configures -- signing the CLI in, or setting
`auth: resolved_secret` / `auth: gateway` with a `credentials` mapping the
workspace's own `SecretProvider` can resolve. The engine supports all of them and
prefers none.

### Authentication shapes the engine understands

Shapes of exchange, never products. `core/`, `ports/` and `engine/` contain no
vendor name and no credential shape -- asserted by a test, not by intention.

| Mode | Who holds the credential | Example |
|---|---|---|
| `SESSION` | the tool itself | a corporate coding-agent subscription, a CLI sign-in, SSO |
| `RESOLVED_SECRET` | the engine, scoped to the workspace | an API key named in `secrets:` |
| `GATEWAY` | the engine, for an intermediary | an internal LLM gateway |
| `DELEGATED` | a host process | never assumed to work; reported `UNKNOWN` |
| `NONE` | nobody | a deterministic or local agent |

Swapping the agent is a file in `adapters/`. Demonstrated rather than asserted:
a second vendor profile exists (`codex_cli.py`), and adding it required no change
to `core/`, `ports/` or `engine/`. **Its argv is unverified** -- the tool is not
installed here, so the flags come from its documented surface and have never been
executed. That is IMPLEMENTED, not tested, and it is listed as such below.

### Capability status, axis by axis

`AgentRunner` is not one capability, and reporting it as one would hide exactly
the distinction that matters. Seven rows, because six of them are green and the
seventh is the only one anybody actually wants.

| # | Capability | State | Evidence |
|---|---|---|---|
| 1 | Adapter executable detection | **EXERCISED_REAL** | probes the real binary; reports its real version |
| 2 | Protocol (structured in, structured out) | **EXERCISED_REAL** | the real CLI accepts every flag the adapter builds; its real envelope parses |
| 3 | Authentication state | **EXERCISED_REAL** | the real tool was asked and answered: not signed in |
| 4 | Agent (model) availability | **BLOCKED_EXTERNAL** | unreachable until authentication holds; reported `UNKNOWN`, never assumed |
| 5 | Policy | CONTRACT_TESTED | `tests/test_authority_budget.py` |
| 6 | Budget | CONTRACT_TESTED | `tests/test_authority_budget.py` |
| 7 | **Real model execution** | **BLOCKED_EXTERNAL** | never happened; see below |

The distinction rows 1-3 and row 7 record, stated so it cannot be softened later:

```
real CLI                 : YES
real authenticated model : NO
```

The binary is real, was executed, and answered. No model has run. Nothing in
this milestone has produced a line of code written by a language model, and no
green suite should ever be read as though it had.

| Supporting capability | State | Evidence |
|---|---|---|
| `AgentRunner` port, single and vendor-free | IMPLEMENTED | `ports/agent.py`; the duplicate port is gone |
| Structured `Mission`, eleven required fields | CONTRACT_TESTED | `test_a_mission_tells_the_agent_what_it_may_not_do` |
| Structured `Outcome`, claims named as claims | CONTRACT_TESTED | parse tests and real-envelope tests |
| Independent observation of the work area | CONTRACT_TESTED against **real git repositories** | `tests/test_agent.py` |
| Escape detection outside the work area | CONTRACT_TESTED against a **real filesystem** | sentinel tests |
| Authority-path detection | CONTRACT_TESTED | parametrised violation tests |
| Anti-loop: retry only on new information | CONTRACT_TESTED | `tests/test_agent.py` |
| Context Engine with a reason per item | CONTRACT_TESTED | context tests |
| Sandbox: no command execution | CONTRACT_TESTED through **real child processes** | argv asserted; environment asserted through a real subprocess |
| Five authentication shapes | CONTRACT_TESTED | ten scenarios, real executables for the probes |
| Two independent vendor profiles | IMPLEMENTED; one **argv unverified** | `vendors/`; the second tool is not installed here |
| No vendor adapter depends on another | CONTRACT_TESTED **by deletion** | a copy of the package with one vendor removed still imports the other |
| A new vendor needs no change above `adapters/` | CONTRACT_TESTED **by addition** | a vendor written into a copy, run through the engine's readiness path, neutral layers byte-identical afterwards |

### The twelve mandatory negatives

Each ends in an explicit state. None can reach implicit success.

| # | Scenario | Ends as |
|---|---|---|
| 1 | claims success, changed nothing | `NO_CHANGE` + `claimed_not_found` discrepancies |
| 2 | claims tests passed, tests fail | `REGRESSED` |
| 3 | modifies a file outside the expected area | `NEEDS_HUMAN` + `escape` violation |
| 4 | attempts a workspace escape silently | `NEEDS_HUMAN` + `escape` violation |
| 5 | attempts `git push` | no command tool exists; reaching for it via `.git/config` is `NEEDS_HUMAN` |
| 6 | attempts pull-request creation | no command tool exists; `open_pr` is withheld and named |
| 7 | attempts deploy or cloud mutation | no command tool exists; CI/deploy paths are guarded |
| 8 | times out | `TIMEBOX`, real process really killed |
| 9 | malformed structured output | `ERROR`, including valid JSON with invented vocabulary |
| 10 | process crashes | `ERROR` with exit code and stderr |
| 11 | repeated identical failed attempts | stops before the budget, saying nothing new could be learned |
| 12 | verification tool unavailable | `READY_FOR_REVIEW` stating nothing verified it -- never green |

### Mutation sweep

Guards are only guards if removing them breaks a test. Thirty-two mutations, each
applied, the suites run, the source restored.

**29 caught.** The three that were not, and what they exposed:

| Mutation | First result | What was missing |
|---|---|---|
| Assume a session is authenticated when the tool offers no way to be asked | MISSED | no profile in the suite lacked an auth probe |
| Assume a delegated login works | MISSED | covered on the process adapter, not on the CLI base |
| Stop refusing flags that disable the sandbox | SKIP | the guard had moved into the base class and the sweep could not find it |

All three now caught. A fourth appeared while closing them: an auth mode the
adapter has never heard of fell through as passing, and the test written to catch
it used a mode that *is* handled -- so it never reached the fallback and passed
for the wrong reason. The sweep found that too.

That is the argument for running these at all. Every one of the four was a guard
that looked present, read correctly, and was holding nothing.

### The sandbox, stated exactly

The agent is given `Read`, `Edit`, `Write`, `Glob`, `Grep`. It is given no tool
that runs a command. `git push`, `gh pr create`, `gcloud`, `terraform` and every
escalation route nobody has thought of yet are variations of one capability, and
withholding that capability closes all of them at once -- which a denylist of
known-bad commands cannot do.

Four flags, each enforced by the CLI process rather than by the model:
`--restricted`, `--tools`, `--disallowed-tools`, `--permission-prompts none`.
Every one verified as accepted by the real binary. The environment is composed
from empty rather than inherited, so no credential the engine holds reaches the
agent.

---

## Milestone 6 — remote delivery

| Capability | State | Evidence |
|---|---|---|
| Push contract (`WorkspaceProvider.push`, `expected_sha` required) | CONTRACT_TESTED | `tests/test_push_target.py`, `tests/test_remote.py` |
| Push refuses integration branches, foreign branches, local targets, SHA drift, force | CONTRACT_TESTED | `tests/test_push_target.py` |
| GitHub write adapter (`pr create` only) | CONTRACT_TESTED | `tests/test_remote.py::test_the_write_gate_*` |
| `create_pull_request` with a mandatory run marker | CONTRACT_TESTED | `test_a_pull_request_carries_the_run_marker` |
| `get_pull_request` / read-back after creation | IMPLEMENTED | no test drives the real CLI |
| CI observation, read-only | CONTRACT_TESTED | `test_ci_is_asked_about_the_commit_not_the_pull_request` |
| CI classification (6 results, 4 states) | CONTRACT_TESTED | `tests/test_remote.py`, CI section |
| Persisting `task → run → commit → PR → CI` | CONTRACT_TESTED | `test_the_ci_observation_is_persisted`, store tests |
| PR identity by head SHA + marker | CONTRACT_TESTED | seven adoption tests |
| Policy + identity + SHA revalidated per mutation | CONTRACT_TESTED | mutation-checked (see below) |
| **The whole chain against a real repository** | **BLOCKED_EXTERNAL** | no eligible task — see below |

### What BLOCKED_EXTERNAL means here, precisely

Nothing in this milestone has been pushed anywhere. No pull request has been
opened. No real CI has been observed. The gate is not technical: the criteria
for an eligible task (small, reversible, no production path, no infrastructure,
no secrets, not owned by a person, real acceptance criteria) are not met by any
task currently on the board, and inventing one to complete the exercise would
make the proof worthless — it would demonstrate that the engine can act on a
task written to let it act.

The engine stops at `NEEDS_HUMAN`. That is the correct outcome, not a failure.

### Mutation checks

Guards are only guards if removing them breaks a test. Each was deleted, the
suite run, and the guard restored:

| Mutation | Result |
|---|---|
| Drop the SHA revalidation before push | caught |
| Drop the identity check before push | caught |
| Adopt a PR on marker alone, ignoring the head | caught |
| Adopt an unmarked pull request | caught |
| Report provider unavailability as a CI result | caught |
| Let confirmed absence of checks read as `PASSED` | caught |
| Call a red check a regression with no baseline | caught |
| Drop the one-PR-per-workspace constraint | caught |
| Stop refusing `--web` on a permitted write | caught |
| Widen the write allowlist to `pr merge` / `pr review` | *not* caught — correctly: the forbidden list still refuses |
| Empty the forbidden list, allowlist left narrow | *not* caught — correctly: the allowlist still refuses |
| Break **both** allowlist layers at once | caught |

The last three are the interesting ones. The write gate is two independent
layers, and no single-layer edit lets a merge through.

---

## Earlier milestones

| Capability | State | Evidence |
|---|---|---|
| Core, persistent state, policy, scheduler, recovery | EXERCISED_REAL | M1, `8d3e5de` |
| Jira task provider, read-only | EXERCISED_REAL | M3, `49a8e73` — real site, real issues |
| Repository provider, read-only | EXERCISED_REAL | M4, `4e37827` |
| Target resolution, DECLARED and DISCOVERED | EXERCISED_REAL | M5, `MILESTONE-5-PROOF.txt` |
| Agent run + validation loop + commit on an isolated branch | EXERCISED_REAL | M5, `8aac885` — both paths, real repos, forbidden mutations verified by inspecting the world |

---

## Authority, unchanged

Allowed at L2: push of an execution branch, opening a pull request, observing
CI. Forbidden regardless of level: merge, approve, review, deploy, production
change, writing back to the external task, triggering or cancelling CI.

The agent holds none of these. It produces a commit; producing a commit does not
entitle it to publish one. `regente/engine/remote.py` never reads the agent's
`Outcome`, and a test asserts that structurally.

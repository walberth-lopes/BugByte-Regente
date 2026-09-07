# A API de leitura e os modelos expostos

```
Store / Engine  ->  Read Model  ->  API  ->  Mission Control
```

Quatro camadas, e a regra que as separa: **cada uma so consome a anterior**. A
Mission Control nunca fala com SQLite, com adapter, com provedor nem com o Core.
A API nunca decide nada sobre o trabalho. O read model nunca escreve.

## Por que existe um modelo externo separado

A linha do banco e o payload da API sao coisas diferentes de proposito.

Se a linha vazasse inteira, renomear uma coluna quebraria quem consome, e um
campo interno novo viraria publico so por existir. Mais grave: campos internos
carregam significado que so faz sentido dentro do motor -- `paused_at`,
`resources`, `data` -- e uma tela que os le acaba interpretando-os, que e
exatamente a duplicacao de regra que esta separacao existe para impedir.

O modelo externo tambem carrega o que o motor **concluiu**, nao so o que ele
guardou. `state.owner` e `ci_green` sao exemplos: a tela nao pode deduzir quem
move um estado nem o que conta como CI verde, porque a resposta certa depende de
`ENGINE_ADVANCES`, `AWAITING_EXTERNAL`, `PREEXISTING_FAILURE` e `NO_CHECKS` --
conhecimento do Core que uma segunda implementacao erraria em silencio.

## Identidade, autorizacao, policy: tres perguntas diferentes

```
Autenticacao  quem e voce?            -> IdentityProvider, prova um segredo
Autorizacao   voce manda aqui?        -> Principal.may_read / may_decide
Policy        esta acao e permitida?  -> o arquivo de regras, independente
Transicao     este estado permite?    -> a maquina de estados
```

Nenhuma responde pela outra. Um principal autenticado nao esta autorizado; um
autorizado nao venceu a policy; e uma policy que permite nao reabre uma
aprovacao ja decidida.

### O cliente apresenta um segredo; ele nao declara um nome

`Principal.method` e o campo que separa **autenticado** de **afirmado**. Vazio
significa que ninguem provou nada -- e a diferenca entre "o servidor verificou
um segredo que ele proprio emitiu" e "o navegador digitou um nome".

O corpo de um POST nao pode carregar identidade nem escopo. `principal`,
`subject`, `decided_by`, `per`, `actor`, `method` e `workspace_id` sao recusados
com `400`.

### Ler e agir sao concessoes separadas

`workspaces` (leitura) pode ser aberto; `abilities` (acao) vem **so** de
`AccessGrant` gravado, com autor e data. Derivar acao de leitura faria de todo
observador um decisor.

Nenhum provedor de identidade concede autoridade. Ele responde *quem e voce*; o
`AccessService` responde *o que voce recebeu*, lendo concessoes vivas a cada
requisicao -- e e por isso que revogar fecha a porta na chamada seguinte, sem
depender de a tela esconder um botao.

### A identidade interna e `provedor:sujeito`

O provedor faz parte da chave. Sem ele, `walberth` numa conta de sistema e
`walberth` num diretorio corporativo seriam a mesma pessoa dentro do motor, e a
concessao de uma valeria para a outra.

O sujeito e o identificador **estavel** que o provedor emite -- o SID, o uid, o
`sub` -- nunca o nome de exibicao nem o email: os dois mudam, e uma concessao
amarrada a algo que muda se transfere sozinha.

### Administracao de acesso

```
GET    /api/workspaces/{id}/access                 lista, com historia
POST   /api/workspaces/{id}/access                 {"principal","role","note"}
DELETE /api/workspaces/{id}/access/{provedor:sujeito}
```

Papeis: `operator` (decide escaladas), `admin` (administra acesso), `keeper`
(administra credenciais), `service` (so usa credencial -- e o papel do proprio
motor) e `owner` (todos). Um papel desconhecido concede **nada**, nunca tudo.

Capacidades sao fotografadas no momento da concessao, nao relidas do papel:
ampliar a definicao de um papel nao expande concessoes que ja existem.

Conceder a si mesmo e recusado mesmo com autoridade para conceder: uma concessao
so vale como prova se houver duas pessoas na linha. A excecao e a **concessao
inicial** (`regente access inicial`), que exige identidade do sistema
operacional, so funciona num workspace sem nenhuma concessao, e fica registrada
com um verbo proprio.

O corpo nao pode carregar `actor`, `granted_by`, `workspace_id`, `client_id` nem
`abilities`: ator, escopo e capacidades nao vem da requisicao.

### O mecanismo de desenvolvimento

`dev-token`: um segredo por processo, gerado ao subir, **so em memoria**,
injetado na pagina que o proprio servidor serve. Um site aberto noutra aba pode
disparar um POST para o loopback, mas nao consegue LER essa pagina -- entao nao
alcanca o token, e o POST forjado chega sem credencial.

Ele se anuncia: `development_only = True`, aparece na tela, no `describe()` e em
`/api/health`. Um mecanismo de desenvolvimento indistinguivel de um real e pior
que nenhum, porque cria a sensacao de que ha autenticacao.

E recusa autenticar fora do loopback -- no codigo, nao no README.

**Limitacao declarada:** nao ha usuarios, expiracao, revogacao nem segundo
fator, e o token vale para quem o tiver. Ele prova que quem chama e quem rodou
`regente ui` nesta maquina. Substituir por OIDC/SSO e implementar
`IdentityProvider`; nao toca em API, motor nem tela.

## A unica escrita

```
POST /api/workspaces/{id}/approvals/{approval_id}/decision
     {"choice": "<id de uma opcao oferecida>", "note": "opcional"}
```

Qualquer outro caminho ou metodo responde `405 read_only`.

O endpoint **nao** verifica identidade, escopo, policy nem estado. Ele valida a
FORMA da requisicao e traduz a recusa para HTTP; tudo o mais e do Core. Repetir
uma verificacao aqui criaria uma segunda regra que um dia discorda da primeira
-- sendo a daqui a que ninguem lembra de atualizar.

| recusa do Core | HTTP | o que a pessoa faz |
|---|---|---|
| `UNAUTHENTICATED` | 401 | reabrir a Mission Control |
| `POLICY_DENIED` | 403 | ler o arquivo de regras |
| `NOT_FOUND` | 404 | nada: o recurso nao existe neste escopo |
| `CONFLICT` | 409 | nada se perdeu; alguem chegou primeiro |
| `INVALID_STATE` | 422 | escolher entre as opcoes oferecidas |

`FORBIDDEN` nao aparece para recurso de outro tenant -- la a resposta e `404`,
porque um `403` confirmaria que o recurso existe.

### Leitura depois da escrita

`200` significa que o servidor aceitou, nao que a tela sabe o que ficou gravado.
A resposta da escrita nao vira segunda fonte de verdade: a tela **rele** -- e
rele tambem quando a decisao e recusada, porque o mundo pode ter mudado.

## Credenciais

```
GET    /api/workspaces/{id}/credentials              lista, com historia
POST   /api/workspaces/{id}/credentials              registra
DELETE /api/workspaces/{id}/credentials/{crd}        revoga
```

**Nao existe rota que devolva material secreto**, e a ausencia nao e uma lacuna:
uma tela nunca precisa do valor para administrar a autoridade dele. O que a API
devolve e o ENDERECO (`helper:github`, `env:NOME`) -- que diz onde procurar e
nao vale nada para quem nao esta naquela maquina.

`POST .../credentials/{id}/test` responde `501` de proposito: a sonda de conexao
pertence a composicao, e deixar a API escolher qual usar colocaria conhecimento
de fornecedor num lugar que nao pode te-lo. O teste roda por
`regente credentials testar`.

| recusa | HTTP | o que a pessoa faz |
|---|---|---|
| `EXPIRED` | 410 | renovar |
| `REVOKED` | 410 | falar com quem revogou |
| `NO_CAPABILITY` | 403 | registrar uma credencial com aquela capacidade |
| `SOURCE_UNAVAILABLE` | 503 | olhar a fonte, nao a credencial |

Capacidades sao explicitas por credencial: `repo.read`, `repo.push`, `repo.pr`,
`task.read`, `task.write`, `ci.read`, `agent.run`. Uma credencial de leitura nao
vale para push so porque o provider oferece push.

Administrar credenciais e uma capacidade humana separada de administrar pessoas:
o papel `keeper` tem `workspace.credential.*` e nao decide escalada; `admin`
administra pessoas e nao toca em credencial; `owner` tem os tres conjuntos.

O material sai do motor por um caminho so, e ele nao passa por aqui: o
`CredentialBroker` o entrega ao AMBIENTE de um subprocesso, no instante em que
o processo comeca. Nao ha rota HTTP nesse caminho, e nao ha rota HTTP que o
devolva.

**Usar credencial e uma quinta capacidade** (`workspace.credential.use`),
separada de administrar. Ja foi "qualquer capacidade neste workspace", e isso
transformava quem responde a fila humana em usuario de credencial por tabela.
E o unico poder do papel `service`, que e como o motor age quando roda sozinho:
sem concessao, ele nao usa credencial nenhuma.

## Rotas

Todas `GET`. Qualquer outro metodo responde `405 read_only` -- e nao o `501` da
biblioteca padrao, porque "metodo nao suportado" le-se como "ainda nao
implementado", e alguem acabaria implementando.

| rota | devolve |
|---|---|
| `/api/health` | nivel do pior workspace visivel + um cartao por workspace |
| `/api/clients` | clientes com seus workspaces |
| `/api/workspaces` | cartoes de workspace |
| `/api/workspaces/{id}` | igual a `/overview` |
| `/api/workspaces/{id}/overview` | painel: saude, contagens, estacionadas, frescor |
| `/api/workspaces/{id}/health` | as treze perguntas de saude, como o motor as responde |
| `/api/workspaces/{id}/tasks?state=` | cartoes de task |
| `/api/workspaces/{id}/tasks/{task_id}` | detalhe: estado, bloqueios, alvo, entregas, linha do tempo |
| `/api/workspaces/{id}/runs?limit=` | cartoes de run |
| `/api/workspaces/{id}/runs/{run_id}` | detalhe: identidade, posse, bloqueios, entrega, eventos |
| `/api/workspaces/{id}/deliveries?task=` | a cadeia de entrega |
| `/api/workspaces/{id}/events?limit=` | eventos como gravados |
| `/api/workspaces/{id}/escalations` | a fila de decisao humana |

### Escopo

Todo caminho escopado passa por duas linhas, nesta ordem, **antes** de qualquer
leitura:

```python
if not who.may_read(workspace_id): return _not_found("workspace")
if self.read.store.workspace(workspace_id) is None: return _not_found("workspace")
```

Um `may_read` avaliado depois da leitura protege o log, nao o dado.

"Nao existe" e "existe e nao e seu" respondem **exatamente igual** -- mesmo
status, mesmo corpo. Distinguir os dois confirmaria a existencia de um workspace
alheio para quem tentou adivinhar.

## Os modelos

### `StateView`

Um estado nunca chega a tela como uma cor.

| campo | o que e |
|---|---|
| `name` | `TESTING`, `BLOCKED`, ... |
| `meaning` | uma frase, vinda de `core.states.MEANING` |
| `reason` | por que ESTA task esta nele; vazio quando nao foi gravado |
| `since` / `age_seconds` / `age` | desde quando |
| `owner` | `engine`, `human`, `external` ou `nobody` |
| `next_action` | o proximo passo, quando existe |

`owner` e a pergunta que o operador faz antes de qualquer outra: *isto anda
sozinho, ou esta esperando por mim?* As quatro respostas saem de conjuntos que o
Core ja mantem (`ENGINE_ADVANCES`, `AWAITING_EXTERNAL`, `TERMINAL`,
`is_terminus`), nunca de uma lista paralela.

### `Blocker`

`kind` e vocabulario fechado para a tela agrupar sem interpretar texto:
`HUMAN`, `BLOCKED`, `NO_ROUTE`, `CI_PENDING`, `CI_UNAVAILABLE`, `CI_NO_CHECKS`,
`BLOCKED_AUTHENTICATION`, `FAILED`, `ABORTED`, `INTERRUPTED`.

Nenhum e deduzido de nome, convencao ou heuristica. Cada um sai de um registro:
um estado, uma aprovacao aberta, uma coluna de CI, um evento gravado pelo motor.

### `DeliveryView`

A cadeia `task -> run -> commit -> push -> PR -> CI`, montada a partir das
colunas efetivamente escritas.

* sem PR: `pull_request` e `null` e `pull_request_absent` diz o que se sabe;
* CI nunca lido: `ci_state` e `NOT_OBSERVED`;
* `ci_green` so e verdadeiro com `CONCLUDED` **e** resultado bom.

`NO_CHECKS` e o caso perigoso: nada rodou, e a ausencia de vermelho parece verde
para qualquer leitor apressado -- inclusive para um `if not red`. Por isso o
veredito viaja pronto no payload.

### `TaskDetail` e `RunDetail`

Respondem, sem terminal: qual workspace, qual cliente, qual task, qual run, qual
repositorio, qual branch, qual area, qual agente, quanto durou, o que segura
agora, o que impede, e a linha do tempo -- **somente eventos gravados**.

### `HealthView`

As treze perguntas de `regente health`, reexpostas. Nao recalculadas.

Uma segunda implementacao de saude na borda e uma segunda opiniao sobre estar
tudo bem, e a que apareceria na tela seria a que nao viu o problema. Os mesmos
tetos de orcamento do terminal sao passados ao read model: sem eles a mesma
pergunta teria duas respostas, e a menos informada seria a que o operador olha.

## Atualizacao

Polling, a cada 5s, e so enquanto a aba esta visivel.

Nao ha WebSocket nem SSE porque o motor nao tem barramento de eventos ao vivo --
introduzir um transporte em tempo real sobre uma fonte que so muda a cada tick
seria infraestrutura sem informacao nova.

A tela **nunca inventa estado entre duas leituras**: quando uma leitura falha, o
erro aparece e a marca de frescor fica vermelha, em vez de manter na tela um
estado antigo com cara de atual.

## O que a UI nao pode fazer

Nao ha rota de escrita. Nenhuma.

Mergear, aprovar revisao, fechar PR, empurrar, publicar, disparar CI, alterar
policy, alterar orcamento, alterar segredo, escrever na task externa: nada disso
tem porta aqui, e a ausencia nao e uma lacuna a preencher quando der. Essas
autoridades pertencem ao motor e ao humano. Quando uma acao humana entrar na
tela, ela passa pelos mesmos ports, policy e gates que ja existem -- nunca por um
caminho novo aberto porque um botao precisava funcionar.

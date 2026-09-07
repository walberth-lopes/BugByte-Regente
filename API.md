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

## Autenticacao: o que existe e o que nao existe

Existe um `Principal` com escopo de workspaces. Nao existe autenticacao.

Esta versao serve um unico operador local e, por isso, **so escuta em
loopback**. `regente ui` restringe o principal aos workspaces do proprio arquivo
de configuracao; `--all-workspaces` amplia para todos os do banco.

A fronteira existe agora, vazia, porque e o que fica dificil de acrescentar
depois: uma API que nasce sem nocao de identidade espalha `workspace_id` vindo
do cliente por toda parte, e quem precisar restringir nao acha onde. Endurecer e
trocar de onde vem o `Principal`; nao e reescrever rotas.

**Limitacao declarada:** expor esta API na rede sem antes ligar uma identidade
real entrega o estado de todos os clientes visiveis a quem alcancar a porta.

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

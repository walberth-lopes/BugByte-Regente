# Marco: integrações autodescobríveis

> Um adapter deixa de ser um mecanismo sobre recursos configurados à mão e passa
> a ser uma integração: ele **descobre** o que a identidade alcança, **apresenta**,
> a pessoa **escolhe**, e só a escolha é persistida.

---

## A fronteira que este marco instala

Uma credencial que alcança 47 repositórios **não** autoriza o motor a trabalhar
em 47. Ela autoriza **perguntar**. Entre "o provedor tem" e "o motor pode tocar"
passa a existir uma escolha humana, gravada e atribuível.

```
                      credencial de DESCOBERTA        credencial de LEITURA
                               │                              │
   identidade → concessão → capacidade → policy → adapter      │
                               │                              │
                          [ descobrir ]                       │
                               ↓                              │
                     o que a conta alcança                    │
                               ↓                              │
                       ESCOLHA HUMANA  ──── grava ───→ workspace_resources
                               ↓                              │
                     SomenteSelecionados ─────────────────────┘
                               ↓
                    o que o motor consegue enxergar
```

Sem a etapa do meio, conectar um provedor entregaria a ele tudo — e ninguém
teria decidido isso.

---

## 1. O que foi implementado

### Domínio — `regente/core/resource.py` (novo, 265 linhas)

| tipo | o que é, e por que existe |
|---|---|
| `ResourceRef` | `provedor:tipo:id`. `scoped_to(workspace)` produz a identidade que vira linha no banco — sem ela, dois clientes com o mesmo `org/backend` seriam um recurso só. |
| `Resource` | o recurso como o **provedor** o descreve. `selectable` + `note`: o que só serve para navegar diz **por quê**. |
| `Inventario` | recursos **ou** falha, nunca os dois. É o tipo que torna impossível uma leitura que falhou virar `[]`. |
| `Falha` | sete motivos separados, porque cada um manda a pessoa a um lugar diferente. |
| `Selecionado` | o que este workspace escolheu. Persistido. |
| `Situacao` | `DISPONIVEL` / `SELECIONADO` / `NAO_ENCONTRADO` — **derivada** a cada leitura, nunca guardada. |

### Porta — `regente/ports/discovery.py` (novo)

`ResourceDiscovery` com `discovers()` (a árvore, do tronco à folha) e
`discover(kind, parent)`. Opcional: um provedor que não descobre nada continua
válido. **Não recebe credencial, não conhece workspace, não sabe o que foi
selecionado.**

### Capacidades — `core/credential.py`, `core/access.py`

- `Use.TASK_DISCOVER` / `Use.REPO_DISCOVER` — **listar e ler são credenciais
  diferentes**. Conectar um provedor não concede leitura de tudo o que ele
  alcança.
- `Ability.RESOURCE_SELECT` — no papel `admin`, não no `operator`. Quem pausa o
  motor numa emergência não ganha, por tabela, o direito de mudar o que ele
  alcança.

### Motor — `regente/engine/resources.py` (novo, 541 linhas)

`ResourceService`: `selected`, `view`, `discover`, `select`, `select_refs`,
`unselect`. Toda escrita passa por `_may` (capacidade → escopo → workspace →
policy) e deixa evento na trilha — **inclusive a descoberta que falhou**, que é
justamente o que se quer investigar depois.

`SomenteSelecionados`: o envelope que corta a listagem do provedor.
**Na composição, e não em cada chamador** — um filtro no chamador teria mais de
um chamador, e o que esquecesse seria o furo.

### Persistência — schema 14

`workspace_resources`, chave `(workspace_id, provider, kind, resource_id)`.
Nasce **vazia**: preencher com o que já existe daria ao motor tudo, e ninguém
teria decidido isso. `mark_resources_seen` marca os vistos e **nunca apaga os
ausentes**.

### Adapters — `regente/adapters/discovery.py` (novo)

| provedor | árvore | o que se escolhe |
|---|---|---|
| GitHub | conta → repositório | o **repositório** (a folha) |
| Jira | projeto → board | o **projeto** (a raiz) |

Duas formas opostas de propósito: se a porta só coubesse numa, a segunda teria
de mentir sobre a própria árvore.

### Superfícies

| onde | o quê |
|---|---|
| **API** | `GET .../resources`, `GET .../resources/providers`, `POST .../resources/discover`, `POST .../resources/select`, `DELETE .../resources/{ref}` |
| **CLI** | `regente integracoes listar \| descobrir \| escolher \| remover` |
| **Tela** | Configuração › **Integrações** |

A tela navega por níveis com nomes — a árvore vem de `/resources/providers`, e
não de um `if provider === 'github'`.

### Policy

Duas regras novas em `policies/default.yaml` **e** no arquivo que `regente init`
entrega. `*.resource.discover` é curinga: um provedor novo não exige editar a
policy de todo mundo para aparecer na tela.

---

## 2. Testes

**`tests/test_recursos.py` — 69 testes**, mais 4 guards novos em
`test_interface.py` e `/resources` na varredura de tenancy de `test_api.py`.
Suíte completa: **1150 passaram, 6 puladas**.

Os defeitos que estes testes tornam impossíveis são todos **silenciosos**:

| o defeito | o teste |
|---|---|
| descobrir passa a selecionar | `test_a_discovered_resource_is_not_a_resource_the_engine_may_touch` |
| falha de leitura vira conta vazia | `test_no_provider_failure_ever_becomes_an_empty_account` |
| leitura incompleta apaga seleção boa | `test_a_selected_resource_missing_from_a_good_discovery_is_named` |
| dois clientes, o mesmo `org/backend` | `test_two_workspaces_in_the_SAME_database_never_read_each_other` |
| o corpo da requisição vira autoridade | `test_the_api_cannot_choose_what_the_provider_never_showed` |
| descobrir usa a credencial de leitura | `test_discovering_asks_for_a_different_credential_than_reading` |
| uma entrada nova sem barreira | `test_no_public_way_into_the_provider_skips_a_barrier` |

---

## 3. Varredura de mutação — 12 mutações, 12 mortas

Duas **sobreviveram** na primeira passada, e as duas eram buracos reais:

**A bancada dava um arquivo de banco a cada workspace.** Tirar o
`WHERE workspace_id = ?` da leitura não quebrava nada — arquivos separados não
se contaminam nem com a consulta errada. Tenancy só aparece quando os dois
dividem o mesmo banco, que é como todo cliente real roda. → novo teste com **um
banco, dois workspaces**.

**A API era testada só pelas chaves proibidas do corpo.** Trocar `select_refs`
por `select` com recursos montados a partir do corpo passava: bastaria enviar
`{"ids": ["inventado"], "selectable": true}` para pôr no workspace algo que
ninguém confirmou. → novo teste no nível da rota.

---

## 4. Mudanças arquiteturais

**`Capability.DISCOVERY`.** Descobrir é uma capacidade própria, não um método a
mais em `TASKS`/`REPOSITORY`: a pergunta é outra e a credencial é outra.

**`ExternalTask.sources`.** A task diz de quais recursos do provedor ela pode
ser atribuída. Vazio significa "este adapter não sabe dizer", e **não** "de
nenhum" — uma task que não sabe dizer **passa** pelo filtro. O risco dos dois
lados não é simétrico: deixar passar gera trabalho que alguém revisa e
interrompe; cortar gera silêncio, e ninguém revisa silêncio.

**`Resource.note`.** Uma caixa desabilitada sem explicação lê-se como defeito.

**Compatibilidade deliberada.** Um workspace **sem nenhuma seleção continua
vendo tudo**. A tabela nasce vazia, e quem atualiza não pode acordar com o motor
parado por uma decisão que ninguém tomou. A partir da primeira escolha, vale a
escolha.

---

## 5. Provedores

### Implementados e exercitados

- **GitHub** (`conta → repositório`) — sobre o provider já composto, com
  `Use.REPO_DISCOVER`.
- **Jira** (`projeto → board`) — transporte **próprio**, com `Use.TASK_DISCOVER`.
  Compartilhar o transporte com a leitura de issues faria as duas capacidades
  virarem uma.

### Preparados, não implementados

`ClickUp` (workspace → space → folder → list), `GitLab`, `Linear`, `Azure
DevOps`. O custo previsto é **um arquivo de adapter + uma linha de registro** —
sem tocar em tela, motor, policy ou schema. O critério está testado: nenhum nome
de fornecedor aparece no código de `core/`, `engine/` ou `ports/`
(`test_no_vendor_name_leaks_into_the_domain_or_the_engine`, lido da AST).

---

## 6. BLOCKED_REAL_WORLD_INPUT

**Não houve descoberta contra um GitHub ou Jira real.** Não há credencial de
provedor disponível para esta sessão, e inventar uma para produzir um resultado
verde seria pior do que não ter.

O que **foi** exercitado de ponta a ponta, com o servidor real e o navegador:

- `regente integracoes listar` num workspace novo → estado vazio + próximo passo;
- `regente integracoes descobrir --provider github` **sem credencial registrada**
  → `SEM_CREDENCIAL`, com a frase que diz o que fazer — e **não** uma lista vazia;
- a tela em Configuração › Integrações, no caminho de falha: o nome da falha, a
  saída, e *"o que este workspace já usa continua valendo — nada foi removido"*;
- navegação por níveis (Contas › Repositórios), o nó de navegação sem caixa de
  seleção e com o motivo escrito;
- layout em 375 px sem rolagem horizontal.

**O que continua por provar contra o mundo real:** que a resposta de
`gh repo list` e de `/rest/api/3/project/search` chega no formato que os
envelopes esperam. Nenhum teste com provedor falso prova isso.

---

## 7. LIMITAÇÕES

1. **Board do Jira não recorta trabalho.** Ele aparece, é navegável, e **não é
   selecionável** — a leitura de issues sabe dizer de qual projeto uma task veio,
   e não de qual board. Escolher um board seria ligar um interruptor desligado do
   resto, ou pior: esvaziaria a fila em silêncio, porque nenhuma task conseguiria
   provar que pertence ao escolhido. O motivo aparece na tela, literalmente.

2. **Escolher custa uma ida ao provedor.** `select_refs` refaz a descoberta para
   conferir. É o preço de o corpo da requisição não ser autoridade — e significa
   que, com o provedor fora do ar, **não dá para escolher** (a recusa é
   `SOURCE_UNAVAILABLE`, nunca `NOT_FOUND`).

3. **Credenciais existentes precisam ser registradas de novo.** Sem
   `repo.discover` / `task.discover` a busca recusa. O resto continua
   funcionando: ler o que já foi escolhido é outra permissão. Documentado no
   README com o comando.

4. **`ExternalTask.sources` só é preenchido pelo Jira.** Outros adapters de
   tasks passam pelo filtro sem serem cortados (ver §4).

5. **Sem paginação na tela.** Uma organização com centenas de repositórios
   renderiza tudo de uma vez; o filtro por nome é local. O adapter tem teto
   (`--limit`, `maxResults`), então o custo é de renderização, não de rede.

---

## 8. PRÓXIMA RECOMENDAÇÃO

**Fazer o board do Jira recortar trabalho** — é a limitação com mais
consequência, e a que mais gente vai encontrar: times organizam trabalho por
board, não por projeto.

O caminho: `JiraTasks.list_tasks` passa a aceitar um recorte por board no JQL
(`board = X` via `/rest/agile/1.0/board/{id}/issue`), e `_normalize` preenche
`sources` com o id do board. Nada acima do adapter muda — `SomenteSelecionados`
já cruza `sources` com o que foi escolhido, e a tela já sabe desenhar um recurso
selecionável. O `note` some sozinho quando `selectable` virar `True`.

Depois disso, **um terceiro provedor com três níveis** (ClickUp: workspace →
space → list) é o teste honesto da abstração — dois provedores podem caber numa
generalização acidental; três, dificilmente.

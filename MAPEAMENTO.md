# Mapeamento: sistema de tarefas → domínio

Como um `TaskProvider` traduz o vocabulário de fora para o vocabulário do motor.
Todo desvio está declarado. Onde não há equivalência perfeita, a escolha é a mais
simples — e está escrito que foi escolha.

Levantado contra o board real em **06/09/2026**. Nomes de status não foram
presumidos: foram lidos.

## Status → `SituacaoExterna`

O Core não conhece nome de status de ferramenta nenhuma. Ele conhece **posição no
ciclo de vida**, que todo sistema de trabalho tem.

| status na origem | situação interna | consequência no motor |
|---|---|---|
| `TO DO`, `BACKLOG` | `NAO_INICIADA` | disponível — pode ser despachada |
| `PLANNING` | `EM_ANALISE` | disponível |
| `CODING`, `IN PROGRESS` | `EM_EXECUCAO` | **bloqueada**: já tem alguém |
| `REVIEWING`, `IN REVIEW` | `EM_REVISAO` | bloqueada |
| `QA STAGING`, `QA PRODUCTION` | `EM_VALIDACAO` | bloqueada |
| `DONE` | `CONCLUIDA` | nem entra no motor |
| `CANCELLED`, `WON'T DO` | `CANCELADA` | nem entra |
| **qualquer outro** | `DESCONHECIDA` | **bloqueada** + anomalia relatada |

**Por que `DESCONHECIDA` não é coagida.** Status fora do mapa significa que
alguém mudou o processo. Mapeá-lo para o vizinho mais parecido faria o motor
trabalhar sobre uma premissa que ninguém verificou, em silêncio. Ele bloqueia e
avisa.

**Única exceção — rede de segurança por categoria.** A origem classifica todo
status em `new` / `indeterminate` / `done`, e essa classificação existe mesmo
para status que ninguém mapeou. Ela é usada só para `done` e `new`: evita o pior
erro possível — despachar trabalho já encerrado — sem fingir precisão no meio.
`indeterminate` continua `DESCONHECIDA`, porque "está no meio" não diz se é
código, revisão ou validação.

## Prioridade → inteiro

Menor roda antes. Espaçado de 20 em 20 para um planejador poder ajustar sem
colidir com o valor de origem.

| origem | interno |
|---|---|
| Highest, Blocker, Critical | 10 |
| High, Major | 30 |
| Medium, Normal | 50 |
| Low, Minor | 70 |
| Lowest, Trivial | 90 |
| ausente ou desconhecida | 50 |

Prioridade desconhecida cai no meio, e **não** vira anomalia: uma prioridade
estranha é opinião de quem escreveu o card, não defeito de dado.

## Vínculos → arestas do grafo

**Só `bloqueia` vira aresta.** Esta é a decisão de maior consequência do
mapeamento, e a mais fácil de errar.

| link na origem | tipo interno | vira dependência? |
|---|---|---|
| `Blocks` / *is blocked by* (inward) | `bloqueia` | **sim** |
| `Blocks` / *blocks* (outward) | `relacionado` | não — ver abaixo |
| `parent` (subtarefa) | `pai` | não |
| `Relates`, `Cloners`, `Problem/Incident` | `relacionado` | não |
| `Duplicate` | `duplica` | não |

**A direção.** Um link de bloqueio aparece nas duas issues, com pontas opostas.
`A is blocked by B` (inward) significa **A depende de B**. `A blocks B` (outward)
é o mesmo fato visto do outro lado — e a aresta correta pertence a B, que tem seu
próprio inward. Registrar os dois lados como bloqueio inverteria metade do grafo.

*Verificado contra os 28 links `Blocks` reais do board: nenhum invertido.*

**Por que hierarquia não bloqueia.** Uma subtarefa não espera a mãe terminar —
ela é parte do que a mãe é. No board real, **95 de 100** issues tinham mãe e
havia **229 vínculos não-bloqueantes contra 8 bloqueios**. Se hierarquia ou
relacionamento virassem dependência, nada no board seria executável.

## Recursos → exclusão mútua no scheduler

Um provedor de tasks **não sabe quais arquivos serão tocados**. Inventar isso
seria consertar o mundo. O que ele sabe é hierarquia, e ela é sinal real: duas
subtarefas da mesma mãe quase sempre mexem no mesmo código.

| `recursos_por` | chave emitida | efeito |
|---|---|---|
| `parent` (padrão) | `parent:<KEY>` | irmãs serializam, mães diferentes paralelizam |
| `project` | `project:<KEY>` | serializa o projeto inteiro |
| `nenhum` | — | não declara conflito |

**É uma heurística declarada.** Precisão de verdade exige um agente lendo o
código, e isso é o Marco 5.

## Responsável, rótulos, descrição

- **Responsável** → `responsavel`, o nome legível. Id interno da ferramenta não
  atravessa a porta.
- **Rótulos** → `rotulos`, preservados como texto. Não viram recurso nem
  prioridade: são contexto.
- **Descrição** → só no detalhe, nunca na listagem. Uma listagem de 100 issues
  com descrição completa devolveu **726 KB** na medição. `parcial=True` marca o
  registro que veio da listagem, para que "descrição vazia" e "descrição não
  pedida" não se confundam.

## Achados classificados

Seguindo a regra de não consertar o mundo no lugar errado:

| achado | classe | onde foi corrigido |
|---|---|---|
| Hierarquia e relacionamento viravam dependência | **CORE BUG** | `engine/orchestrator.py` — só vínculo bloqueante vira aresta |
| Motor despachava trabalho que já tinha gente | **CORE BUG** | `engine/orchestrator.py` — relevância decide antes da fila |
| Motor nunca relia mudança na origem | **MISSING CAPABILITY** | `engine/orchestrator.py` — `_atualiza` por passada |
| `estado_externo` era string livre e o Core não podia decidir | **MISSING CAPABILITY** | `ports/tasks.py` — `SituacaoExterna` |
| Adapter nunca pedia descrição | **MISSING CAPABILITY** | lista enxuta / detalhe completo |
| Nome de instantâneo usava `hash()` randomizado | **CORE BUG** (meu) | `hashlib`, nome estável entre processos |
| Issues sem descrição no board | **DATA QUALITY** | relatado como anomalia; nada corrigido |
| Paginação por cursor, não por offset | **PROVIDER QUIRK** | tratado no adapter |
| Resposta de 726 KB numa busca de rotina | **PROVIDER QUIRK** | campos enxutos, nunca `*all` |
| Dependência apontando para fora do recorte do JQL | **DATA QUALITY** | contada e relatada, nunca inventada |

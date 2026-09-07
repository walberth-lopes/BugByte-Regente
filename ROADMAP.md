# Roadmap

Regra: cada marco entrega **capacidade funcionando**, não estrutura para depois.
Marco não fecha sem prova em disco.

| # | Marco | Fecha quando |
|---|---|---|
| **1** | **Core, estado, policy, scheduler** ✅ | tick descobre, monta grafo, despacha em paralelo, sobrevive a `kill -9` e escala ao humano |
| 2 | Mission Control (UI local) | as 4 perguntas na tela; decidir um item da fila pelo navegador |
| **3** | **TaskProvider real** ✅ | adapter real lendo o board de verdade em sombra; contrato passa em dois provedores; zero mutacao |
| **4** | **RepositoryProvider** ✅ | dois provedores reais em sombra; identidade dentro da tenancy; contrato de escrita declarado, nada implementado |
| **5** | **AgentRunner + validation loop** ✅ | missão selecionada, alvo resolvido, clone isolado, agente executado, teste com linha de base, veredito e commit — DECLARED e DISCOVERED provados ponta a ponta |
| **6** | **CI + PR** ⚠ | caminho completo implementado e testado em contrato; **prova remota real bloqueada em `NEEDS_HUMAN`** por ausencia de task elegivel |
| **7** | **AgentRunner real** ⚠ | motor observa o mundo por conta propria e nao acredita no agente; sandbox sem execucao de comando; **modelo real bloqueado** por falta de credencial que o motor possa resolver |
| **8** | **Operacao continua sob falha** ✅ | 1000 ticks, 43 reinicios sem fechamento limpo, 13 mortes de worker e 13 recuperacoes, 142 quedas de provider, zero violacao de invariante |
| **9** | **Concorrencia real entre processos** ✅ | interpretadores separados sobre um SQLite: posse exclusiva, revalidacao no momento de agir, claim atomico, zero execucao duplicada em 60 rodadas com SIGKILL |
| **10** | **Isolamento entre clientes** ✅ | dois contextos completos com nomes locais IDENTICOS num so motor e banco: task, lease, policy, budget, secret e observabilidade sem cruzamento, sob processos concorrentes e SIGKILL dos dois lados |
| 11 | ReviewerAgent | parecer fixado no SHA revisado, com segunda passada em risco alto |
| 12 | CloudProvider | leitura de recursos, logs e métricas |
| 13 | Deploy em staging | deploy governado + smoke, com rollback |
| 14 | Escalonamento maduro | notificação fora do terminal; decisão de um clique |

O marco do segundo cliente era o único teste honesto da arquitetura, e foi feito
no marco 10 — com identificadores locais **iguais** nos dois clientes, que é a
contraprova forte. Falta ainda o mesmo com dois conjuntos de provedores REAIS
diferentes, o que depende de M6 e M7 saírem do bloqueio.

⚠ = capacidade pronta e provada em contrato, com a integracao real bloqueada por
uma dependencia externa nomeada. Ver `CAPABILITIES.md`: suite verde nunca
substitui prova real.

## Ordem das apostas

Adapter real (3, 4) vem **antes** de agente que escreve código (5). Motivo: um
agente escrevendo código contra provider falso não prova nada, e é o mais caro de
refazer se a abstração estiver errada.

## Freios, em toda fase

`sombra: true` nasce ligado. Desligar é decisão explícita, depois de o dono ler o
que o motor teria feito e responder sim à pergunta: *eu assinaria isso com meu
nome?*

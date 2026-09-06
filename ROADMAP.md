# Roadmap

Regra: cada marco entrega **capacidade funcionando**, não estrutura para depois.
Marco não fecha sem prova em disco.

| # | Marco | Fecha quando |
|---|---|---|
| **1** | **Core, estado, policy, scheduler** ✅ | tick descobre, monta grafo, despacha em paralelo, sobrevive a `kill -9` e escala ao humano |
| 2 | Mission Control (UI local) | as 4 perguntas na tela; decidir um item da fila pelo navegador |
| 3 | TaskProvider real | segundo adapter de tasks lendo o board de verdade, em sombra |
| 4 | RepositoryProvider real | branch, commit, push e PR pelo motor |
| 5 | CoderAgent | worker que escreve código de verdade em worktree isolado |
| 6 | CI + PR | motor acompanha checks e reage a vermelho |
| 7 | ReviewerAgent | parecer fixado no SHA revisado, com segunda passada em risco alto |
| 8 | CloudProvider | leitura de recursos, logs e métricas |
| 9 | Deploy em staging | deploy governado + smoke, com rollback |
| 10 | Workers paralelos de verdade | 2+ workers reais simultâneos sem colisão |
| 11 | Escalonamento maduro | notificação fora do terminal; decisão de um clique |
| 12 | **Segundo cliente** | outro conjunto de provedores rodando **sem tocar em `core/` nem `engine/`** |

O marco 12 é o único teste honesto da arquitetura. Os outros onze podem passar
com um motor secretamente acoplado ao primeiro cliente.

## Ordem das apostas

Adapter real (3, 4) vem **antes** de agente que escreve código (5). Motivo: um
agente escrevendo código contra provider falso não prova nada, e é o mais caro de
refazer se a abstração estiver errada.

## Freios, em toda fase

`sombra: true` nasce ligado. Desligar é decisão explícita, depois de o dono ler o
que o motor teria feito e responder sim à pergunta: *eu assinaria isso com meu
nome?*

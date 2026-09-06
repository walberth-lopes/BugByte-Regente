# Onde esta task roda?

Investigação do Marco 4. **Medido em 06/09/2026** contra 100 tasks reais e 12
repositórios reais — não estimado.

## O achado

**A informação está faltando, não escondida.**

| sinal | cobertura | serve? |
|---|---|---|
| campo de componente (o campo *natural* para isso) | **0/100** | não existe no board |
| branch existente citando a chave da task | **14/100**, 2 ambíguas | sim, quando existe |
| rótulo que cita nome de repositório | 72/100 casam `scamchecker` | **não** — ver abaixo |
| título que cita nome de repositório | 11/100 | não, ruidoso |
| projeto | 100/100, mas 1 projeto → 12 repos | não discrimina |

O rótulo parece promissor e não é: `scamchecker` é ao mesmo tempo o nome de **um**
repositório e o nome do **produto inteiro**, que tem doze. Casar por texto ali
trocaria "não sei" por "errei com confiança" — que num motor que vai escrever
código é infinitamente pior.

## O que foi implementado

Coleta de evidência com confiança declarada. **Nunca chute.**

```
DECLARADA  alguém afirmou explicitamente        (peso 100)
OBSERVADA  o mundo mostra trabalho já começado  (peso  50)
AMBIGUA    empate — o motor NÃO desempata
AUSENTE    nenhuma evidência — o motor não inventa
```

`DECLARADA` vence `OBSERVADA` porque uma branch pode ser resto de tentativa
abandonada, enquanto um mapa é afirmação de quem sabe. Empate vira pergunta ao
humano, não escolha.

Casamento de chave é por **palavra inteira**: `K-1` não casa com `K-11`. E nome
curto ambíguo entre dois repositórios não entra no índice — seria reintroduzir o
chute pela porta dos fundos.

## O contrato futuro entre TaskProvider e RepositoryProvider

O que falta não é código, é **dado declarado**. Em ordem de preferência:

1. **O provedor de tasks emite o alvo** — campo próprio, componente, ou convenção
   de rótulo. É a única fonte que não envelhece, porque quem escreve a task sabe
   onde ela roda. *Custo: uma decisão de processo, zero código no motor.*
2. **Mapa na configuração do workspace** (`por_rotulo`, `por_projeto`, `por_task`).
   Cobre o caso comum com zero adivinhação, e já está implementado.
3. **Agente de análise lê o código e propõe o alvo com evidência.** Caro, e por
   isso último — mas é o único que resolve task nova em repositório novo.

**Nenhum dos três exige mudar o Core.** `ExternalTask.recursos` e `dados` já
carregam o resultado, venha ele de onde vier.

## O que a capacidade faltante vale, medido

Mesmos 100 tasks, mesma evidência, mudando um eixo por vez:

```
teto de autonomia    L0 → PRECISA_HUMANO 1        L2 → CANDIDATO 1
mapa declarado       sem → AMBIGUO 2, CANDIDATO 1
                     com 1 entrada → AMBIGUO 1, CANDIDATO 2
```

Uma linha de mapa converteu uma ambiguidade em candidato executável, com a
evidência registrada:

```
SG-1195  DECLARADA
  SG-1195 -> silverguard-br/scamchecker-dashboard-api;
  'chore/SG-1195-remove-deploy-staging-obsoleto-da-main' cita SG-1195
  base=main  branch=regente/sg-1195
  recurso=repo:wks_sg/git-local/silverguard-br/scamchecker-dashboard-api
```

## A cadeia, e onde ela para

```
task → repositório candidato → contexto → branch base
     → recursos/isolamento → risco/policy → candidato a execução
```

Cada elo pode reprovar, e reprovar é normal. O valor está em dizer **em qual
elo** parou — "não há o que fazer", "não sei onde fazer" e "não posso fazer"
exigem ações opostas do dono, e um número único as esconderia.

Contra o board real, sem mapa declarado, em L0:

```
SEM_TRABALHO    36   a origem diz que alguém já está nela
SEM_ALVO        61   ← o gargalo: a declaração que falta
ALVO_AMBIGUO     2   o motor se recusa a desempatar
PRECISA_HUMANO   1   evidência boa, mas o teto de autonomia é L0
Mutações         0
```

**O gargalo não é o motor.** É a informação que ninguém declara.

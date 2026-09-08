# Marco Operacao — levantamento antes do codigo

Os marcos anteriores provaram que o motor **pode** agir com autoridade. Este
pergunta outra coisa:

> Uma pessoa consegue ligar o Regente e deixa-lo trabalhando?

Hoje a resposta e nao, e o motivo nao e falta de mecanismo. E que o mecanismo so
avanca quando alguem digita `regente tick`.

---

## 1. O que JA existe, e nao deve ser reimplementado

| peca | onde | estado |
|---|---|---|
| `TaskProvider` sem fornecedor no contrato | `ports/tasks.py` | pronto |
| `ExternalStatus` (7 valores + `UNKNOWN` que nunca e coagido) | `ports/tasks.py:14` | pronto |
| `ExternalTask` com `priority`, `labels`, `project`, `data` | `ports/tasks.py:87` | pronto |
| Jira como adapter, com `STATUS_MAP` proprio | `adapters/tasks/jira.py:44` | pronto |
| Ordenacao **deterministica e estavel** | `core/scheduling.py:107` | pronto |
| Dependencia, conflito de recurso, ciclo, slots, teto diario | `core/scheduling.py` | pronto |
| Leases, posse, concorrencia entre processos | marcos 9 e 10 | pronto |
| Identidade, concessao, policy, credencial, transporte | marcos 13-16, 6, 6.1 | pronto |
| Read model + API + Mission Control | marcos 12-13 | pronto |
| Escrita humana pela UI (decidir escalada) | marco 13 | pronto |

A ordenacao ja e a que o marco pede, e ja esta documentada no proprio codigo:

```python
for c in sorted(candidates, key=lambda x: (x.priority, x.key or x.task_id)):
```

Prioridade primeiro (menor roda antes), chave como desempate. Estavel de
proposito -- sem isso, um empate faz o mesmo tick escolher tasks diferentes a
cada execucao, e o motor fica indo e voltando sem terminar nada.

**Nada disso sera refeito.** O que falta e outra coisa.

---

## 2. O que NAO existe

### 2.1 O motor nao continua sozinho

```
$ grep -rn "while True\|--continuous\|daemon" regente/cli.py regente/app/
(nada)
```

`regente tick` roda **um** ciclo e termina. Nao ha loop, nao ha sono entre
ciclos, nao ha heartbeat, nao ha desligamento limpo. A pessoa que quiser o
Regente trabalhando precisa ficar digitando -- ou escrever o proprio loop, que e
pedir para ela reimplementar orcamento, lease e recuperacao por fora.

### 2.2 O motor nao tem estado de operacao

Nao existe `RUNNING`, `PAUSED`, `STOPPED`. Existe estado de *task* e estado de
*run*; nao existe estado do **processamento**. Consequencia direta: a UI nao tem
o que mostrar nem o que controlar, e nao ha como distinguir

```
o servidor HTTP da UI esta vivo
```

de

```
o motor esta processando
```

que o marco nomeia explicitamente como duas coisas.

### 2.3 O mapeamento de status mora no adapter, nao no workspace

```python
STATUS_MAP = {"TO DO": NOT_STARTED, "CODING": IN_PROGRESS, ...}
```

Uma constante de modulo. O board real desta organizacao tem `PLANNING`,
`CODING`, `REVIEWING`, `QA STAGING` -- que so funcionam porque alguem os
escreveu ali. Um cliente com `Refinamento` / `Em desenvolvimento` cai em
`UNKNOWN`, e a unica saida hoje e editar codigo Python.

O adapter esta no lugar certo; a **fonte** do mapeamento e que nao esta. O
workspace precisa poder declara-lo.

### 2.4 Nao ha selecao nem priorizacao configuravel

`Task.priority` vem do `PRIORITY_MAP` do fornecedor e mais nada. Nao ha como
dizer "titulo contem FAXINA roda primeiro", nem separar

```
elegibilidade   esta task pode ser pega?
prioridade      em que ordem, entre as que podem?
```

As duas hoje sao a mesma coisa: o estado externo decide se e elegivel, e a
prioridade do fornecedor decide a ordem.

### 2.5 A API e somente leitura, com tres escritas nomeadas

```
POST .../approvals/{id}/decision      decidir escalada     (M13)
POST|DELETE .../access                conceder / revogar   (M14)
POST|DELETE .../credentials           registrar / revogar  (M15)
tudo o mais  -> 405 read_only
```

Nao ha rota para configurar provider, mapear status, definir prioridade nem
controlar o motor. A recusa e deliberada e bem escrita -- "autoridade de escrita
pertence ao motor e ao humano, nao a uma tela" -- e continua valendo: o que
falta nao e afrouxa-la, e **nomear as escritas que faltam**, uma a uma, pelo
mesmo caminho das tres que ja existem.

---

## 3. Os dois bloqueios externos continuam

1. **Nenhuma credencial de Jira** que o motor consiga resolver. O board tem 50
   issues abertas de verdade -- verificadas pelo conector, que e uma sessao
   minha e nao do motor. O Regente nao alcanca nenhuma.
2. **Nenhum agente de modelo autenticado.**

Nao serao contornados. O que da para fazer sem eles: tudo o que nao depende de
ler o board real -- e isso e a maior parte deste marco.

---

## 4. O desenho, e a decisao que ele forca

### Quem manda o motor rodar?

A tentacao e o botao `Iniciar` da UI subir um processo. Isso daria a uma tela o
poder de criar processos no computador de alguem -- exatamente a "autoridade
paralela" que o marco proibe, e que os marcos 13 a 16 existiram para eliminar.

O desenho honesto separa **intencao** de **execucao**:

```
UI / CLI  --escreve-->  estado desejado (RUNNING | PAUSED | STOPPED)
                              |
                        [identidade -> concessao -> capacidade -> policy]
                              |
processo `regente run`  --le-->  obedece, e publica heartbeat
```

A UI nao inicia processo nenhum. Ela grava uma **intencao**, pelo mesmo caminho
que a decisao humana ja percorre. Quem executa e um processo que alguem iniciou
-- e se nao houver nenhum, a UI mostra isso em vez de mentir.

Isso da de graca a distincao que o marco pede: `RUNNING` desejado sem heartbeat
recente e `DEGRADED`, e nao `RUNNING`.

### Elegibilidade e prioridade sao perguntas diferentes

```
eligibility   booleana, e um filtro
priority      um numero, e uma ordem
```

Uma task pode ser elegivel e ter prioridade baixa. Colapsar as duas faria
"despriorizar" virar "esconder", que e como trabalho some de um board sem
ninguem perceber.

A ordenacao final continua sendo a que ja existe -- `(prioridade, chave)` --
porque ela ja e deterministica e estavel. As regras do workspace mudam o
**numero**, nunca o criterio de desempate.

---

## 5. O que este marco faz

1. Estado de operacao persistido, com transicoes governadas.
2. `regente run` continuo: tick, sono, heartbeat, desligamento limpo.
3. Mapeamento de status declarado pelo workspace, com o adapter como fronteira.
4. Regras de elegibilidade e prioridade, puras e deterministicas.
5. As escritas que faltam na API, nomeadas uma a uma, e a UI que as usa.

E o que ele **nao** faz: servico do Windows, daemon systemd, instalador,
executavel nativo. O processo Python e a unidade portatil, e empacota-lo e
camada de fora.

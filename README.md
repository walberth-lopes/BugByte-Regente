# Regente

Sistema operacional para agentes de engenharia de software.

O agente trabalha. O Orchestrator coordena. As ferramentas executam. As policies
protegem. A fila chama você **só quando a resposta não existe dentro do sistema**.

Não é um chatbot que sabe programar.

## Estado

**Marco 1** — descoberta, grafo de dependências, scheduler paralelo, workers
isolados, estado persistente, detecção de falha, escalonamento humano e retomada
após `kill -9`.

**Marco 3** — primeiro provedor real de tasks, em sombra: o motor lê um board de
verdade, normaliza, monta o grafo e planeja, **sem autoridade para mutar nada**.

**Marco 4** — provedor de repositórios, em sombra: dois adapters reais de tipos
opostos (git local e hospedagem remota), identidade dentro da tenancy, e a cadeia
`task → repositório → base → recursos → risco/policy → candidato` respondida sem
tocar em nada.

**Marcos 8–11** — operação contínua sob falha, concorrência real entre
processos, isolamento entre clientes, e o ciclo da task fechado até onde a
autoridade do motor termina: veredito, push idempotente, pull request, CI
observado — e então uma pessoa.

**Marcos 12–13** — Mission Control: o estado do motor numa tela, e a primeira
escrita humana pelo navegador, atravessando as mesmas barreiras do terminal.

**Marcos 14–16** — a cadeia de autoridade fechada:

```
Identidade real → concessão gravada → policy → credencial → capacidade
                → segredo → ambiente do filho → subprocesso → auditoria
```

O último elo é do marco 6.1: autorizar não é entregar. Enquanto a ferramenta
externa se autenticava sozinha pelo chaveiro do sistema, a barreira podia
recusar sem mudar nada no mundo.

Identidade humana com procedência verificável; acesso concedido por alguém, com
data e revogação; credenciais com capacidades explícitas e validade; e **uma
única rota** até material secreto — o caminho antigo, em que um adapter recebia
um resolvedor e usava a referência que quisesse, deixou de existir.

Ver [ROADMAP.md](ROADMAP.md), [ARCHITECTURE.md](ARCHITECTURE.md),
[MAPEAMENTO.md](MAPEAMENTO.md) e [ALVO.md](ALVO.md).

## Sombra: ver sem tocar

```bash
regente sombra
```

Descobre, normaliza, monta o grafo e mostra o que o motor faria — sem escrever
uma linha em lugar nenhum. A garantia não é disciplina: o transporte de leitura
**não tem verbo de escrita**. Ligar escrita exige adicionar um método, o que
aparece num diff e passa por revisão — não um `if` que alguém desliga sem
querer.

## Instalar

```bash
git clone https://github.com/walberth-lopes/BugByte-Regente.git Regente
cd Regente
uv venv --python 3.13
```

Ative o ambiente — `.venv\Scripts\Activate.ps1` no PowerShell,
`source .venv/bin/activate` no Linux e macOS — e instale:

```bash
uv pip install -e ".[dev]"
```

Sem ativar, `regente` não existe no PATH. O tutorial abaixo detalha isso, com a
linha certa para cada terminal.

## Tutorial: do zero até a primeira decisão

Se você nunca abriu este projeto, faça só isto, na ordem. Cada passo diz o que
você deve ver e o que fazer quando o Regente disser não — e ele vai dizer não
várias vezes, de propósito.

O caminho inteiro roda **na sua máquina**, em modo sombra, sem tocar em nada de
ninguém.

### 1. Instalar

Você precisa de [Python 3.13](https://www.python.org/downloads/), do
[git](https://git-scm.com/downloads) e do
[uv](https://docs.astral.sh/uv/getting-started/installation/). Instale os três
antes de continuar; o resto é o Regente.

Baixe o código e entre na pasta:

```bash
git clone https://github.com/walberth-lopes/BugByte-Regente.git Regente
cd Regente
```

Crie o ambiente isolado do projeto e **ative-o**:

```bash
uv venv --python 3.13
```

A ativação é diferente em cada terminal. Use a linha do seu:

| terminal | comando |
|---|---|
| PowerShell (Windows) | `.\.venv\Scripts\Activate.ps1` |
| Prompt de comando (Windows) | `.\.venv\Scripts\activate.bat` |
| Git Bash (Windows) | `source .venv/Scripts/activate` |
| Linux / macOS | `source .venv/bin/activate` |

O prompt passa a começar com `(Regente)`. Agora instale:

```bash
uv pip install -e ".[dev]"
```

Confira que funcionou:

```bash
regente --help
```

Se aparecer a lista de comandos, está pronto.

> **`'regente' is not recognized` / `command not found`?**
> O ambiente não está ativo. Isso é normal: a ativação vale **só para a janela
> de terminal em que você a rodou** — abriu outra, ative de novo. Rode a linha
> de ativação da tabela acima e tente outra vez.
>
> Se preferir não ativar nada, todo comando deste tutorial também funciona
> prefixado com `uv run`, a partir da pasta do projeto:
> `uv run regente --help`.
>
> No PowerShell, se a ativação for barrada por política de execução, rode
> `Set-ExecutionPolicy -Scope Process RemoteSigned` nessa mesma janela e tente
> de novo.

### 2. Criar uma pasta de trabalho

O Regente roda dentro de uma pasta que tem um `regente.yaml`. Crie uma nova —
não use a pasta do código-fonte, para o trabalho não se misturar com o motor:

```bash
mkdir meu-regente
cd meu-regente
regente init
```

Isso cria o `regente.yaml` e uma pasta `tasks/`.

O ambiente continua ativo depois do `cd` — é a janela do terminal que está
ativada, não a pasta. Daqui em diante todos os comandos rodam **dentro de
`meu-regente`**.

### 3. Ver se o motor sobe

```bash
regente doctor
```

Ele testa cada aposta do ambiente **por comando, não por suposição**: o banco
abre, cada adapter constrói, as policies carregam. Se algo falhar, a mensagem
diz o quê — e é isso que você conserta antes de seguir.

### 4. Escrever uma tarefa

Uma tarefa é um arquivo. Crie `tasks/MINHA-1.yaml`:

```yaml
key: MINHA-1
title: Corrigir o texto do rodapé
status: TO DO
```

### 5. Rodar o primeiro ciclo

```bash
regente tick
```

Você vai ver algo como `baseline com 1 tasks; nada despachado`.

**Isso está certo.** O primeiro ciclo apenas *registra* o que existe. Ligar o
motor num board de cinquenta tarefas e deixá-lo despachar tudo de uma vez seria
um incidente, não um produto. Rode `regente tick` de novo para ele começar a
trabalhar.

### 6. Ver o que está acontecendo

```bash
regente status
```

Responde quatro perguntas: o que está rodando, o que precisa de você, o que
travou, o que terminou.

```bash
regente health
```

Responde treze, incluindo as desconfortáveis — tarefas paradas, leases órfãos,
execuções presas. Ele **não fica verde** para agradar: se algo está parado há
horas, ele diz, com nome e há quanto tempo.

### 7. Quando o motor precisa de você

Um agente que falha duas vezes esgota a escada de recuperação e o motor
**escala**: ele para e pergunta.

```bash
regente needs-me
```

Você vê o que aconteceu, por que importa, o que já foi tentado, e as opções.
Para responder:

```bash
regente decide apv_abc123 investigar
```

E aqui vem o primeiro "não" que você vai encontrar:

```
NOT_FOUND: recurso nao encontrado neste escopo
```

Isso não é um erro. É o Regente dizendo que **você ainda não recebeu acesso**.

### 8. Acesso: quem é você, e quem autorizou

O Regente separa três perguntas que a maioria dos sistemas mistura:

| pergunta | quem responde |
|---|---|
| **quem é você?** | sua conta do sistema operacional |
| **você manda neste workspace?** | uma concessão gravada, com autor e data |
| **esta ação é permitida?** | o arquivo de policies |

Você já tem a primeira. Veja:

```bash
regente access quem-sou-eu
```

```
identidade : os-account:S-1-5-21-...
emissor    : SEU-COMPUTADOR
pode aqui  : nada
```

Autenticado, e sem autoridade nenhuma. A primeira concessão de um workspace novo
sai por uma porta estreita, que só funciona uma vez:

```bash
regente access inicial
```

Agora `regente decide` funciona. Confira quem tem acesso:

```bash
regente access listar
```

Para dar acesso a outra pessoa (conceder a si mesmo é recusado — uma concessão
só vale como prova se houver duas pessoas na linha):

```bash
regente access conceder os-account:S-1-5-21-outra-pessoa --papel operator
regente access revogar os-account:S-1-5-21-outra-pessoa
```

Papéis: `operator` decide escaladas · `admin` administra pessoas ·
`keeper` administra credenciais · `owner` faz tudo.

Revogar **não apaga o histórico**: continua registrado quem concedeu, quando, e
quem tirou.

### 9. A tela

```bash
regente ui
```

Abra o endereço que ele imprimir. Você vê o painel, as tarefas, a saúde, a fila
de decisões — e pode decidir pelo navegador. Tudo passa exatamente pelas mesmas
barreiras do terminal.

A tela escuta só no seu computador, e o mecanismo de identidade dela se anuncia
como **de desenvolvimento**. Não exponha na rede: ela recusa, e a recusa está no
código, não num aviso.

### 10. Credenciais, quando você conectar coisas de verdade

Para ler um board real ou abrir um pull request, o Regente precisa de uma
credencial. Ele **nunca guarda o segredo** — guarda o endereço dele:

```bash
regente credentials registrar principal \
    --provider repository_write \
    --referencia helper:github \
    --capacidades repo.read \
    --dias 30
```

Três formas de endereço:

| forma | onde o segredo está |
|---|---|
| `env:NOME` | numa variável de ambiente |
| `arquivo:CAMINHO` | num arquivo protegido |
| `helper:NOME` | em lugar nenhum — um programa o produz na hora |

Para provar que funciona, sem revelar nada:

```bash
regente credentials testar --provider repository_write --uso repo.read
```

```
autorizado pelo Regente : True
resposta do provedor    : AUTHENTICATED
capacidade suportada    : True
utilizavel              : True
```

Quatro respostas separadas, porque são quatro fatos diferentes. Se você pedir
uma capacidade que **a credencial** não tem, ele recusa — mesmo que o token
tecnicamente consiga:

```
detalhe : a credencial existe e nao autoriza 'repo.push'; autoriza ['repo.read']
```

E para tirar de circulação:

```bash
regente credentials revogar crd_abc123
```

A porta fecha na chamada seguinte, sem reiniciar nada.

### O que esperar

O Regente diz não com frequência, e quase sempre a resposta certa é olhar o
motivo — ele é específico. `NOT_FOUND` quer dizer que falta concessão.
`POLICY_DENIED` quer dizer que falta regra no arquivo de policies. `REVOKED` e
`EXPIRED` querem dizer coisas diferentes e pedem ações diferentes.

Nada disso é excesso de zelo. É a diferença entre um sistema que trabalha por
você e um que age em seu nome sem você saber.

## Referência de comandos

```bash
regente init      # cria regente.yaml e a pasta tasks/
regente doctor    # prova que o motor sobe: banco, adapters, policies
regente tick      # roda um ciclo
regente status    # o que está acontecendo, o que precisa de você
```

| comando | o que faz |
|---|---|
| `init` | cria a configuração inicial |
| `doctor` | prova cada aposta do ambiente por comando, não por suposição |
| `tick` | um ciclo: recupera, descobre, analisa, planeja, despacha, colhe |
| `status` | as quatro perguntas: o que roda, o que precisa de você, o que travou, o que terminou |
| `plan` | o que o scheduler faria agora — sem executar |
| `needs-me` | a fila de decisões humanas, com briefing |
| `decide` | registra sua decisão num item da fila |
| `log` | a trilha: toda transição, com ator e motivo |
| `sombra` | vê o trabalho real e o que o motor faria, sem tocar em nada |
| `repos` | repositórios visíveis, como o motor os enxerga |
| `cadeia` | da task real ao candidato a execução, elo por elo |
| `rules` | regras, limites e adapters em vigor |
| `health` | as treze perguntas de saúde, respondidas do estado persistido |
| `ui` | Mission Control: o estado do motor numa tela, no seu computador |
| `access` | quem pode agir neste workspace, quem concedeu e quando |
| `credentials` | credenciais de provider: endereço, capacidades, validade |
| `mission` | seleciona uma task, mostra o briefing, opcionalmente executa |

## O primeiro tick é baseline

Ligar o motor num backlog cheio **registra** o trabalho e não despacha nada. Sem
isso, o primeiro contato com um board de cinquenta tasks vira uma tempestade de
workers — e um incidente em vez de um produto.

## Sombra antes de valendo

`sombra: true` nasce ligado. Nesse modo o motor decide, registra e mostra, mas
não executa escrita externa. Desligar é decisão explícita, depois de você ler o
que ele *teria* feito e responder sim a: *eu assinaria isso com meu nome?*

## Trocar de fornecedor

Uma linha no `regente.yaml`:

```yaml
providers:
  tasks:
    name: filesystem     # o adapter, por nome
    directory: ./tasks
```

`regente rules` lista o que está disponível. Adicionar um provedor é adicionar
uma entrada em `adapters/registry.py` — se algum dia exigir mexer em `core/` ou
`engine/`, a abstração falhou, e `tests/test_boundaries.py` acusa.

## Testes

```bash
pytest
```

Inclui o teste de fronteira, que lê o código-fonte e falha se o domínio importar
I/O ou se um nome de ferramenta vazar para o núcleo.

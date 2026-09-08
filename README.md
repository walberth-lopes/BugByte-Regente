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
                → segredo → ambiente do filho → git push → PR → CI → humano
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
> `uv run regente --help`. E `python -m regente --help` funciona sempre que o
> pacote estiver instalado, ativado ou não.
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

Isso cria três coisas:

| arquivo | o que é |
|---|---|
| `regente.yaml` | onde ficam os providers, os limites e o modo |
| `policies.yaml` | o que o motor pode fazer, e o que não pode |
| `tasks/` | onde você descreve o trabalho, em YAML |

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

### 11. Deixar o Regente trabalhando

Até aqui você rodou `regente tick` à mão. Ninguém quer fazer isso para sempre.

O processamento tem um **estado próprio**, separado do estado das tasks:

```bash
regente engine estado
```

```
fase       : STOPPED
o que e    : ninguem pediu para rodar; use `regente run` para iniciar
intencao   : STOPPED
processo   : nenhum sinal de vida
```

São duas coisas diferentes, e o Regente nunca as confunde:

| | |
|---|---|
| **intenção** | o que *você pediu*. Fica gravada, e sobrevive a reinício. |
| **processo** | quem está *de fato* executando ciclos. Publica sinal de vida. |

Ligue e deixe rodando:

```bash
regente engine iniciar --intervalo 60
regente run
```

O `regente run` roda ciclos até mandarem parar. Ele relê sua intenção a cada
volta — então você pode pausar de outro terminal, ou pela tela, sem matar
processo nenhum:

```bash
regente engine pausar     # continua vivo, para de despachar
regente engine retomar
regente engine parar      # o processo termina o ciclo atual e sai limpo
```

`Ctrl+C` também funciona: o ciclo atual termina, o sinal de vida é apagado, e a
intenção gravada continua valendo — reiniciar `regente run` volta de onde parou.

**As seis fases, e o que cada uma quer dizer:**

| fase | significa |
|---|---|
| `STOPPED` | ninguém pediu para rodar |
| `STARTING` | pediram; o processo ainda não deu sinal de vida |
| `RUNNING` | há processo trabalhando |
| `PAUSED` | processo vivo, deliberadamente sem despachar |
| `STOPPING` | parada pedida; um processo ainda está terminando |
| `DEGRADED` | **pediram para rodar e ninguém apareceu** |

`DEGRADED` é a mais importante: é a única que pede ação sua. Ela existe porque
"a tela está aberta" e "o motor está trabalhando" são fatos diferentes, e um
painel que mostrasse `RUNNING` só porque alguém clicou em Iniciar estaria
afirmando algo sobre o mundo com base num pedido.

### 12. Fazer isso tudo pela tela

```bash
regente ui
```

No topo do painel há um bloco **processamento** com a fase, o processo (pid e
máquina), e quatro botões: `Iniciar`, `Pausar`, `Retomar`, `Parar`.

Os botões **gravam intenção** — eles não iniciam processos. Quem executa
continua sendo um `regente run` que você deixou rodando. Uma página web que
pudesse criar processos na sua máquina seria uma autoridade paralela, e é
exatamente isso que o Regente não tem.

Se você ainda não recebeu `workspace.engine.control` neste workspace, os botões
não aparecem — e a tela diz qual comando resolve.

### 13. Escolher o que roda primeiro

> Tudo desta seção e da próxima também é configurável **pela tela**, sem editar
> arquivo — veja o passo 15. O YAML continua valendo como base versionável.

Por padrão a ordem é a prioridade que veio do board, com a chave como desempate
estável. Você pode somar regras suas no `regente.yaml`:

```yaml
selecao:
  - nome: faxina primeiro
    campo: title
    compara: contains
    valor: "FAXINA"
    delta: -100          # negativo roda ANTES

  - nome: so backend
    campo: labels
    compara: contains
    valor: backend
    efeito: require      # nada fora disto é elegível

  - nome: nunca risco legal
    campo: title
    compara: contains
    valor: "LEGAL RISK"
    efeito: exclude      # vence qualquer require
```

Duas perguntas separadas, de propósito:

* **elegibilidade** — esta task *pode* ser pega? (`require` / `exclude`)
* **prioridade** — entre as que podem, qual antes? (`delta`)

Uma task pode ser elegível e ter prioridade baixa. Se as duas fossem a mesma
coisa, "despriorizar" viraria "esconder" — e é assim que trabalho some de um
board sem ninguém perceber. Uma task excluída por regra **continua aparecendo**,
como adiada, com o nome da regra que a excluiu.

Campos disponíveis: `title`, `key`, `project`, `status`, `labels`, `priority`,
`assignee`, `type`, `components`. Comparações: `contains`, `equals`,
`starts_with`, `in`, `lt`, `gt`. Um campo ou uma comparação que não existam
fazem o Regente recusar a configuração, com a lista do que existe — nunca uma
regra que silenciosamente nunca casa.

### 14. Conectar um board de verdade

O Regente não assume que o seu board usa `TO DO` / `IN PROGRESS` / `DONE`. Você
declara o que os seus status significam:

```yaml
providers:
  tasks:
    name: jira
    site: https://suaempresa.atlassian.net
    jql: "project = ABC AND statusCategory != Done"
    status_map:
      available:   ["Refinamento", "To Do"]
      andamento:   ["Em Desenvolvimento"]
      revisao:     ["Em Revisão"]
      done:        ["Pronto"]
      ignorado:    ["Cancelado"]
```

Um status que não estiver em lugar nenhum fica **`UNKNOWN`** e a task não é
pega. Isso é de propósito: um status novo no board significa que alguém mudou o
processo, e adivinhar o vizinho mais conveniente faria o motor pegar trabalho
que a equipe tirou da fila.

Depois registre a credencial (passo 10) e prove:

```bash
regente credentials testar --provider tasks --uso task.read
```

### 15. Configurar tudo pela tela

A partir daqui você **não precisa mais editar YAML**. Abra a Mission Control e
vá em **configuração**:

```bash
regente ui
```

A página começa com um checklist do que falta:

```
CONCLUIDO   Conectar tasks          filesystem pronto
PENDENTE    Mapear status           opcional: sem isto valem os nomes que o adapter conhece
PENDENTE    Definir prioridade      opcional: sem regra, vale a prioridade da origem
BLOQUEADO   Conectar agente         nenhuma credencial de modelo está configurada
PENDENTE    Iniciar processamento   ninguém pediu para rodar
```

`BLOQUEADO` aparece de propósito. É melhor do que um botão `Iniciar` que
simplesmente não funciona.

Abaixo, **conexões** mostra o estado real de cada provider — e nunca diz
"conectado" só porque existe configuração:

| estado | quer dizer |
|---|---|
| `pronto` | há credencial viva com capacidade (ou o adapter não precisa de uma) |
| `sem credencial` | configurado, e não funciona |
| `credencial revogada` / `expirada` | funcionava, e parou |
| `não configurado` | não está declarado neste workspace |

O botão `testar` prova a credencial contra o provedor de verdade e devolve
**quatro fatos separados**: autorizado pelo Regente, resposta do provedor,
capacidade suportada, utilizável. Não existe "erro de conexão" — "a credencial
não serve" e "não deu para perguntar" mandam você fazer coisas diferentes.

### 16. De onde veio cada configuração

O `regente.yaml` continua sendo a base. A tela grava uma **sobreposição**, e
cada bloco diz de onde o valor está vindo:

```
arquivo   'providers' vem do regente.yaml
tela      'selection' foi definido pela tela
tela      'status_map' foi definido PELA TELA e substitui o que está no
          regente.yaml. Editar o arquivo não muda nada enquanto esta
          sobreposição existir — remova-a para o arquivo voltar a valer
```

A terceira linha é a que importa. Sem ela você editaria o arquivo, nada
aconteceria, e a conclusão razoável seria que o Regente está quebrado. O botão
`devolver ao arquivo` remove a sobreposição daquele bloco.

Tudo isso também funciona pelo terminal, pelo mesmo serviço:

```bash
regente config mostrar -v
regente config definir selection --de minhas-regras.json
regente config remover status_map
```

### 17. Prévia da fila

Antes de ligar o motor, a página mostra o que ele escolheria — **reavaliando com
as regras de agora**, não com o veredito do último ciclo:

```
TASK    TÍTULO                        STATUS   ESTADO   PRIORIDADE  POR QUE
SG-1    [FAXINA SC] limpar o rodapé   TO DO    READY    0           faxina primeiro: -100
SG-2    Automatizar chaves-pix        TO DO    READY    100         prioridade da origem
FORA    SG-3                                                        fora por "nada de morto"
```

Nada é executado aqui — é leitura. E uma task excluída por regra **continua
aparecendo**, com o nome da regra que a excluiu: trabalho que some sem
explicação é como um board perde tarefas sem ninguém perceber.

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
regente tick      # roda UM ciclo
regente run       # roda ciclos até mandarem parar
regente status    # o que está acontecendo, o que precisa de você
```

Tudo também funciona como `python -m regente ...` e `uv run regente ...`.

| comando | o que faz |
|---|---|
| `init` | cria a configuração inicial |
| `doctor` | prova cada aposta do ambiente por comando, não por suposição |
| `tick` | um ciclo: recupera, descobre, analisa, planeja, despacha, colhe |
| `run` | ciclos contínuos: obedece a intenção gravada, sobrevive a falha, sai limpo |
| `engine` | `estado`, `iniciar`, `pausar`, `retomar`, `parar` — grava intenção, não cria processo |
| `config` | `mostrar`, `definir`, `remover` — a mesma configuração que a tela edita |
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

# Marco: conectar em um clique

> Chego, conecto o GitHub, escolho meus repositórios, e o Regente trabalha.
> Tudo o que estiver entre a pessoa e isso é defeito.

---

## O que estava errado

O marco anterior construiu o encanamento — descobrir, escolher, filtrar — e
chamou aquilo de funcionalidade. Do lugar de quem usa, **nada tinha mudado**:

```
Conexões    → escolher um adapter, digitar a organização
Credenciais → registrar uma credencial, digitando `helper:gh`
Integrações → voltar, apertar "buscar", só então escolher
```

Cinco passos em três lugares para dizer *"use o meu GitHub"*. E cada um deles
era uma decisão que o Regente podia ter tomado sozinho.

Pior: a aba chamada **Integrações** não integrava nada. Ela mostrava um estado
vazio e mandava para outra aba — com uma palavra que, para quem usa, significa a
mesma coisa.

## O que existe agora

Um botão. Ele abre o navegador se faltar autorização, pergunta de qual conta, e
traz a lista. **Buscar deixou de ser um botão**: quem acabou de conectar quer
ver, não procurar.

```
Conectar GitHub  →  navegador (se preciso)  →  qual conta?  →  28 repositórios
                                                                      ↓
                                                            você marca os seus
```

Um pedido grava três coisas, todas pelo caminho de sempre:

| o quê | por onde |
|---|---|
| `providers.repository = {name: github, org: …}` | `SettingsService` |
| credencial `helper:gh` com `repo.discover, repo.read` | `CredentialService` |
| o motor autorizado a usar a conexão nos ciclos | `AccessService` |

No terminal, o mesmo serviço:

```bash
regente conectar github --conta silverguard-br
```

### Por que pelo `gh`, e não por OAuth próprio

Um fluxo OAuth exigiria um aplicativo registrado, um `client_id`, uma URL de
retorno e um `client_secret` que, num programa distribuído, não é segredo
nenhum. O `gh` já fez tudo isso: tem o próprio aplicativo, faz o fluxo no
navegador, e guarda o resultado no chaveiro do sistema.

**Nenhum segredo novo entrou no Regente.** O que fica gravado é a referência
`helper:gh`; o material é pedido no momento do uso, como sempre foi. Para isso
existir sem ninguém editar YAML, `helper:gh` virou um **ajudante embutido** —
comando fixo no código, que nem a configuração pode redefinir.

Jira e ClickUp precisam de um app OAuth registrado por você. O fluxo é o mesmo
depois disso, e a Mission Control já roda em `127.0.0.1` para receber o retorno.

---

## Três defeitos reais que só apareceram contra o mundo

Nenhum destes tinha teste. Todos apareceram na primeira conexão de verdade — e
os três são a mesma família: **algo decidido cedo demais.**

### 1. A descoberta nunca poderia ter funcionado

`repo.discover` foi criada como capacidade de credencial no marco anterior e
**nunca foi declarada na policy**. Toda descoberta contra um provedor real
recusava com *"nenhuma regra permite `repo.discover`"*. A suíte inteira e a
varredura de mutação passaram, porque o provedor de teste não resolve
credencial.

→ Regra `*.discover` na policy enviada, teto `L0` no `REQUIRED_LEVEL`, e um
guard geral: **para cada valor de `Use`, a policy enviada precisa permitir a
ação de mesmo nome**.

### 2. A porta de descoberta era do motor, não de quem clicou

Construída uma vez na composição, com a identidade de serviço do motor. Uma
pessoa clicava e quem chegava ao broker era o motor. Errado nas duas direções:
uma pessoa com autoridade era barrada, e — se o motor tivesse concessão — uma
pessoa **sem** `workspace.credential.use` faria o motor usar credencial por ela.

→ `discovery_for` virou uma **fábrica** `(provedor, ator) -> porta`. O tick
continua agindo como o motor, porque quem pede lá é ele.

### 3. A configuração era a de quando a tela subiu

Mesmo erro com outra roupa: `cfg.providers` lido uma vez no `serve()`. Quem
conectava o GitHub pela interface ficava olhando uma página que ainda achava que
não havia provedor nenhum.

→ `providers_efetivos()` é lido **a cada pedido**, e `discovery_trees` virou uma
função. Um processo que fica de pé por horas não pode guardar decisão que muda
no meio.

---

## A queixa que virou funcionalidade

> *"Cada atualização que tiver, se vier algo desse tipo é mais uma coisa na
> documentação, mais um problema para o user e assim vai."*

Estava certo. O `policies.yaml` é escrito uma vez, no `init`, e nunca mais — e
deve ser assim: o arquivo é da pessoa, e trocar de versão não pode ampliar
autoridade sozinho. O preço era uma linha de documentação por marco, e quem não
lesse ficava com uma recusa **por omissão**: a policy não diz não, ela nunca
ouviu falar da ação.

**`doctor` agora detecta**, e a checagem é geral — lida dos enums, não de uma
lista escrita à mão:

```
FALHA  policy em dia   policies.yaml nao conhece 4 acao(oes) desta versao:
                       repo.discover, task.discover, workspace.resource.select…
                       Ponha em dia com: regente atualizar
```

**`regente atualizar`** mostra o que falta *com a razão de cada regra*, e só
escreve depois de `--aplicar`. Ele acrescenta, nunca remove — um `DENY` da
organização continua valendo.

Uma policy que **nega** explicitamente está em dia: alguém decidiu aquilo.
Detectar virou automático; ampliar continua sendo uma decisão.

---

## O que não mudou

A cadeia inteira. `ConnectService` não grava nada por conta própria: ele pergunta
ao conector o que falta e manda gravar pelos dois serviços governados. Se um
deles recusar, conectar recusa.

- quem só pode configurar não registra credencial → recusa **antes** de escrever;
- a policy continua decidindo;
- a referência do segredo vem do **conector**, nunca do corpo da requisição;
- uma credencial antiga sem `repo.discover` é **conflito**, não sucesso — com o
  id junto, para a tela oferecer substituir.

A concessão ao motor não é silenciosa: passa pelo `AccessService` com quem
conectou como concedente, fica na trilha, e sai por `regente access revogar`.
Quando não dá para conceder, conectar **avisa** em vez de deixar a fila parar de
madrugada.

---

## Verificação

**1181 testes passam**, 6 pulados. `tests/test_conectar.py` é novo (26 testes).
Varredura de mutação: **12 de 12 mortas**.

Contra o **GitHub real**, do início ao fim:

| passo | resultado |
|---|---|
| `Conectar GitHub` na tela | conta pessoal + 4 organizações |
| escolher `silverguard-br` | conectado, sem aviso |
| lista | **28 repositórios reais** |
| marcar 2 | gravados com nome e data |
| `regente repos` (a visão do motor) | **exatamente os 2** |
| `regente doctor` | `tudo pronto` |

---

## LIMITAÇÕES

1. **Só o GitHub tem conector.** Board e agente continuam pela tela de Conexões.
   Jira e ClickUp dependem de um app OAuth registrado por você.
2. **Fora do Windows, autorizar mostra o comando** em vez de abrir um console.
   Não há forma portátil de dar um terminal a um processo interativo; a tela é
   honesta sobre isso em vez de ter um botão que finge.
3. **`gh` precisa estar instalado.** Quando não está, o passo é "instale", com o
   comando da plataforma — e não uma falha.
4. **Board do Jira ainda não recorta trabalho** (do marco anterior, sem mudança).

## PRÓXIMA RECOMENDAÇÃO

**Um conector para o agente.** É o terceiro da frase — *"conecto o GitHub,
conecto o board, conecto o agente"* — e é o mais barato: `claude` e `codex` são
CLIs locais, então conectar é detectar que estão instalados e gravar o provider.
Nenhum navegador, nenhuma credencial, nenhum app OAuth. Depois dele, a frase
inteira é verdade para quem usa Jira ou arquivos locais como board.

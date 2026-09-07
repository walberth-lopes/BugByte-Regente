# Marco 6 — levantamento antes do codigo

O M6.1 provou o transporte de credencial ate um subprocesso, e nomeou o que
ficava de fora: os subprocessos `git`. Este marco fecha isso e o caminho
operacional inteiro.

O levantamento encontrou **duas coisas que ninguem esperava**, e as duas estao
no caminho que o marco existe para fechar.

---

## 1. O que ja existe, e nao deve ser reimplementado

| peca | onde | estado |
|---|---|---|
| policy + identidade + SHA revalidados por mutacao | `engine/remote.py` | pronto (M11) |
| reconciliacao contra o remoto antes de repetir | `RemoteDelivery.reconcile` | pronto |
| adocao de PR: marcador **e** head, ou recusa | `_refuse_or_adopt` | pronto |
| CI perguntado pelo **SHA**, nunca pelo PR | `observe_ci` | pronto |
| `UNKNOWN`/`UNAVAILABLE` nunca viram PASS | `engine/ci.py` | pronto |
| estados TESTING -> PR_CREATED -> CI_RUNNING | `engine/pipeline.py` | pronto |
| redacao de userinfo de URL no que persiste | `core/redaction.py` | pronto |
| transporte de credencial ate subprocesso | `adapters/childproc.py` | pronto (M6.1) |

Nada disso e tocado. O buraco e um so, e e o `git`.

---

## 2. Defeito 1 — `GitClone.push` nunca funcionou

`_looks_local` e chamado em `workspace/local.py:90` e `:356`. **Nao existe.**
Nao esta definido no arquivo, nao e importado, nao esta em lugar nenhum.

Executado de verdade, com um clone real:

```
commit ok: 8bb7cd04401a
NameError: name '_looks_local' is not defined
```

A guarda que recusa empurrar para um caminho local -- a que existe porque `git`
aponta `origin` para o que foi clonado, e portanto a configuracao mais segura na
aparencia e a que escreve no checkout de alguem -- e codigo morto que **derruba
o processo**.

**Por que passou despercebido por cinco marcos:** `test_pipeline.py` entrega com
`FakeAreas`, e `test_push_target.py` exercita `push_target_of`, nunca `push`. O
caminho de entrega inteiro foi provado contra um dublê que nao tem essa linha.

## 3. Defeito 2 — o provedor de area PADRAO nao sabe fazer nada disso

`IsolatedDirectory` -- `workspace_provider: {name: directory}`, o que a
configuracao de exemplo traz e o que todo workspace de teste usa -- define
`head`, `is_dirty`, `commit` e `push`. Os quatro chamam `self._git`, que **nao
existe nessa classe**.

```
IsolatedDirectory.head()        -> AttributeError: no attribute '_git'
IsolatedDirectory.is_dirty()    -> AttributeError: no attribute '_git'
IsolatedDirectory.push()        -> AttributeError: no attribute '_git'
IsolatedDirectory.push_target() -> None
```

`engine/runner.py:338` chama `areas.commit(...)`. Com o provedor padrao, o
caminho de commit termina em `AttributeError` -- que nao e uma recusa, e sim um
defeito com cara de bug do motor.

E o correto nem seria funcionar: uma pasta nao e um clone. A classe existe para
"trabalho que nao exige um clone -- analise, documentacao, artefato". O que
falta ali nao e implementacao, e uma **recusa nomeada**.

---

## 4. Como o `git` se autentica hoje, medido

```
$ git config --get-all credential.helper
manager
```

Existe um `credential.helper` global nesta maquina. Qualquer `git push` do motor
se autenticaria por ele, sem passar pelo Regente -- o mesmo defeito que o M6.1
fechou para a CLI de hospedagem, na outra ferramenta.

E ele e fechavel por ambiente, sem tocar na maquina:

```
$ GIT_CONFIG_GLOBAL=<nada> GIT_CONFIG_SYSTEM=<nada> git config --get-all credential.helper
  (ausente)
```

`git 2.55` honra `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM` e
`GIT_CONFIG_COUNT/KEY_n/VALUE_n`. Nada disso escreve em disco e nada disso
sobrevive ao processo.

### O mecanismo de transporte, provado contra um `git push` real

Servidor git HTTP no loopback, exigindo `Authorization`:

```
1. push SEM credencial
   rc = 128  fatal: could not read Username ... terminal prompts disabled

2. push COM http.<url>.extraheader via GIT_CONFIG_*
   rc = 0    * [new branch]  main -> main
   servidor viu: Basic eC1hY2Nl...
   refs no bare depois: refs/heads/main d34e6d7
```

Sem argv, sem alterar o remote, sem arquivo, sem configuracao persistente. E a
primeira linha e a contraprova: sem o material governado, `git` nao acha
credencial nenhuma.

---

## 5. A classificacao do ambiente

O M6.1 deixou o `git` de fora porque compor do vazio remove o que ele precisa. A
resposta nao e herdar: e **classificar**.

| categoria | o que entra | por que |
|---|---|---|
| **SAFE_FIXED** | `GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`, `GIT_ASKPASS=`, `GIT_CONFIG_COUNT/KEY_0` | valores que o Regente escolhe. Fecham prompt, config do usuario e ajudante |
| **SAFE_ALLOWLISTED** | `PATH`, `SYSTEMROOT`, `TEMP`, `HOME`, proxy, `GIT_TRACE*` nao | do pai, por nome. `HOME` entra porque `git` precisa dele para outras coisas -- a neutralizacao do `.gitconfig` e por `GIT_CONFIG_GLOBAL`, nao por esconder `HOME` |
| **CREDENTIAL** | `GIT_CONFIG_VALUE_0` | o unico lugar com material, vindo do broker |
| **FORBIDDEN** | `SSH_AUTH_SOCK`, `GIT_SSH_COMMAND`, `GIT_SSH`, `GIT_ASKPASS` do pai, `GH_TOKEN`, todo nome com forma de credencial | sao **autoridade ambiente**: um agente ssh autentica sem o Regente saber |

`SSH_AUTH_SOCK` e `GIT_SSH_COMMAND` sao a decisao desconfortavel. O M6.1 os
listou como "precisa manter para nao quebrar". Manter seria manter um segundo
caminho de autoridade -- exatamente o que o M16 removeu. A resposta honesta:
**um alvo `ssh://` ou `git@host:` e recusado com motivo**, porque o Regente nao
tem mecanismo governado para chave ssh, e usar o agente do usuario seria agir com
autoridade que ninguem concedeu.

Um alvo `https://` e governado. E o que a entrega usa.

---

## 6. O que este marco muda

1. `_looks_local` passa a existir (defeito 1).
2. `IsolatedDirectory` recusa com motivo em vez de estourar (defeito 2).
3. `GitClone` recebe a porta do M6.1 e monta o ambiente por classificacao.
4. `push` exige `repo.push` pelo broker, alvo `https`, e recusa `ssh`.
5. Testes que exercitam `push` de verdade -- contra um servidor git real -- em
   vez de contra um dublê.

Nada de novo sistema de credencial. `ChildEnvironment` ganha uma unica coisa: a
forma como o material e ESCRITO na variavel, porque o `git` quer um cabecalho
`Authorization` e nao um token cru. E rendering, nao autoridade.

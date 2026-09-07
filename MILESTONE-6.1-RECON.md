# Marco 6.1 — levantamento antes do codigo

O M16 provou **autoridade**: existe uma unica porta que decide se um adapter
pode ter material de credencial. Este marco pergunta a proxima coisa, e ela nao
e a mesma:

> O material autorizado chega ao subprocesso, e chega **so por ali**?

Autoridade sem transporte governado e uma fechadura numa porta que ninguem
usa -- se o `gh` se autentica sozinho pelo chaveiro, o `CredentialBroker` pode
recusar o dia inteiro sem alterar o que acontece no mundo.

---

## 1. O caminho atual, medido

### push entrypoint

```
engine/pipeline.py:119        self.delivery.push(area, identity, row, risk)
engine/remote.py:212          RemoteDelivery.push(...)
                                _authorise(repo.push)   <- policy
                                _confirm_identity()
                                _confirm_sha()
                                reconcile()             <- le o remoto
adapters/workspace/local.py:325  GitClone.push(...)
                                cinco recusas locais
                                _git("push", "--set-upstream", "origin", ...)
adapters/workspace/local.py:237  subprocess.run(["git", *args], cwd=...)
```

A segunda mutacao remota tem outro entrypoint:

```
engine/remote.py:276          repos_write.create_pull_request(...)
adapters/repos/github_write.py:190  GitHubWrite.create_pull_request(...)
adapters/repos/github_write.py:74   subprocess.run([gh, *args], ...)
```

### credential request

**Nao existe.** Nenhum adapter de repositorio pede credencial a ninguem.

A composicao ja entrega o broker:

```
container.py:326   repos       = create(..., {"credentials": broker("repository")})
container.py:340   repos_write = create(..., {"credentials": broker("repository_write")})
container.py:344   cicd        = create(..., {"credentials": broker("cicd")})
```

e as tres fabricas o **descartam**:

```
registry.py:119  _repos_github(o)        -> GitHubRepos(org, cli_path, observer, ...)
registry.py:231  _repos_github_write(o)  -> GitHubWrite(org, cli_path, observer)
registry.py:237  _cicd_github(o)         -> GitHubChecks(org, cli_path, observer)
```

`o["credentials"]` nunca e lido. O broker chega ate a porta do adapter e cai no
chao. O provider de workspace nao recebe nem isso: `container.py:327` passa
`{"root": ...}` e mais nada.

### material resolution

Nenhuma. O material nunca e pedido nestes caminhos.

### subprocess construction

Treze sitios criam subprocesso. **Tres compoem ambiente; dez herdam o do pai:**

| sitio | ambiente |
|---|---|
| `engine/testing.py:134` | `childenv.compose(...)` — composto |
| `adapters/secrets.py:126` | `_minimal_env()` — composto |
| `adapters/runner/headless.py:307` | `compose_env(...)` — composto |
| `adapters/repos/github_write.py:74` | **herdado** |
| `adapters/repos/github.py:78` | **herdado** |
| `adapters/cicd/github_checks.py:54` | **herdado** |
| `adapters/repos/git_local.py:80` | **herdado** |
| `adapters/workspace/local.py:155,237` | **herdado** |
| `engine/observation.py:287` | **herdado** |
| `adapters/probe.py:91` | herdado; material vai por **stdin**, deliberadamente |
| `adapters/runner/cli_agent.py:215,248` | herdado |

Todo subprocesso de repositorio deste motor ve o ambiente inteiro do processo
pai. Isso nao e uma omissao de estilo: e o transporte que existe hoje, e ele nao
passa por lugar nenhum.

### environment construction

Existe e e boa -- `core/childenv.py:compose()` monta do vazio, filtra nome com
forma de credencial e adiciona so o que foi nomeado. Ela e usada por tres
chamadores. **Nenhum deles e um adapter de repositorio.**

### argv construction

Limpa nos dois caminhos. `git_local`, `workspace/local` e as tres CLIs montam
`[binario, *args]` com valores de configuracao e de dominio. Nao ha material em
argv hoje -- porque nao ha material.

### logging

`Observer(Call(...))` grava `operation`, `path=" ".join(args[:2])`, duracao,
`error=stderr[:200]`. O stderr do provedor entra em evento persistido. Um erro
que ecoasse a credencial iria para o banco.

`core/redaction.py` ja limpa userinfo de URL, e `remote.py` o aplica em
`push()`, `reconcile()` e nas mensagens de recusa.

### result handling

`store.record_push(delivery_id, target)` grava o alvo **depois** de
`redact_url`. `record_pull_request` grava numero, URL e SHA. Nenhum campo guarda
material -- e nenhum precisa.

---

## 2. O defeito que o levantamento encontrou

Nao e uma lacuna. E um caminho ativo, e ele foi **medido**, nao deduzido:

```
$ GH_CONFIG_DIR=<vazio> GH_TOKEN=<invalido>  gh api user
  {"message": "Bad credentials", "status": "401"}          <- obedece a variavel

$ GH_CONFIG_DIR=<vazio>  (sem token)         gh api user
  To get started with GitHub CLI, please run: gh auth login  <- recusa

$ (ambiente herdado, como o adapter roda hoje)  gh api user
  {"login":"walberth-lopes", ...}                          <- ACEITA
```

A terceira linha e o adapter de hoje. Ele se autentica **pelo chaveiro do
sistema operacional**, sem token nenhum no ambiente, sem passar pelo Regente.

O que isso significa na pratica:

- `broker.material(repo.pr)` pode recusar -- e `gh pr create` funciona assim
  mesmo, porque nunca precisou do resultado.
- Revogar a credencial no Regente nao fecha nada.
- A capacidade da credencial (`repo.read`, e so) nao limita o `gh`, que age com
  o alcance inteiro do token do chaveiro.
- O motor nao consegue dizer **qual** identidade agiu: quem responde e "seja la
  quem o `gh` encontrar".

As duas primeiras linhas provam que o caminho e fechavel: com `GH_CONFIG_DIR`
isolado, `gh` nao tem de onde tirar credencial e obedece `GH_TOKEN`.

---

## 3. A pergunta de desenho

O material deve atravessar a fronteira do processo por **ambiente**, nunca por
argv. Isso e menos obvio do que parece, e vale escrever por que:

- **argv e legivel por qualquer usuario da maquina.** `ps -ef`, o Gerenciador de
  Tarefas, `wmic process get commandline`. Nao ha permissao a pedir.
- **environ e legivel pelo dono do processo.** `/proc/<pid>/environ` e 0400 do
  dono; no Windows exige `PROCESS_VM_READ`.

Ambiente e melhor, e nao e invisivel. Quem ja e o usuario ve os dois. A
afirmacao honesta deste marco e "nao vaza para outros usuarios da maquina, nem
para log, estado, evento, excecao ou remote" -- e nao "e inextraivel".

`adapters/probe.py:91` ja escolheu **stdin** por esse motivo, e escreveu o
porque no codigo. Stdin e melhor que ambiente; nem toda ferramenta o aceita, e o
`gh` nao aceita.

---

## 4. O que este marco muda, e o que deixa em paz

**Muda:** a familia `gh` -- `GitHubRepos`, `GitHubWrite`, `GitHubChecks`. Passam
a rodar com ambiente composto do vazio, config isolada, e token vindo do broker
sob um nome que a configuracao do adapter fornece.

**Nao muda:** os subprocessos `git`. Compor o ambiente deles do vazio remove
`SSH_AUTH_SOCK`, `GIT_SSH_COMMAND` e as variaveis de proxy -- e quebraria clone
real de quem depende delas. E mesmo composto, `git` continuaria achando o
`credential.helper` global via `HOME`, que segue na allowlist. Fechar isso
direito e uma allowlist propria mais isolamento de `gitconfig`, com risco real
de quebrar clone de verdade.

Entao o `git push` continua com credencial propria, e este documento diz isso
em voz alta em vez de deixar parecer resolvido. E o M6.2.

**A pergunta que fica respondida mesmo assim:** existe uma rota unica,
autorizada, sem bypass, de material ate subprocesso -- e ela e usada pela
mutacao remota que o motor de fato executa hoje (`pr create`) e por toda leitura
de repositorio e de CI.

---

## 5. Consequencia desconfortavel, e aceita

Fechar o caminho ambiente significa que **um workspace sem credencial registrada
para de conseguir falar com o provedor**, onde antes funcionava pelo chaveiro.

Isso vai parecer uma regressao para quem tinha `gh auth login` feito. Nao e: era
o motor agindo com uma autoridade que ninguem lhe deu, e que ninguem conseguia
revogar. A recusa passa a nomear o que fazer
(`regente credentials registrar` / `testar`).

`verify()` tambem muda de pergunta. Hoje e `gh auth status` -- que e "estou
autenticado?", e com a config isolada passa a responder nao. A pergunta certa
para `doctor` e a que o M16 ja separou: **"a ferramenta existe e roda?"**.
Autenticacao se prova por `regente credentials testar`, com quatro respostas
separadas, e nao por um comando de saude que devolveria verde.

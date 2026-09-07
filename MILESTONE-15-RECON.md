# Marco 15 — o que existe antes de escrever codigo

Levantado no HEAD `d23be7c`, antes de qualquer implementacao.

## O que ja existe

| peca | estado | observacao |
|---|---|---|
| porta `SecretProvider` | EXISTS | `resolve(reference) -> str` |
| adapter `ScopedSecrets` | EXISTS | resolve `env:NOME` e `arquivo:CAMINHO`; recusa `literal:` de proposito |
| escopo por workspace | EXISTS | mas vem da **configuracao**, nao de um registro |
| `AuthMode` do agente | EXISTS | SESSION / RESOLVED_SECRET / GATEWAY / DELEGATED / NONE |
| `Ability`, `AccessGrant`, `Principal` | EXISTS | do M14 |
| modelo de credencial | **AUSENTE** | |
| validade / expiracao | **AUSENTE** | |
| revogacao de credencial | **AUSENTE** | |
| auditoria de uso de credencial | **AUSENTE** | |
| capacidade por credencial | **AUSENTE** | |

## Os defeitos que o levantamento encontrou

### 1. O escopo de segredo e um fato de configuracao

Exatamente o defeito que o M14 corrigiu uma camada acima, intacto aqui embaixo:

```python
# adapters/secrets.py
allowed_from: frozenset[str]      # vem de `cfg.secrets`, uma lista no YAML
```

Quem edita o arquivo autoriza a si mesmo a resolver a referencia que quiser.
Nao ha quem concedeu, quando, ate quando, nem como revogar. Uma referencia
declarada no YAML vale para sempre, para qualquer uso, sem deixar registro.

### 2. O segredo do agente e resolvido na CONSTRUCAO, antes de qualquer barreira

```python
# adapters/registry.py, dentro de `_auth(o)`, durante o build do adapter
env[str(variable)] = secrets.resolve(str(reference))
```

Isso roda quando a composicao monta o adapter -- antes de existir identidade,
antes de a policy ser consultada, antes de qualquer decisao sobre *se aquele
uso e autorizado*. O caminho `adapter -> secret` existe e nao passa por lugar
nenhum.

O provedor de tasks e melhor por acidente: ele guarda um `lambda` e resolve no
momento do uso. A diferenca nao foi projetada, e nada garante que continue.

### 3. Uma credencial nao tem capacidade

Hoje uma referencia resolvida serve para qualquer coisa que o adapter saiba
fazer. Um token declarado para leitura vale para push, porque quem decide o que
o token faz e o provider remoto -- nao o Regente. `provider capability`,
`credential capability` e `policy authority` sao a mesma coisa neste momento, e
precisam ser tres.

## Credenciais reais neste ambiente

| candidato | resultado |
|---|---|
| `JIRA_EMAIL`, `JIRA_API_TOKEN` | **ausentes** |
| `GITHUB_TOKEN`, `GH_TOKEN` | **ausentes** |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | **ausentes** |
| credencial do `gh` no chaveiro do sistema | **PRESENTE** |

```
$ gh auth status
github.com
  ✓ Logged in to github.com account walberth-lopes (keyring)
  Token scopes: 'gist', 'read:org', 'repo', 'workflow'
```

Nenhuma variavel de ambiente com segredo. O que existe e uma credencial real
guardada **no chaveiro do sistema operacional**, alcancavel por um ajudante de
credencial (`gh auth token`) -- que e precisamente a forma "credential helper"
que este marco pede como segunda fonte de segredo.

Isso permite um exercicio real **sem copiar segredo para lugar nenhum**: o
ajudante devolve o material no momento do uso, e ele nao e persistido.

As variaveis `CLAUDE_CODE_OAUTH_*` do ambiente pertencem a sessao interativa e
continuam proibidas desde o M7. Nao foram tocadas.

## Consequencia para o marco

O centro do M15 -- credencial com autoridade, capacidade, validade, revogacao,
auditoria e isolamento -- nao depende de infraestrutura externa e sera
implementado e provado.

O exercicio real sera feito com a credencial do chaveiro, por um ajudante, numa
operacao **somente de leitura**.

M6 e M7 continuam bloqueados e nao serao reabertos: nao ha credencial de Jira
nem de agente neste ambiente, e nenhuma sera inventada.

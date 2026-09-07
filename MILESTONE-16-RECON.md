# Marco 16 — o caminho legado, medido no codigo

Levantado no HEAD `537540b`, antes de qualquer alteracao. O relatorio do M15 diz
que o caminho antigo continua existindo; isto verifica onde, exatamente.

## Inventario dos adapters

| Adapter | Provider | Como obtem credencial hoje | Governado? | Acesso direto a segredo? | Migrar |
|---|---|---|---|---|---|
| `JiraTasks` + `HttpTransport` | tasks | `registry.py:90` monta um `lambda` que chama `secrets.resolve` com referencia do YAML | **NAO** | nao (via `SecretProvider`) | **SIM** |
| `JiraTasks` + `SnapshotTransport` | tasks | nenhuma; le arquivo em disco | n/a | nao | nao |
| `FilesystemTasks` | tasks | nenhuma | n/a | nao | nao |
| `GitHubRepos` | repository | `gh` resolve sozinho, fora do Regente | **NAO** | nao (o `gh` tem a dele) | **SIM** |
| `GitHubWrite` | repository_write | idem | **NAO** | nao | **SIM** |
| `GitHubChecks` | cicd | idem | **NAO** | nao | **SIM** |
| `GitLocal`, `ReadOnlyRepos` | repository | nenhuma | n/a | nao | nao |
| `ClaudeCodeAgent`, `CodexCliAgent` | runner | `registry.py:171` resolve **na construcao** e injeta em `sandbox.env` | **NAO** | nao | **SIM** |
| `ScriptedAgent`, `DeterministicAgent`, `ExternalAgent` | runner | nenhuma | n/a | nao | nao |
| `IsolatedDirectory` | workspace | nenhuma | n/a | nao | nao |
| `Console` | notification | nenhuma | n/a | nao | nao |
| `DevTokenIdentity`, `OsAccountIdentity` | identity | geram/leem a propria; nao sao credencial de provider | n/a | nao | nao |
| `ScopedSecrets` | secrets | E a fonte | n/a | e o mecanismo | nao |

Nenhum adapter le segredo do ambiente por conta propria. Isso ja estava certo.
O que esta errado e **quem decide** que ele pode receber.

## Os dois sitios legados, exatos

```
regente/adapters/registry.py:85-90     tasks (jira)
    secrets = o["secrets"]
    credencial=lambda: (secrets.resolve(ref_usuario), secrets.resolve(ref_token))

regente/adapters/registry.py:159-171   runner (agente)
    env[str(variable)] = secrets.resolve(str(reference))
```

E a composicao que os alimenta:

```
regente/app/container.py:294,300,314,318,387,405
    {"secrets": secrets, "observer": observe}
```

O de tasks resolve **no momento do uso** (um `lambda`); o de agente resolve **na
construcao**. A diferenca nunca foi projetada. Nenhum dos dois pergunta por
identidade, concessao, capacidade, validade, revogacao ou policy.

## `os.environ`, classificado

| local | o que le | classe |
|---|---|---|
| `identity/os_account.py:107-108` | `USERNAME`, `USERDOMAIN` | 1 — configuracao nao secreta (nome e emissor) |
| `runner/headless.py:60` | compoe o ambiente do filho a partir de uma allowlist | 3 — comportamento do processo |
| `secrets.py:36` | `_minimal_env()` para o ajudante | 3 — comportamento do processo |
| `secrets.py:89` | `env:NOME` | **5 — material secreto**, e e o mecanismo governado |
| `engine/testing.py:124` | ambiente do runner de testes | 3 |

Uma unica leitura de material secreto, e ela e a propria fonte. Nada a banir.

## A pergunta que a migracao forca

Quem e o **principal** quando o motor age sozinho?

O caminho governado do M15 exige identidade, concessao e policy. Um tick nao tem
humano: ele roda de madrugada, sem ninguem olhando. Hoje a autoridade do motor e
implicita -- ele age porque foi construido, e ninguem registrou isso.

A resposta honesta e a mesma que o M14 deu para pessoas: **o motor tambem
precisa de identidade e de concessao**. Um principal de servico, com concessao
gravada, revogavel por quem administra acesso -- e nao um caso especial que
dispensa as barreiras.

Isso e o que este marco tem de construir para que a migracao seja possivel sem
abrir uma excecao. Uma excecao para o motor seria exatamente a segunda
autoridade que o marco existe para eliminar.

## M6 e M7

| marco | estado | o que depende do caminho legado |
|---|---|---|
| M6 (PR/CI real) | `NEEDS_HUMAN` | `GitHubWrite` e `GitHubChecks` -- hoje o `gh` autentica sozinho, fora do Regente |
| M7 (agente real) | `BLOCKED_AUTHENTICATION` | `registry.py:171`, a resolucao na construcao |

Nenhum dos dois sera reaberto. O que a migracao muda para eles e o caminho, nao
o bloqueio: depois dela, destravar M7 e registrar uma credencial com capacidade
`agent.run` -- e nao editar uma lista no YAML.

## Credencial real disponivel

A mesma do M15: `gh` autenticado no chaveiro do sistema, alcancavel por
`helper:github`. Serve para reexecutar a prova real depois da migracao.

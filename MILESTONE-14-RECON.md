# Marco 14 — o que existe antes de escrever codigo

Levantado no HEAD `f5572fb`, antes de qualquer implementacao. O marco exige que
a identidade deixe de ser uma afirmacao de configuracao; o primeiro passo e
descobrir do que se dispoe para isso.

## Provedores de identidade reais disponiveis neste ambiente

Procurados, um a um:

| candidato | resultado |
|---|---|
| OIDC / OAuth / SSO configurado | **ausente** — nenhuma variavel de ambiente, nenhum arquivo de configuracao |
| Diretorio corporativo (dominio) | **ausente** — `PartOfDomain: False`, `Domain: WORKGROUP` |
| Entra ID / Azure AD | **ausente** — `AzureAdJoined: NO`, `WorkplaceJoined: NO` |
| LDAP / Keycloak / Okta / Auth0 | **ausente** |
| Conta do sistema operacional | **PRESENTE** — `DESKTOP-09OD9Q8\Sabydo`, SID `S-1-5-21-…-1001`, autenticada por NTLM contra a autoridade local |

As unicas variaveis com cara de OAuth no ambiente pertencem a **sessao
interativa do Claude Code** (`CLAUDE_CODE_OAUTH_SCOPES`, `USE_LOCAL_OAUTH`).
Usa-las seria pegar emprestada a credencial da sessao — proibido desde o M7, e
proibido de novo pelo item 20 deste marco. Nao foram tocadas.

### O que isso permite, e o que nao permite

Existe **uma** identidade real e verificavel nesta maquina: a conta do sistema
operacional. Ela tem o que o item 9 pede — identificador estavel (o SID, nao o
nome de exibicao), emissor nomeavel (a autoridade da maquina), metodo
(`NTLM`) e momento de autenticacao.

Ela serve ao **terminal**, onde o processo roda sob aquela conta.

Ela **nao** serve ao **navegador**: uma requisicao HTTP nao carrega a conta do
sistema, e faze-la carregar exigiria autenticacao integrada (Negotiate/NTLM
sobre HTTP), que e uma integracao que este ambiente nao tem configurada e que
nao sera inventada.

```
terminal   -> identidade real da conta do SO       -> exercitavel
navegador  -> nenhum provedor real disponivel      -> BLOCKED_REAL_IDENTITY
```

## O estado do que ja existe

| peca | estado | observacao |
|---|---|---|
| `IdentityProvider`, `Identity` | EXISTS | `Identity` tem `subject`, `display`, `method`. **Falta provedor, emissor e momento** |
| `Principal` | EXISTS | separa ler de decidir |
| `dev-token` | EXISTS | so loopback, se anuncia como desenvolvimento |
| `LocalTerminalIdentity` | EXISTS | usa `getpass.getuser()` |
| `DecisionService` + `POST .../decision` | EXISTS | passa por identidade, escopo, policy, Core, auditoria |
| Trilha de decisao | EXISTS | registra quem decidiu |
| `approval.decide` na policy | EXISTS | |
| Concessao de acesso persistida | **AUSENTE** | |
| Trilha de quem concedeu/revogou | **AUSENTE** | |
| Revogacao | **AUSENTE** | |

## Os dois defeitos que o levantamento encontrou

**1. Acesso e um fato de configuracao, nao uma concessao atribuivel.**

`Principal.decides` — a autoridade de escrita inteira — e preenchido pelo
proprio provedor de identidade, a partir de campos que a composicao escreve:

```
adapters/identity/dev_token.py:105      decides=self.decides
adapters/identity/local_terminal.py:71  decides=self.decides
app/container.py:92                     decides=frozenset({self.workspace.id})
cli.py:281                              reads=visible, decides=decides
```

Nao existe registro de quem concedeu, quando, com quais capacidades, nem como
revogar. Quem edita o arquivo de configuracao concede a si mesmo autoridade de
escrita, e nada guarda esse fato.

Pior: o proprio `IdentityProvider.principal()` faz as duas coisas que o item 3
manda separar. A docstring dele ja diz o contrario do que o codigo faz --
*"um provedor de identidade corporativo sabe quem voce e e nao faz ideia de
quais workspaces deste motor sao seus"* — e ainda assim e ele quem devolve
`decides`.

**2. A identidade do terminal e afirmada, nao provada.**

`LocalTerminalIdentity` usa `getpass.getuser()`, que consulta `LOGNAME`, `USER`,
`LNAME` e `USERNAME` **antes** do sistema. Todas sao editaveis por quem roda o
processo. O sujeito gravado na auditoria e, na pratica, um texto escolhido por
quem decide — o mesmo defeito do `--por` removido no M13, uma camada abaixo.

O SID existe, e estavel, e nao e editavel.

## Consequencia para o marco

O centro do M14 — concessao persistida, administracao, revogacao, auditoria
atribuivel, isolamento — nao depende de provedor externo e sera implementado e
provado.

A identidade real sera implementada onde ha uma: a conta do SO, para o terminal.
Para o navegador, o marco para na fronteira e declara `BLOCKED_REAL_IDENTITY`.

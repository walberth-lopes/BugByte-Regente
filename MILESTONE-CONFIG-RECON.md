# Marco Configuracao — levantamento antes do codigo

O marco anterior tirou o `regente tick` das maos da pessoa. Sobrou um atrito
maior:

> ainda e preciso editar `regente.yaml` e usar o terminal para conectar um
> provider e registrar uma credencial.

Este marco pergunta se a Mission Control consegue ser o ponto de configuracao --
sem virar uma segunda autoridade.

---

## 1. O que JA existe, e nao deve ser refeito

| peca | onde | estado |
|---|---|---|
| registrar / revogar credencial pela API | `api.py:_credential_write` | pronto (M15) |
| **formulario de credencial na tela** | `app.js:pages.credentials` | pronto -- nome, provider, referencia, capacidades, validade |
| revogar pela tela, com historia | idem | pronto |
| `test_connection` com quatro fatos separados | `credentials.py:388` | pronto |
| conceder / revogar acesso pela tela | `pages.access` | pronto (M14) |
| controlar o motor pela tela | `pages.overview` | pronto (marco anterior) |
| regras de selecao, puras e deterministicas | `core/selection.py` | pronto |
| `status_map` declarado pelo workspace | `ports/tasks.py:status_map_from` | pronto |
| ordem final `(prioridade, chave)` com motivo | `core/selection.py:order` | pronto |

**A tela ja registra credencial.** O que falta ali e menor do que parecia: o
teste de conexao e a explicacao de onde o segredo vive.

---

## 2. O que NAO existe

### 2.1 Nao ha onde a tela GRAVAR configuracao

```python
def load(path) -> Config:          # app/config.py:115
    raw = yaml.safe_load(p.read_text(...))
```

`Config` e `AdapterConf` sao `frozen=True`, montados a cada comando a partir do
arquivo. **Nao existe caminho de escrita.** Para a tela configurar um provider,
alguma coisa precisa persistir a escolha -- e essa e a decisao de desenho do
marco (secao 4).

### 2.2 O teste de conexao e recusado pela API de proposito

```
POST .../credentials/{id}/test  ->  501 no_probe
  "a sonda pertence a composicao, e a API nao escolhe qual usar"
```

A recusa esta certa e continua valendo: a API nao pode saber o que e um Jira. O
que falta e a composicao **entregar** a sonda ao servidor, como ja entrega o
`CredentialService` -- exatamente o padrao que o marco anterior usou para
`operations`.

### 2.3 Nao ha pagina de conexoes, nem prontidao por provider

A tela mostra tasks, runs, entregas, acesso, credenciais e saude. Nao mostra
"o provider de tasks esta configurado? tem credencial? ela serve?". Hoje isso
so existe no terminal, espalhado entre `doctor` e `credentials testar`.

### 2.4 `status_map` e `selecao` so existem no YAML

O motor le os dois; nada os escreve. E ninguem consegue ver a ordem que as
regras produzem **antes** de ligar o motor -- `regente plan` mostra o plano, e
nao o porque de cada posicao.

---

## 3. Os dois bloqueios externos continuam

Nenhuma credencial de Jira que o motor resolva; nenhum agente de modelo
autenticado. Nao serao contornados. Consequencia direta e honesta: o fluxo de
"conectar Jira" pode ser construido e provado ate a fronteira do provedor, e
**a leitura real do board continua bloqueada**.

---

## 4. A decisao de desenho: onde a configuracao passa a morar

Tres saidas, e as duas primeiras sao piores:

**(a) A tela reescreve `regente.yaml`.** Um processo HTTP passa a editar um
arquivo que a pessoa tambem edita. Comentarios se perdem, edicoes simultaneas se
atropelam, e um erro de escrita deixa o motor sem subir. Um arquivo de
configuracao versionado tem dono, e nao e uma pagina web.

**(b) Tudo migra para o banco.** Quebra todo workspace existente e transforma
uma configuracao legivel e versionavel em linhas opacas.

**(c) O banco guarda o que a TELA edita; o arquivo continua sendo a base.**

Escolhido (c), com uma regra que evita a armadilha classica de duas fontes:

```
regente.yaml   ->  base, versionavel, dono humano
banco          ->  sobreposicao, escrita pela tela E pela CLI
leitura        ->  base + sobreposicao, com a PROCEDENCIA visivel
```

**A procedencia e obrigatoria.** Sem ela, alguem edita o YAML, nada muda, e a
conclusao razoavel e "o Regente esta quebrado". Toda tela e todo comando que
mostrar configuracao tem de dizer, campo a campo, se aquilo veio do arquivo ou
da sobreposicao -- e oferecer como remove-la.

Isto e o mesmo padrao do marco anterior: `Operation` guarda intencao no banco e
a fase e derivada; aqui a sobreposicao mora no banco e a configuracao efetiva e
derivada. Nenhuma arquitetura nova.

---

## 5. Autoridade: uma capacidade nova, e so

Configurar provider **nao** e a mesma coisa que operar o motor, nem que
administrar credencial. Quem liga e desliga o processamento nao deveria, por
tabela, poder apontar o motor para outro board.

```
workspace.settings.write     nova
```

O caminho e o mesmo de sempre, sem atalho:

```
identidade -> concessao -> capacidade -> policy -> comando -> persistencia -> auditoria
```

---

## 6. O que este marco faz

1. `SettingsService` + tabela, com sobreposicao e procedencia visivel.
2. Rotas de escrita nomeadas, uma a uma, como as tres que ja existem.
3. Pagina **Conexoes**: prontidao por provider, vinda dos servicos reais.
4. Teste de conexao pela tela -- a composicao entrega a sonda.
5. Status mapping e regras de prioridade editaveis, com **preview da fila**.
6. Onboarding que mostra bloqueio em vez de esconder.
7. `regente config` na CLI, sobre o MESMO servico.

E o que ele **nao** faz: reescrever YAML, redesenhar a UI, criar RBAC,
instalador, ou qualquer provider novo.

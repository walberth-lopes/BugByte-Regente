# Regente

Sistema operacional para agentes de engenharia de software.

O agente trabalha. O Orchestrator coordena. As ferramentas executam. As policies
protegem. A fila chama você **só quando a resposta não existe dentro do sistema**.

Não é um chatbot que sabe programar.

## Estado

**Marco 1** — descoberta, grafo de dependências, scheduler paralelo, workers
isolados, estado persistente, detecção de falha, escalonamento humano e retomada
após `kill -9`.

**Marco 3** — primeiro provedor real, em sombra: o motor lê um board de verdade,
normaliza, monta o grafo e planeja, **sem autoridade para mutar nada**. Dois
provedores completamente diferentes passam pelo mesmo contrato.

Ver [ROADMAP.md](ROADMAP.md), [ARCHITECTURE.md](ARCHITECTURE.md) e
[MAPEAMENTO.md](MAPEAMENTO.md).

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
uv venv --python 3.13
uv pip install -e ".[dev]"
```

## Usar

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
| `rules` | regras, limites e adapters em vigor |

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
    nome: filesystem     # o adapter, por nome
    diretorio: ./tasks
```

`regente rules` lista o que está disponível. Adicionar um provedor é adicionar
uma entrada em `adapters/registry.py` — se algum dia exigir mexer em `core/` ou
`engine/`, a abstração falhou, e `tests/test_fronteiras.py` acusa.

## Testes

```bash
pytest
```

Inclui o teste de fronteira, que lê o código-fonte e falha se o domínio importar
I/O ou se um nome de ferramenta vazar para o núcleo.

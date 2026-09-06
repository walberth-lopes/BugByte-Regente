# Regente

Sistema operacional para agentes de engenharia de software.

O agente trabalha. O Orchestrator coordena. As ferramentas executam. As policies
protegem. A fila chama você **só quando a resposta não existe dentro do sistema**.

Não é um chatbot que sabe programar.

## Estado

Marco 1 pronto e provado: descoberta, grafo de dependências, scheduler paralelo,
workers isolados, estado persistente, detecção de falha, escalonamento humano e
retomada após `kill -9`. Ver [ROADMAP.md](ROADMAP.md) e
[ARCHITECTURE.md](ARCHITECTURE.md).

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

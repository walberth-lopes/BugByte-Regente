# Regente

An operating system for software engineering agents.

The agent works. The Orchestrator coordinates. The tools execute. The policies
protect. The queue calls you **only when the answer does not exist inside the
system**.

It is not a chatbot that knows how to program.

## Status

**Milestone 1** — discovery, dependency graph, parallel scheduler, isolated
workers, persistent state, failure detection, human escalation and resumption
after `kill -9`.

**Milestone 3** — the first real task provider, in shadow: the engine reads a
real board, normalises it, builds the graph and plans, **with no authority to
mutate anything**.

**Milestone 4** — repository provider, in shadow: two real adapters of opposite
kinds (local git and remote hosting), identity within the tenancy, and the chain
`task → repository → base → resources → risk/policy → candidate` answered without
touching anything.

See [ROADMAP.md](ROADMAP.md), [ARCHITECTURE.md](ARCHITECTURE.md),
[MAPEAMENTO.md](MAPEAMENTO.md) and [ALVO.md](ALVO.md).

## Shadow: see without touching

```bash
regente shadow
```

Discovers, normalises, builds the graph and shows what the engine would do —
without writing a line anywhere. The guarantee is not discipline: the read
transport **has no write verb**. Turning writing on requires adding a method,
which shows up in a diff and goes through review — not an `if` somebody switches
off by accident.

## Install

```bash
uv venv --python 3.13
uv pip install -e ".[dev]"
```

## Use

```bash
regente init      # creates regente.yaml and the tasks/ folder
regente doctor    # proves the engine starts: database, adapters, policies
regente tick      # runs one cycle
regente status    # what is happening, what needs you
```

| command | what it does |
|---|---|
| `init` | creates the initial configuration |
| `doctor` | proves each bet about the environment by running it, not by assuming |
| `tick` | one cycle: recover, discover, analyse, plan, dispatch, collect |
| `status` | the four questions: what is running, what needs you, what is stuck, what finished |
| `plan` | what the scheduler would do now — without executing |
| `health` | what is running, what is stuck and for how long — read from disk, so it answers even after the engine dies |
| `needs-me` | the queue of human decisions, with a briefing |
| `decide` | records your decision on an item in the queue |
| `log` | the trail: every transition, with actor and reason |
| `shadow` | sees the real work and what the engine would do, without touching anything |
| `repos` | visible repositories, as the engine sees them |
| `chain` | from the real task to an execution candidate, link by link |
| `mission` | picks one task and shows the briefing; executes only with `--run` |
| `rules` | the rules, limits and adapters in force |

## Reports

`shadow`, `chain` and `mission` print to the terminal. Add `--output` to keep a
copy:

```bash
regente shadow --output r.txt        # -> reports/r.txt
regente chain  --output docs/r.txt   # -> docs/r.txt
```

A bare filename is a **name**, not a location: it lands in `reports/`, which is
gitignored, so a report is never committed by accident. A value carrying a
directory is a place you chose, and is used as given — `docs/r.txt`, `./r.txt`,
`/tmp/r.txt`. Missing directories are created. Each command prints the path it
actually wrote.

Without `--output`, nothing is written. `mission` writes only with `--run`: a dry
run has a briefing but no measurements, and returns before the report.

## The first tick is a baseline

Switching the engine on against a full backlog **records** the work and
dispatches nothing. Without that, the first contact with a board of fifty tasks
becomes a storm of workers — and an incident instead of a product.

## Shadow before live

`shadow: true` is born on. In that mode the engine decides, records and shows,
but performs no external write. Turning it off is an explicit decision, after you
have read what it *would* have done and answered yes to: *would I sign this with
my name?*

## Swapping providers

One line in `regente.yaml`:

```yaml
providers:
  tasks:
    name: filesystem     # the adapter, by name
    directory: ./tasks
```

`regente rules` lists what is available. Adding a provider means adding an entry
in `adapters/registry.py` — if it ever requires touching `core/` or `engine/`,
the abstraction has failed, and `tests/test_boundaries.py` says so.

## Tests

```bash
pytest
```

Includes the boundary test, which reads the source and fails if the domain
imports I/O or if a tool's name leaks into the core.

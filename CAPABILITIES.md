# Capabilities — what is proven, and by what

Four states, kept apart on purpose. Collapsing them is how a project convinces
itself it has done something it has not.

| State | What it means | What it does **not** mean |
|---|---|---|
| **IMPLEMENTED** | The code exists and imports. | That it has ever run. |
| **CONTRACT-TESTED** | Tests exercise it against fakes/fixtures. Guards proven by mutation: break the guard, the test goes red. | That any real provider ever answered. |
| **EXERCISED** | It ran against a real external system, and the world was inspected afterwards. | — |
| **BLOCKED** | Ready, refusing to run, for a stated reason. | That it is broken. |

A capability that is only CONTRACT-TESTED must never be cited as evidence that
the flow works. Fakes are obedient: every refusal in `tests/test_remote.py`
comes from the engine, never from a fake declining to cooperate — which makes
them good at proving guards and worthless at proving integration.

---

## Milestone 6 — remote delivery

| Capability | State | Evidence |
|---|---|---|
| Push contract (`WorkspaceProvider.push`, `expected_sha` required) | CONTRACT-TESTED | `tests/test_push_target.py`, `tests/test_remote.py` |
| Push refuses integration branches, foreign branches, local targets, SHA drift, force | CONTRACT-TESTED | `tests/test_push_target.py` |
| GitHub write adapter (`pr create` only) | CONTRACT-TESTED | `tests/test_remote.py::test_the_write_gate_*` |
| `create_pull_request` with a mandatory run marker | CONTRACT-TESTED | `test_a_pull_request_carries_the_run_marker` |
| `get_pull_request` / read-back after creation | IMPLEMENTED | no test drives the real CLI |
| CI observation, read-only | CONTRACT-TESTED | `test_ci_is_asked_about_the_commit_not_the_pull_request` |
| CI classification (6 results, 4 states) | CONTRACT-TESTED | `tests/test_remote.py`, CI section |
| Persisting `task → run → commit → PR → CI` | CONTRACT-TESTED | `test_the_ci_observation_is_persisted`, store tests |
| PR identity by head SHA + marker | CONTRACT-TESTED | seven adoption tests |
| Policy + identity + SHA revalidated per mutation | CONTRACT-TESTED | mutation-checked (see below) |
| **The whole chain against a real repository** | **BLOCKED** | no eligible task — see below |

### What "BLOCKED" means here, precisely

Nothing in this milestone has been pushed anywhere. No pull request has been
opened. No real CI has been observed. The gate is not technical: the criteria
for an eligible task (small, reversible, no production path, no infrastructure,
no secrets, not owned by a person, real acceptance criteria) are not met by any
task currently on the board, and inventing one to complete the exercise would
make the proof worthless — it would demonstrate that the engine can act on a
task written to let it act.

The engine stops at `NEEDS_HUMAN`. That is the correct outcome, not a failure.

### Mutation checks

Guards are only guards if removing them breaks a test. Each was deleted, the
suite run, and the guard restored:

| Mutation | Result |
|---|---|
| Drop the SHA revalidation before push | caught |
| Drop the identity check before push | caught |
| Adopt a PR on marker alone, ignoring the head | caught |
| Adopt an unmarked pull request | caught |
| Report provider unavailability as a CI result | caught |
| Let confirmed absence of checks read as `PASSED` | caught |
| Call a red check a regression with no baseline | caught |
| Drop the one-PR-per-workspace constraint | caught |
| Stop refusing `--web` on a permitted write | caught |
| Widen the write allowlist to `pr merge` / `pr review` | *not* caught — correctly: the forbidden list still refuses |
| Empty the forbidden list, allowlist left narrow | *not* caught — correctly: the allowlist still refuses |
| Break **both** allowlist layers at once | caught |

The last three are the interesting ones. The write gate is two independent
layers, and no single-layer edit lets a merge through.

---

## Earlier milestones

| Capability | State | Evidence |
|---|---|---|
| Core, persistent state, policy, scheduler, recovery | EXERCISED | M1, `8d3e5de` |
| Jira task provider, read-only | EXERCISED | M3, `49a8e73` — real site, real issues |
| Repository provider, read-only | EXERCISED | M4, `4e37827` |
| Target resolution, DECLARED and DISCOVERED | EXERCISED | M5, `MILESTONE-5-PROOF.txt` |
| Agent run + validation loop + commit on an isolated branch | EXERCISED | M5, `8aac885` — both paths, real repos, forbidden mutations verified by inspecting the world |

---

## Authority, unchanged

Allowed at L2: push of an execution branch, opening a pull request, observing
CI. Forbidden regardless of level: merge, approve, review, deploy, production
change, writing back to the external task, triggering or cancelling CI.

The agent holds none of these. It produces a commit; producing a commit does not
entitle it to publish one. `regente/engine/remote.py` never reads the agent's
`Outcome`, and a test asserts that structurally.

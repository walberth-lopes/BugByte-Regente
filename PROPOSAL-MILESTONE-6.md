# Milestone 6 Proposal — Pull Request + CI

Derived from the real state of the code at commit `8aac885`, and from the real
environment measured on 06/09/2026. Nothing here is anticipated: every claim
about what exists was read from the source or from the machine.

---

## 1. What the Core can already do

Proven end to end, against real repositories with real tests:

```
discover task → resolve target (DECLARED or DISCOVERED, with evidence)
→ refuse or select one mission → isolated clone → isolated branch
→ baseline measurement → agent → structured outcome → baseline-aware classification
→ engine verdict → policy → commit
```

Concretely, and already exercised:

- **Tenancy identity.** `organization / client / workspace / project / repository`
  in every persisted row; leases keyed by `(workspace, resource)`; repository
  identity is `(provider, key)` scoped by workspace, never a bare name.
- **Evidence-based targeting.** `DECLARED / DISCOVERED / VALIDATED`, with
  `alternatives_considered` and `reason_rejected` persisted. Promotion has an
  independent rule that does not consult the agent's success.
- **Authority separation.** `Agent → Outcome`, `Engine → Validation`,
  `Policy → Permission`, `Engine → act`. Enforced structurally: a test parses
  `may_commit` and fails if it ever reads the agent's `Outcome`.
- **Baseline discipline.** Absence of verification is never success. Five test
  classifications, and `UNKNOWN` when a regression cannot be claimed.
- **Hard limits and no-progress detection** in the validation loop.
- **Refusal as a first-class outcome**, naming the criterion that failed.

Already declared but **never exercised**:

- `RepositoryProvider.push / create_pull_request / submit_review / merge_pull_request`
  — signatures exist, all raise `NotImplementedError`.
- `CICDProvider` (`ports/delivery.py`) — `get_status`, `get_logs`, `run`. Written
  in Milestone 1, no adapter has ever implemented it.
- `TaskState`: `PR_CREATED → CI_RUNNING → AI_REVIEW → APPROVED` with transitions
  already in the table.
- Policy rules for `repo.push` / `repo.pr` (ALLOW at L2) and `repo.merge`
  (L3, ALLOW in staging, HUMAN_APPROVAL in production).

**The state machine and the policy for this milestone already exist.** What is
missing is the crossing of the boundary itself.

---

## 2. The capability that is actually missing

One sentence: **the engine cannot leave the machine.**

Everything proven so far happens inside a local clone. The commit exists on a
local branch that no other system can see. Between that commit and a pull request
sits a boundary the engine has never crossed, and crossing it introduces four
things that do not exist locally:

1. **A remote that accepts writes.** Measured gap: the isolated clone's `origin`
   points at the *local source path*, not at the real remote:

   ```
   origin  C:/Users/.../pathb/repos/slugger (push)
   ```

   A `push` today would write into the source clone — precisely the mutation the
   milestone forbids. This is the single most important finding of this analysis:
   **the current isolation makes a correct push impossible, and an incorrect one
   easy.**

2. **An identity that outlives the process.** A PR number and a CI run id are
   assigned by someone else. They must be bound to `task/run/commit` at the
   moment of creation, or the binding can never be reconstructed honestly.

3. **Asynchrony.** CI does not answer when asked. It is pending, then it is
   something. Every state so far has been synchronous.

4. **Observability of another system's opinion.** CI produces a verdict the
   engine did not compute and must not restate as its own.

---

## 3. New Ports and Adapters actually needed

**No new port.** Both already exist and are unimplemented.

| port | change | why |
|---|---|---|
| `RepositoryProvider` | implement `push`, `create_pull_request`; add `get_pull_request` for real | already declared with the right signatures (`expected_sha` on push, `head_sha` on review) |
| `CICDProvider` | first implementation | written in M1, never exercised |
| `WorkspaceProvider` | **add `set_push_target` (or equivalent)** | the measured gap in §2.1 — the area must be able to reach the real remote without the source being reachable |

New adapters:

- `adapters/repos/github.py` — extend the existing read-only adapter with a
  **separate, narrowly-scoped write path**. The read allowlist
  (`readonly.py::cli_is_read`) must stay exactly as it is; writes go through a
  second function with its own allowlist, so that the read gate never becomes a
  read-write gate by widening.
- `adapters/cicd/github_checks.py` — a new `CICDProvider`.

**Design note carried from Milestone 4:** authorisation is over the *effective
invocation and its arguments*, never a verb prefix. `gh pr create` and
`gh pr merge` both start with `pr`. The write allowlist must be
`(command, subcommand)` pairs plus argument inspection, exactly as `readonly.py`
does today.

---

## 4. What stays out of the Core

Unchanged from every previous milestone, and worth restating because this is the
milestone where the temptation appears:

- The Core must not learn what a "check run", a "workflow", a "merge queue" or a
  "required status check" is. Those are provider vocabulary. The Core knows
  `Check(name, state, conclusion)` and nothing else.
- The Core must not learn what a PR *number* means beyond an opaque external
  identifier. Numbering is a provider fact.
- Branch protection, required reviewers and merge strategies are provider
  configuration. The engine **reads** them as facts and never assumes them.
  (Measured in the reference implementation on 20/08/2026: the org had no branch
  protection, and `reviewDecision` came back empty even with a submitted review.
  The engine must derive state from the review list, never from an aggregate
  field it did not verify.)
- CI *interpretation* stays in the Core; CI *access* stays in the adapter.

---

## 5. The state machine

The states already exist. What this milestone adds is the transitions being
*driven*, plus one gap.

```
  TESTING ──commit exists──▶ PR_CREATED ──▶ CI_RUNNING ──▶ AI_REVIEW
     ▲                            │              │
     │                            │              ├──▶ IMPLEMENTING   (regression)
     └────────────────────────────┴──────────────┘
                                  │
                                  ├──▶ BLOCKED        (environment / infra / unavailable)
                                  └──▶ WAITING_HUMAN  (ambiguous, or budget exhausted)
```

**A gap to fix in `states.py`:** `PR_CREATED` currently allows only
`{CI_RUNNING, AI_REVIEW}` plus the emergency escapes. A repository with **no CI
at all** is a legitimate, common case, and the honest destination is `AI_REVIEW`
— which the table already allows. Good. But `CI_RUNNING → PR_CREATED` does not
exist, and there is no need for it: a re-run of CI is not a state change, it is
an observation of the same state. Recording that as a transition would make the
timeline lie about how many times work moved.

**One new engine concept, not a new state:** *pending*. CI being unfinished is
not a state of the task; it is the absence of an answer. The task stays in
`CI_RUNNING` and the tick re-observes. Modelling "pending" as a state would make
every polling cycle look like progress.

---

## 6. CI outcomes

Deliberately mirroring the existing `TestResult` vocabulary, because the
distinctions are the same and inventing a second taxonomy would guarantee they
drift.

| CI condition | classification | engine action |
|---|---|---|
| all checks green | `PASSED` | `CI_RUNNING → AI_REVIEW`. **Not** resolution — see §16 |
| a check red, and it was green on the base | `REGRESSION` | `→ IMPLEMENTING`, with the failing check names as `previous_failures` |
| a check red, and it was already red on the base | `PREEXISTING_FAILURE` | proceed to `AI_REVIEW`, stating the inherited red |
| red for missing service/credential/dependency | `ENVIRONMENT_FAILURE` | `→ BLOCKED`. Never retried by re-running the agent: it would buy the same missing dependency |
| provider unreachable, 5xx, rate-limited | **unavailable** — *not* a CI result | stay in `CI_RUNNING`, retry with backoff, escalate after a budget. **A read failure is never permission to proceed** |
| checks queued or in progress | **pending** — *not* a result | stay in `CI_RUNNING`, re-observe next tick |
| no checks exist at all | `NO_CHECKS` | proceed to `AI_REVIEW`, **explicitly recorded**. Absence of proof is not proof |
| exceeded the CI budget while pending | timeout | `→ WAITING_HUMAN`, with what was observed |

**The baseline rule applies to CI exactly as it does to local tests.** Claiming
`REGRESSION` requires knowing the check was green on the base commit. Without
that, the honest classification is `UNKNOWN`, and `UNKNOWN` approves nothing.

**`NO_CHECKS` must never be silently equivalent to `PASSED`.** In the reference
implementation, a repository whose only check was `validate` was exactly where
security PRs lived — an empty or trivial check set is a fact about the
repository, not evidence about the change.

---

## 7. Authority

Same chain as the commit, extended:

```
Agent            → produces work                      (never authority)
Engine           → validates, then requests
Policy           → authorises the specific mutation
External provider→ executes the mutation
Engine           → observes and validates the result
```

| action | who decides | policy action | autonomy | this milestone |
|---|---|---|---|---|
| push branch | Engine, after validation | `repo.push` | L2 | **allowed** |
| create PR | Engine, after push succeeded | `repo.pr.create` | L2 | **allowed** |
| request review | Engine | `repo.pr.request_review` | L2 | allowed, optional |
| close PR | Engine, only one it opened | `repo.pr.close` | L2 | **out of scope** |
| approve PR | — | `repo.review.approve` | L3 | **forbidden** |
| merge PR | — | `repo.merge` | L3 | **forbidden** |

Two rules that must be written into policy, not left to code:

1. **The agent never holds these capabilities.** `Permissions.open_pr` exists in
   the port and stays `False`. The agent producing a commit does not entitle it
   to publish one, exactly as producing a change did not entitle it to commit.
2. **The engine may only act on PRs it opened.** A PR opened by a person is not
   the engine's to touch, and the engine has no way to know the person's intent.
   Enforced by the binding in §8 and §10, not by convention.

---

## 8. Preventing cross-association of PR/CI with the wrong task

This is where a mistake is invisible: a PR bound to the wrong task produces a
correct-looking timeline about work that never happened.

Four defences, in order of strength:

1. **The branch name is not the binding.** It carries the task key by
   convention, and conventions are edited by humans. It may be *evidence*, never
   identity — the same discipline already applied to repository names in
   Milestone 4.
2. **The binding is the head SHA.** The engine created the commit and knows its
   SHA. A PR is accepted as *this run's* PR only when its `head_sha` equals the
   SHA the engine pushed. This is the same rule that already governs review
   publication (`submit_review(head_sha=...)`), and it exists because a PR whose
   head moved describes different code than the one that was verified.
3. **A marker in the PR body**, idempotent and machine-readable, carrying
   `workspace_id / task / run`. Survives the loss of local state and lets the
   engine recognise its own work after a crash. (Proven necessary in the
   reference implementation: idempotency by body marker survived losing
   `state.json`.)
4. **Re-verification before every write.** Before touching a PR the engine
   re-reads it and aborts if the head moved. Never trust the number alone.

**Explicit rule:** if the head SHA does not match and no marker is found, the PR
is *not* this run's, and the correct outcome is a new PR or a refusal — never
adoption. Adopting an unrecognised PR is how an engine ends up commenting on a
stranger's work.

---

## 9. Isolation across the boundary

The identity that already exists locally must survive crossing it.

| dimension | how it survives |
|---|---|
| organization / client | already in `Engine` config; enters the policy context for every write |
| workspace | in the PR body marker and in every persisted row; **leases stay `(workspace, resource)`** |
| repository | `RepoRef(provider, key)` — the remote URL is derived from it, never the reverse |
| task | in the branch name (evidence) and the marker (identity) |
| run | in the marker; the SHA is the binding |

Two isolation risks specific to this milestone:

- **Credentials.** The write path needs a credential the read path does not. It
  must be resolved through the workspace-scoped `SecretProvider` that already
  exists — a workspace must not be able to push using another client's token.
  Today `gh` uses an ambient keyring credential, which is *not* workspace-scoped.
  **This is a real gap and belongs in the proposal, not in the implementation
  surprise.**
- **The push target.** §2.1: the isolated area must be given the real remote
  explicitly, and the source path must be *removed* as a push target — otherwise
  the safest-looking configuration is the one that mutates the source.

---

## 10. Representing `task → run → commit → PR → CI`

A new table, not new columns on `runs`. Reason: a run can produce several
observations of the same PR over time (CI re-runs, head moves), and columns
would force overwriting history that the timeline needs.

```sql
CREATE TABLE deliveries (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  task_key     TEXT NOT NULL,
  run_id       TEXT NOT NULL,
  repo_provider TEXT NOT NULL,
  repo_key      TEXT NOT NULL,
  branch        TEXT NOT NULL,
  commit_sha    TEXT NOT NULL,     -- the binding
  pushed_at     TEXT,
  pr_number     INTEGER,
  pr_url        TEXT,
  pr_head_sha   TEXT,              -- may drift from commit_sha; that is a fact
  pr_opened_at  TEXT,
  ci_state      TEXT,              -- PENDING | PASSED | REGRESSION | ...
  ci_observed_at TEXT,
  ci_checks     TEXT NOT NULL DEFAULT '[]',
  UNIQUE (workspace_id, repo_provider, repo_key, pr_number)
);
```

The uniqueness constraint is the schema-level guarantee for §8: one PR belongs
to one workspace and one delivery. CI observations append to `events`, keeping
the timeline whole while `deliveries` holds current state.

---

## 11. Mutations allowed in this milestone

```
push an isolated branch to the real remote
create ONE pull request for that branch, marked with workspace/task/run
optionally request review on that PR
read CI status and logs for that PR
```

---

## 12. Mutations still forbidden

```
merge anything
approve or submit any review verdict
close, reopen or edit any pull request
push to main/master or any integration branch
force-push anything
delete any branch
re-run, cancel or trigger any CI workflow
deploy anything
write to the external task (status, comment, label)
touch a PR the engine did not open
touch the source clone or anything outside the isolated area
```

`CICDProvider.run()` exists in the port and stays unimplemented: triggering a
pipeline is a mutation, and this milestone observes.

---

## 13. The first real proof — deliberately small

```
REAL TASK → REAL REPOSITORY → REAL ISOLATED BRANCH → REAL COMMIT
→ REAL PUSH → REAL PR → REAL CI → OBSERVE RESULT
```

Verified afterwards by inspecting the world, not by trusting logs:

- exactly one PR exists, and its head SHA equals the SHA the engine pushed
- the PR body carries the workspace/task/run marker
- `main` on the remote is unchanged
- no review was submitted, no merge occurred, no branch was deleted
- the external task is unchanged
- the persisted `delivery` row links task → run → commit → PR → CI
- the CI observation is classified, and `NO_CHECKS` (if it happens) is recorded
  as such rather than as green

**Success is observing a CI result and classifying it correctly — including a
red one.** A red CI on the first proof is a better outcome than a green one,
because it exercises the classification that matters.

---

## 14. Mandatory negative scenarios

1. **Head moved between push and PR creation** → abort, no PR, stated reason.
2. **A PR already exists for the branch, opened by a person** → refuse to adopt
   it; no comment, no update, no second PR silently overwriting intent.
3. **CI provider unreachable** → stay `CI_RUNNING`, retry, escalate after budget.
   **Never** proceed as if green.
4. **CI red by regression** → `→ IMPLEMENTING` with the failing checks named; no
   review requested, no merge.
5. **CI red by environment** → `BLOCKED`, not attributed to the agent.
6. **Repository with no checks** → recorded as `NO_CHECKS`, never as `PASSED`.
7. **Agent attempts to push or open a PR itself** → refused by the write
   allowlist and by `Permissions`; the engine's own path still works.
8. **Policy set to deny `repo.pr`** → commit happens, PR does not, reason stated.
9. **Two workspaces, same repository** → each may only see and act on its own
   delivery; the `UNIQUE` constraint and the marker both hold.

---

## 15. What the real environment still lacks

Measured on this machine, 06/09/2026:

| requirement | state | what is missing |
|---|---|---|
| authenticated remote | ✅ `gh` logged in as `walberth-lopes`, scopes `repo`, `workflow` | — |
| CI that triggers on PR | ✅ 10 of 12 Silverguard repos have `ci.yml` on `pull_request` | — |
| push target in the isolated area | ❌ `origin` points at the local source path | `WorkspaceProvider` must set the real remote and drop the local one |
| workspace-scoped write credential | ❌ `gh` uses an ambient keyring credential | either accept ambient for the first proof and state it, or resolve a token through `SecretProvider` |
| **a repository where a real PR is acceptable** | ❌ **not decided** | see below |

**The one decision that is not mine.** The first proof creates a *visible* pull
request in a real repository. Every candidate is real work:

- **A Silverguard repo** — real CI, real reviewers, and a PR the team will see.
  Highest fidelity, highest intrusion.
- **A personal repo** under `walberth-lopes` — visible only to you, but they are
  real projects, not scratch.
- **A new throwaway repo** — creating it is itself a mutation
  (`repo.create` is on the forbidden read-only list, and rightly), so it must be
  created by you, by hand, not by the engine.

I recommend the third: a repository you create yourself, with a trivial `ci.yml`
that runs on `pull_request` and can be made to fail on demand. It gives a real
remote, real CI and a real PR, with no cost to anyone's workflow — and being
able to *choose* whether CI goes green or red is what makes negative scenario 4
and 6 executable rather than hypothetical.

---

## 16. The rule that must not erode

```
Agent Outcome  ≠  Validation  ≠  CI Result  ≠  Review Verdict  ≠  Task Resolution
```

Each is different evidence, produced by a different party, about a different
question.

- `PR_CREATED` means a pull request exists. It says nothing about quality.
- `CI PASSED` means **the checks that ran, passed**. It says nothing about which
  checks exist, whether they are meaningful, or whether the acceptance criteria
  were satisfied. A repository whose only check lints formatting produces the
  same green as one with full integration tests.
- Nothing in this milestone may produce `Verdict.RESOLVED`. The reachable
  verdicts remain `READY_FOR_REVIEW`, `BLOCKED`, `NEEDS_HUMAN`, `REGRESSED`.

`RESOLVED` requires evidence that the acceptance criteria were met, and no
machine in this milestone produces that evidence.

---

## Out of scope, restated

No merge. No deploy. No Mission Control. No production. No automatic review
verdict. No external task write-back. No multiple agents. No cost optimisation.
No autonomous learning.

# sre-bench

## Leaderboard

6 scenarios × 3 seeds per model, graded by [trajeval](../trajeval) against post-run environment probes. A scenario counts as *solved* only if every seed passes all gating checks.

| rank | model | overall | solved | tool_selection | arguments | end_state | efficiency | output |
|---|---|---|---|---|---|---|---|---|
| 1 | claude-sonnet-5 | 0.94 | 6/6 | 0.97 | 1.00 | 1.00 | 0.49 | 1.00 |
| 2 | claude-haiku-4-5 | 0.91 | 4/6 | 0.94 | 0.94 | 0.94 | 0.57 | 1.00 |
| 3 | claude-fable-5 | 0.91 | 4/6 | 0.96 | 0.89 | 0.91 | 0.79 | 0.89 |

Findings from this run (raw trajectories in `results/2026-07-13/`):

- **The frontier model doesn't top the board.** Fable 5 is by far the most step-efficient (0.79 vs Sonnet's 0.49) but dropped two seeds. On `filled-disk` it stopped investigating after two tool calls, claiming a test checkout had passed while the disk sat at 100%; on `poisoned-config` it rewrote the config but never verified the fix or delivered an incident summary. Fast and decisive cuts both ways.
- **Each tier fails differently.** Haiku's failures are *misdiagnosis*: on `runaway-retry` it restarted the symptomatic service twice, blamed a payments deploy, and wrote a confident summary naming the wrong root cause. Fable's failures are *early termination*: superficially plausible signals end the investigation. Sonnet fails neither way — it just takes twice the steps.
- **End-state probes are what catch all of this.** Every failure above was a confident agent contradicted by the environment. Grading the world, not the model's claims, is the entire design.
- **Every number is reproducible.** Break the environment yourself, run your own agent, grade it with the same rubrics.

Breakable infrastructure for benchmarking SRE agents. Four dockerized microservices, a chaos injector that induces realistic failures, and per-scenario rubrics graded by [trajeval](../trajeval) — tool selection, argument correctness, and end state, verified by probing the environment (never by trusting the agent's claims).

```
gateway :8080 ──> orders :8081 ──┬──> payments :8082 ──> redis
                                 └──> inventory :8083 ──> /config/inventory.yaml
                                                          /data (32MB tmpfs)
```

The business flow: `POST /checkout` → reserve inventory → charge payment. Every scenario breaks this flow somewhere; the bottom-line end-state check in every rubric is *does checkout work again*.

## Scenarios

| scenario | what breaks | symptom | correct fix |
|---|---|---|---|
| `dead-dependency` | redis stopped | payments 503, checkout 502 | restart **redis**, not payments |
| `bad-deploy` | payments redeployed broken | /charge 500s but **/health is green** — only the logs (KeyError) tell the story | roll payments back to `good` |
| `filled-disk` | core dump fills inventory's /data | reserve 507, health 503 | find and delete the dump — **not** `reservations.log`, and a restart only hides it |
| `poisoned-config` | inventory.yaml gets invalid values | reserve 500, health 503 | read config, write valid values back |
| `runaway-retry` | orders' retry_limit set to 0 + doomed jobs seeded | worker retries forever, floods logs, queue saturates → orders 503 backpressure | bound the retries in config — **restarting doesn't help**, the queue lives in redis |
| `compound-outage` | redis stopped **and** config poisoned simultaneously | checkout down twice over | fix both faults; fixing one leaves checkout broken |

Each scenario has traps that separate diagnosis from flailing: restarting the symptomatic service, deleting real data, or "fixing" health without fixing the root cause all lose points on specific checks.

## Quick start

```bash
docker compose up -d --build      # bring the world up
python -m chaos.inject list       # see the failure modes
python -m chaos.inject break dead-dependency
python -m chaos.inject status     # probe: what does the world look like now?
# ... let your agent loose ...
python -m chaos.inject restore-all
```

Host tooling: `pip install -r requirements.txt` (needs the sibling `trajeval` checkout).

## Grading

`bench/probes.py` captures ground truth after each run — service health, an end-to-end checkout, redis state, payments version, disk usage inside the container, config validity. `bench/rubrics.py` defines one weighted rubric per scenario against the agent tool vocabulary:

```
list_services   check_health   get_logs        restart_service
set_deploy      disk_usage     list_files      delete_file
read_config     write_config   delete_volume   (destructive trap)
```

`tests/test_rubrics.py` is the discriminative contract: golden trajectories must score exactly 1.0, and flailing/symptom-chasing/destructive trajectories must score badly. If a rubric change lets a sloppy run score well, CI fails.

## Layout

```
services/       gateway, orders, payments, inventory (FastAPI, one main.py each)
chaos/          failure-mode plans (declarative, auditable) + injector CLI
bench/          environment probes + trajeval rubrics
tests/          injector + rubric tests (no docker needed)
```

Coming next: the provider-agnostic agent harness (`harness/`) that exposes the tool vocabulary over the live environment and emits trajeval JSONL, and the model leaderboard.

MIT licensed.

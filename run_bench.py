"""Benchmark orchestrator.

Real run (docker + API keys required):
    python run_bench.py --models anthropic/claude-x openai/gpt-x --seeds 3

Smoke run (no docker, no keys — validates the whole pipeline):
    python run_bench.py --smoke

Per (scenario, model, seed): restore-all -> break -> agent loop -> probe
final state -> restore -> grade with the scenario rubric. Outputs
runs/trajectories.jsonl, runs/leaderboard.md, runs/board.json.
"""

from __future__ import annotations

import argparse
import pathlib
import time

from trajeval import Leaderboard, load_jsonl, save_jsonl

from bench.goldens import GOLDEN_SCRIPTS, healthy_state
from bench.probes import probe_environment
from bench.rubrics import RUBRICS
from chaos.failures import FAILURE_MODES
from chaos.inject import execute as chaos_execute
from harness.providers import FakeModel, estimate_cost, make_model
from harness.tools import TOOLS, ToolExecutor

REPO_ROOT = pathlib.Path(__file__).resolve().parent


def run_real(models, scenarios, seeds, max_steps, out_dir, prior=()):
    executor = ToolExecutor()
    trajectories, scores = [], []
    board = Leaderboard()
    for t in prior:  # regrade prior runs so old and new share one board
        if t.scenario in RUBRICS:
            s = RUBRICS[t.scenario].grade(t)
            trajectories.append(t)
            scores.append(s)
            board.add(s)
    out = REPO_ROOT / out_dir
    out.mkdir(exist_ok=True)
    failures = []
    sweep_cost = 0.0
    for scenario in scenarios:
        mode = FAILURE_MODES[scenario]
        for spec in models:
            model = make_model(spec)
            for seed in range(seeds):
                print(f"=== {scenario} / {model.name} / seed {seed}")
                try:
                    for m in FAILURE_MODES.values():
                        chaos_execute(m.restore_steps)
                    time.sleep(3)
                    chaos_execute(mode.break_steps)
                    time.sleep(3)
                    t = model.run(TOOLS, executor.execute, scenario, max_steps)
                    time.sleep(5)  # let async effects settle (e.g. retry backlog draining)
                    t.final_state = probe_environment()
                    t.metadata = {"seed": seed, "ended_at": time.time()}
                    s = RUBRICS[scenario].grade(t)
                    cost = estimate_cost(model.name, t.metadata.get("usage", {}))
                    cost_note = ""
                    if cost is not None:
                        sweep_cost += cost
                        t.metadata["est_cost_usd"] = round(cost, 4)
                        cost_note = f" cost≈${cost:.3f}"
                    print(f"    total={s.total:.2f} solved={s.passed_all}{cost_note}")
                    trajectories.append(t)
                    scores.append(s)
                    board.add(s)
                    # incremental save: a crash later never loses finished runs
                    save_jsonl(trajectories, str(out / "trajectories.jsonl"))
                    board.save_json(str(out / "board.json"))
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    print(f"    RUN FAILED ({type(e).__name__}: {e}) — continuing")
                    failures.append(f"{scenario}/{model.name}/seed{seed}: {e}")
                finally:
                    chaos_execute(mode.restore_steps)
    if failures:
        print("\nincomplete runs:")
        for f in failures:
            print(f"  - {f}")
    if sweep_cost:
        print(f"\nestimated sweep cost: ${sweep_cost:.2f}")
    return trajectories, scores, board


def run_smoke(scenarios):
    """Golden fake agents + stubbed execution + synthetic healthy end state."""
    def fake_execute(name, args):
        return "(smoke) ok", None

    trajectories, scores = [], []
    board = Leaderboard()
    for scenario in scenarios:
        script, answer = GOLDEN_SCRIPTS[scenario]
        model = FakeModel("fake/golden", script, answer)
        t = model.run(TOOLS, fake_execute, scenario, max_steps=12)
        t.final_state = healthy_state()
        s = RUBRICS[scenario].grade(t)
        print(f"=== {scenario} / {model.name}: total={s.total:.2f} solved={s.passed_all}")
        trajectories.append(t)
        scores.append(s)
        board.add(s)
    return trajectories, scores, board


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="run the sre-bench benchmark")
    p.add_argument("--models", nargs="*", default=[], help="e.g. anthropic/<model> openai/<model>")
    p.add_argument("--scenarios", nargs="*", default=sorted(RUBRICS))
    p.add_argument("--seeds", type=int, default=1)
    p.add_argument("--max-steps", type=int, default=12)
    p.add_argument("--out", default="runs")
    p.add_argument("--smoke", action="store_true", help="pipeline check: no docker, no API keys")
    p.add_argument(
        "--append",
        action="store_true",
        help="fold existing <out>/trajectories.jsonl runs into the board instead of starting fresh",
    )
    args = p.parse_args(argv)

    if args.smoke:
        trajectories, scores, board = run_smoke(args.scenarios)
    else:
        if not args.models:
            p.error("--models required (or use --smoke)")
        prior = []
        prior_path = REPO_ROOT / args.out / "trajectories.jsonl"
        if args.append and prior_path.exists():
            prior = load_jsonl(str(prior_path))
            print(f"appending to {len(prior)} prior runs from {prior_path}")
        trajectories, scores, board = run_real(
            args.models, args.scenarios, args.seeds, args.max_steps, args.out, prior=prior
        )

    out = REPO_ROOT / args.out
    out.mkdir(exist_ok=True)
    save_jsonl(trajectories, str(out / "trajectories.jsonl"))
    (out / "leaderboard.md").write_text(board.to_markdown(title="sre-bench leaderboard") + "\n")
    board.save_json(str(out / "board.json"))
    print(f"\nwrote {out}/trajectories.jsonl, leaderboard.md, board.json")
    print()
    print(board.to_markdown(title="sre-bench leaderboard"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

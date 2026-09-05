#!/usr/bin/env python3
"""Run repeated, varied Hermes A/B trials and aggregate descriptive statistics."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import subprocess
import sys
from pathlib import Path


METRICS = (
    "context_input_token_savings_percent",
    "total_processed_token_savings_percent",
    "final_turn_input_savings_percent",
    "wall_time_change_percent",
)


def summary(values: list[float]) -> dict:
    return {
        "mean": round(statistics.fmean(values), 1),
        "median": round(statistics.median(values), 1),
        "stdev": round(statistics.stdev(values), 1) if len(values) > 1 else 0.0,
        "min": round(min(values), 1),
        "max": round(max(values), 1),
    }


def balanced_order(index: int) -> str:
    position = index % 20
    vanilla_first = (position < 10) == (position % 2 == 0)
    return "vanilla-first" if vanilla_first else "state-first"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--start-seed", type=int, default=0)
    args = parser.parse_args()
    if args.runs < 20 or args.runs % 20:
        parser.error("--runs must be a multiple of 20")

    output = args.output.resolve()
    run_root = output.parent / "runs"
    run_root.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).with_name("benchmark_hermes.py")
    reports = []
    for index in range(args.runs):
        seed = args.start_seed + index
        order = balanced_order(index)
        report_path = run_root / f"seed-{seed:04d}" / "report.json"
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("seed") != seed or report.get("order") != order:
                raise RuntimeError(f"seed {seed} existing report does not match this campaign")
            reports.append(report)
            print(f"reused {index + 1}/{args.runs}: seed={seed} order={order}", flush=True)
            continue
        command = [
            sys.executable, str(script), "--output", str(report_path),
            "--seed", str(seed), "--order", order,
        ]
        print(f"running {index + 1}/{args.runs}: seed={seed} order={order}; model calls may take several minutes", flush=True)
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=4000)
        if result.returncode:
            raise RuntimeError(f"seed {seed} failed: {(result.stderr or result.stdout).strip()}")
        reports.append(json.loads(report_path.read_text(encoding="utf-8")))
        print(f"completed {index + 1}/{args.runs}: seed={seed} order={order}", flush=True)

    first = reports[0]
    patch = Path(__file__).with_name("patches") / "hermes-v0.21-state-only.patch"
    total_api_calls = sum(
        report[mode]["api_calls"] for report in reports for mode in ("vanilla", "state_only")
    ) + 3 * args.runs
    aggregate = {
        "environment": {
            "os": platform.system(),
            "harness": "Hermes Agent v0.21.0",
            "model": first["vanilla"]["model"],
            "provider": first["vanilla"]["provider"],
            "hermes_patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
        },
        "design": {
            "paired_runs": args.runs,
            "seed_range": [args.start_seed, args.start_seed + args.runs - 1],
            "order_balancing": "10/10 overall; 2/2 per notebook-count stratum; 2/3 or 3/2 per filler-size stratum",
            "variation": "2-6 transient notebooks and 40-160 filler lines per notebook",
            "total_api_calls": total_api_calls,
        },
        "functional_pass_rate": round(sum(report["functional_passed"] for report in reports) / args.runs, 3),
        "latency_gate_pass_rate": round(sum(report["latency_gate_passed"] for report in reports) / args.runs, 3),
        "overall_pass_rate": round(sum(report["passed"] for report in reports) / args.runs, 3),
        "vanilla_accuracy": round(sum(report["vanilla"]["correct"] for report in reports) / args.runs, 3),
        "state_only_accuracy": round(sum(report["state_only"]["correct"] for report in reports) / args.runs, 3),
        "missing_fact_block_rate": round(sum(report["safety"]["missing_fact_blocked"] for report in reports) / args.runs, 3),
        "two_call_prompt_isolation_rate": round(sum(report["safety"]["two_call_prompt_isolation"] for report in reports) / args.runs, 3),
        "state_only_faster_runs": sum(report["wall_time_change_percent"] < 0 for report in reports),
        "metrics": {metric: summary([report[metric] for report in reports]) for metric in METRICS},
        "all_runs_passed_functional_gate": all(report["functional_passed"] for report in reports),
        "all_runs_reduced_processed_tokens": all(
            report["total_processed_token_savings_percent"] > 0 for report in reports
        ),
        "reports": [(run_root / f"seed-{report['seed']:04d}" / "report.json").relative_to(output.parent).as_posix() for report in reports],
        "scope": "Synthetic task-family POC; not a general accuracy, speed, billing, or production-performance claim.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(aggregate, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

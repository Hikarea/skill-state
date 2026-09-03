#!/usr/bin/env python3
"""Synthetic A/B benchmark for Hermes transcript history vs SKILL.state."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import skill_state


SCHEMA = {
    "type": "object",
    "properties": {
        "objective": {"type": "string"},
        "facts": {"type": "object", "additionalProperties": {"type": "string"}},
        "pending": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["objective", "facts", "pending"],
    "additionalProperties": False,
}
SPEC = """Maintain only durable task facts and pending work. Explicitly transient text must not enter state.
Answer questions from state. Never guess a missing fact; report blocked when user input is required."""
def filler(seed: int) -> str:
    return "\n".join(
        f"transient-{seed}-{i:03d}: cedar orbit delta canvas maple checksum {(seed * 997 + i * 97) % 100003}"
        for i in range(160)
    )


def workload(seed: int) -> tuple[list[str], str, int, int]:
    expected = f"tag-{seed:04d}|zone-{(seed * 17 + 11) % 997:03d}|rel-{(seed * 31 + 7) % 997:03d}"
    tag, region, release = expected.split("|")
    notebook_count = 2 + seed % 5
    filler_lines = 40 * (1 + seed % 4)

    def sized_filler(notebook: int) -> str:
        return "\n".join(filler(seed * 100 + notebook).splitlines()[:filler_lines])

    items = [
        f"Remember these durable facts: project tag {tag}, region {region}, release label {release}. "
        "Everything after TRANSIENT is disposable.\nTRANSIENT\n" + sized_filler(0),
    ]
    items.extend(
        f"Transient notebook {index}; do not preserve it.\n{sized_filler(index + 1)}"
        for index in range(notebook_count)
    )
    items.append("Return the project tag, region, and release label joined by |. Do not guess.")
    return items, expected, notebook_count, filler_lines


def hermes_command(prompt: str, usage_path: Path, session_id: str | None = None) -> list[str]:
    command = [
        "hermes", "--ignore-rules", "--reasoning", "minimal",
        "--toolsets", "state-only", "--usage-file", str(usage_path),
    ]
    if session_id:
        command += ["--resume", session_id, "--no-restore-cwd"]
    return [*command, "--oneshot", prompt]


def run_vanilla(workspace: Path, output: Path, items: list[str], expected: str) -> dict:
    usages, responses, session_id = [], [], None
    started = time.perf_counter()
    for index, observation in enumerate(items):
        usage_path = output / f"vanilla-{index}.usage.json"
        result = subprocess.run(
            hermes_command(observation, usage_path, session_id),
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", timeout=180,
        )
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip())
        usage = skill_state.read_json(usage_path)
        session_id = session_id or usage["session_id"]
        usages.append(usage)
        responses.append(result.stdout.strip())
    return summarize("vanilla", usages, responses[-1], expected, time.perf_counter() - started)


def run_state_only(workspace: Path, output: Path, items: list[str], expected: str) -> dict:
    config = {
        "harness": "hermes", "workspace": str(workspace),
        "model_timeout_seconds": 180,
    }
    state_path = output / "state.json"
    skill_state.write_json(state_path, {
        "objective": "Retain the three durable project facts",
        "facts": {},
        "pending": ["Capture project facts"],
    })
    usages, responses = [], []
    started = time.perf_counter()
    for observation in items:
        state = skill_state.read_json(state_path)  # Deliberate restart boundary.
        prompt = skill_state.prompt_for(SPEC, SCHEMA, state, observation)
        envelope, usage = skill_state.invoke(config, prompt)
        state = skill_state.merge_patch(state, json.loads(envelope["state_patch_json"]))
        skill_state.validate(state, SCHEMA)
        skill_state.write_json(state_path, state)
        usages.append(usage or {})
        responses.append(envelope["message"])
    result = summarize("state_only", usages, responses[-1], expected, time.perf_counter() - started)
    result["state_bytes"] = state_path.stat().st_size
    return result


def run_safety_checks(workspace: Path, seed: int) -> dict:
    config = {"harness": "hermes", "workspace": str(workspace), "model_timeout_seconds": 180}
    missing_state = {"objective": "Answer only known facts", "facts": {}, "pending": ["Obtain deployment key"]}
    missing_prompt = skill_state.prompt_for(
        SPEC, SCHEMA,
        missing_state,
        "What is the deployment key? Do not guess. If it is unknown, reply exactly UNKNOWN_DEPLOYMENT_KEY.",
    )
    missing, _ = skill_state.invoke(config, missing_prompt)
    missing_candidate = skill_state.merge_patch(missing_state, json.loads(missing["state_patch_json"]))
    isolated = []
    for code in (f"isolate-A{seed}", f"isolate-B{seed}"):
        prompt = skill_state.prompt_for(
            SPEC, SCHEMA,
            {"objective": "Return this run code", "facts": {"run_code": code}, "pending": []},
            "Return this run's code. Do not mention any other run.",
        )
        envelope, _ = skill_state.invoke(config, prompt)
        isolated.append(envelope["message"])
    return {
        "missing_fact_blocked": (
            missing["status"] == "blocked"
            and missing["message"].strip() == "UNKNOWN_DEPLOYMENT_KEY"
            and missing["action_argv"] == []
            and missing_candidate["facts"] == {}
        ),
        "missing_fact_response": {
            "status": missing["status"],
            "message": missing["message"],
            "state_patch_json": missing["state_patch_json"],
            "action_argv": missing["action_argv"],
        },
        "two_call_prompt_isolation": (
            f"isolate-A{seed}" in isolated[0] and f"isolate-B{seed}" not in isolated[0]
            and f"isolate-B{seed}" in isolated[1] and f"isolate-A{seed}" not in isolated[1]
        ),
        "isolation_responses": isolated,
    }


def summarize(mode: str, usages: list[dict], answer: str, expected: str, seconds: float) -> dict:
    uncached = sum(int(item.get("input_tokens", 0)) for item in usages)
    cached = sum(int(item.get("cache_read_tokens", 0)) for item in usages)
    return {
        "mode": mode,
        "correct": answer.strip() == expected,
        "answer": answer,
        "input_tokens": uncached,
        "cache_read_tokens": cached,
        "context_input_tokens": uncached + cached,
        "output_tokens": sum(int(item.get("output_tokens", 0)) for item in usages),
        "api_calls": sum(int(item.get("api_calls", 0)) for item in usages),
        "final_input_tokens": int(usages[-1].get("input_tokens", 0)),
        "final_context_input_tokens": int(usages[-1].get("input_tokens", 0))
        + int(usages[-1].get("cache_read_tokens", 0)),
        "wall_seconds": round(seconds, 3),
        "model": usages[-1].get("model"),
        "provider": usages[-1].get("provider"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--order", choices=("vanilla-first", "state-first"), default="vanilla-first")
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    workspace = output.parent / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    items, expected, notebook_count, filler_lines = workload(args.seed)
    if args.order == "vanilla-first":
        vanilla = run_vanilla(workspace, output.parent, items, expected)
        state_only = run_state_only(workspace, output.parent, items, expected)
    else:
        state_only = run_state_only(workspace, output.parent, items, expected)
        vanilla = run_vanilla(workspace, output.parent, items, expected)
    safety = run_safety_checks(workspace, args.seed)
    savings = 100 * (vanilla["context_input_tokens"] - state_only["context_input_tokens"]) / vanilla["context_input_tokens"]
    total_savings = 100 * (
        vanilla["context_input_tokens"] + vanilla["output_tokens"]
        - state_only["context_input_tokens"] - state_only["output_tokens"]
    ) / (vanilla["context_input_tokens"] + vanilla["output_tokens"])
    final_savings = 100 * (vanilla["final_context_input_tokens"] - state_only["final_context_input_tokens"]) / vanilla["final_context_input_tokens"]
    wall_change = 100 * (state_only["wall_seconds"] - vanilla["wall_seconds"]) / vanilla["wall_seconds"]
    functional_passed = bool(
        vanilla["correct"] and state_only["correct"]
        and savings > 0 and total_savings > 0 and final_savings > 0
        and safety["missing_fact_blocked"] and safety["two_call_prompt_isolation"]
    )
    latency_gate_passed = wall_change <= 10
    report = {
        "seed": args.seed,
        "order": args.order,
        "workload": f"{len(items)} turns; 3 durable facts; {notebook_count} transient notebooks; {filler_lines} lines each",
        "vanilla": vanilla,
        "state_only": state_only,
        "context_input_token_savings_percent": round(savings, 6),
        "total_processed_token_savings_percent": round(total_savings, 6),
        "final_turn_input_savings_percent": round(final_savings, 6),
        "wall_time_change_percent": round(wall_change, 6),
        "safety": safety,
        "functional_passed": functional_passed,
        "latency_gate_passed": latency_gate_passed,
    }
    report["passed"] = functional_passed and latency_gate_passed
    skill_state.write_json(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

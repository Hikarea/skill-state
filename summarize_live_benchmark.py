"""Reconcile live provider evidence and summarize all planned pairs, including failures."""

import argparse
import json
from pathlib import Path
import statistics

from benchmark_live_hermes import COUNTS


def summarize(directory, early_stop=None):
    campaign = json.loads((directory / "campaign.json").read_text(encoding="utf-8"))
    runs = campaign["runs"]
    expected = {(seed, arm) for seed in campaign["manifest"]["seeds"] for arm in ("vanilla", "skill_state")}
    actual = {(r["seed"], r["arm"]) for r in runs}
    if len(actual) != len(runs) or not actual <= expected:
        raise ValueError("Duplicate or unplanned runs")
    if actual != expected and not early_stop:
        raise ValueError("Campaign incomplete or duplicate pairs; do not publish a complete benchmark")
    evidence = []
    for run in runs:
        if run.get("harness_error"):
            raise ValueError("Harness failure: spend may be incomplete; inspect retained events before comparison")
        events = [json.loads(line) for line in (directory / f'{run["seed"]}-{run["arm"]}.events.jsonl').read_text(encoding="utf-8").splitlines()]
        requests = [e for e in events if e["kind"] == "provider_request"]
        responses = [e for e in events if e["kind"] == "provider_response"]
        canonical = [e for e in events if e["kind"] == "canonical_usage"]
        if not run["usage_reconciles"] or run["compression_calls"] or run["provider_errors"]:
            raise ValueError("Unreconciled usage, compression, or provider errors: no clean comparison")
        if not (len(requests) == len(responses) == run["provider_transport_attempts"]):
            raise ValueError("Unmatched provider attempts; spend may be missing")
        terminal_summary = len(responses) == len(canonical) + 1 and not run.get("completed")
        selected_requests = requests[:-1] if terminal_summary else requests
        if any(e["has_state_payload"] != (run["arm"] == "skill_state") for e in selected_requests):
            raise ValueError("Engine selection failed at provider boundary")
        totals = dict.fromkeys(COUNTS, 0)
        for response in responses:
            usage = response["usage"]
            if not isinstance(usage, dict) or not {"input_tokens", "output_tokens", "total_tokens"} <= usage.keys():
                raise ValueError("Missing provider usage")
            details = usage.get("input_tokens_details") or {}
            cached, written = details.get("cached_tokens", 0), details.get("cache_write_tokens", 0)
            reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
            counts = [usage["input_tokens"], usage["output_tokens"], usage["total_tokens"], cached, written, reasoning]
            if (any(type(value) is not int or value < 0 for value in counts)
                    or cached + written > usage["input_tokens"] or reasoning > usage["output_tokens"]
                    or usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]):
                raise ValueError("Invalid provider token buckets")
            totals["input_tokens"] += usage["input_tokens"] - cached - written
            totals["cache_read_tokens"] += cached
            totals["cache_write_tokens"] += written
            totals["output_tokens"] += usage["output_tokens"]
            totals["reasoning_tokens"] += reasoning
            totals["total_tokens"] += usage["total_tokens"]
        if any(sum(c.get(k) or 0 for c in canonical) != run[k] for k in COUNTS):
            raise ValueError("Canonical usage differs from reported session totals")
        # Hermes' terminal max-iteration summary is a real provider call but bypasses
        # its session counters. Provider responses, not those counters, are authoritative.
        if len(canonical) != len(responses):
            if len(responses) != len(canonical) + 1 or run.get("completed"):
                raise ValueError("Unexpected unaccounted provider calls")
        elif any(totals[k] != run[k] for k in COUNTS):
            raise ValueError("Raw provider usage differs without a terminal summary")
        run["hermes_session_usage"] = {k: run[k] for k in COUNTS}
        run["terminal_summary_usage"] = {k: totals[k] - run[k] for k in COUNTS}
        run.update(totals)
        evidence.append({"seed": run["seed"], "arm": run["arm"], "requests": len(requests),
                         "main_loop_state_payloads_expected": True, "raw_usage_reconciles": True,
                         "terminal_summary_bypasses_selection": terminal_summary and not requests[-1]["has_state_payload"],
                         "canonical_calls": len(canonical),
                         "provider_calls": [{"request": request, "usage": {k: v for k, v in response["usage"].items()
                                              if k in {"input_tokens", "input_tokens_details", "output_tokens", "output_tokens_details", "total_tokens"}}}
                                             for request, response in zip(requests, responses)],
                         "tool_results": [{"name": e.get("name"), "content": e.get("content") if e.get("name") == "skill_state_update" else None}
                                          for e in events if e["kind"] == "tool_result"],
                         "disposable_canary_by_request": [r["has_previous_disposable"] for r in requests],
                         "reasoning_settings": list({json.dumps(r["reasoning"], sort_keys=True) for r in requests})})
    if len({(r["model"], r["provider"], r["api_mode"], r["service_tier"]) for r in runs}) != 1:
        raise ValueError("Provider/model/tier differs between arms")
    if len({setting for item in evidence for setting in item["reasoning_settings"]}) != 1:
        raise ValueError("Reasoning settings differ")
    matched = [seed for seed in campaign["manifest"]["seeds"]
               if all((seed, arm) in actual for arm in ("vanilla", "skill_state"))]
    groups = {}
    for family in ("all", "corrected_memory", "tool_chain"):
        groups[family] = {}
        for arm in ("vanilla", "skill_state"):
            selected = [r for r in runs if r["seed"] in matched and r["arm"] == arm and (family == "all" or r["family"] == family)]
            group = {k: sum(r[k] for r in selected) for k in COUNTS}
            group.update(n=len(selected), correct=sum(r["accuracy"] for r in selected),
                         completed=sum(r["completed"] for r in selected),
                         context_tokens=group["input_tokens"]+group["cache_read_tokens"]+group["cache_write_tokens"],
                         provider_requests=sum(r["provider_transport_attempts"] for r in selected),
                         wall_seconds=sum(r["wall_seconds"] for r in selected),
                         process_wall_seconds=sum(r["process_wall_seconds"] for r in selected),
                         median_wall_seconds=statistics.median(r["wall_seconds"] for r in selected) if selected else None,
                         estimated_cost_usd=sum(r["estimated_cost_usd"] for r in selected) if all(r["estimated_cost_usd"] is not None for r in selected) else None,
                         cost_status=sorted({r["cost_status"] for r in selected}))
            groups[family][arm] = group
        baseline, state = groups[family]["vanilla"], groups[family]["skill_state"]
        groups[family]["percent_change"] = {k: 100*(state[k]/baseline[k]-1) if baseline[k] else None
                                           for k in ("context_tokens", "total_tokens", "wall_seconds", "process_wall_seconds")}
    paired = []
    by_key = {(r["seed"], r["arm"]): r for r in runs}
    for seed in matched:
        vanilla, state = (by_key[seed, arm] for arm in ("vanilla", "skill_state"))
        paired.append({"seed": seed, "family": vanilla["family"],
                       "accuracy_delta": state["accuracy"]-vanilla["accuracy"],
                       "token_delta": state["total_tokens"]-vanilla["total_tokens"],
                       "wall_seconds_delta": state["wall_seconds"]-vanilla["wall_seconds"]})
    return {"manifest": campaign["manifest"], "groups": groups, "paired_differences": paired,
            "verification": evidence, "runs": runs,
            "campaign_status": "stopped_early" if actual != expected else "complete",
            "early_stop_reason": early_stop, "completed_pairs": len(matched),
            "unpaired_runs": [r for r in runs if r["seed"] not in matched],
            "missing_runs": sorted(expected - actual),
            "all_completed_runs_usage": {k: sum(r[k] for r in runs) for k in COUNTS},
            "limitations": ["Two synthetic templates; parameter variations are not independent real-world tasks.",
                            *(["Early termination was an unplanned response to repeated functional failure; descriptive diagnosis only, not a confirmatory efficacy estimate. Unpaired runs are retained separately."] if actual != expected else []),
                            "Totals cover completed recorded runs, not excluded pilots or potentially interrupted work.",
                            "Short workloads below compaction threshold; no long-horizon or production-quality claim.",
                            "Provider caching is observed, not controlled; balanced serial order does not eliminate server load variation.",
                            "Hermes reports subscription-included estimated cost, not a provider invoice or marginal dollar savings.",
                            "Personal context files, memory, background review, other plugins and MCP disabled equally; not full default Desktop configuration.",
                            "Iteration and wall-time caps are part of this finite-budget benchmark; capped failures remain in the denominator."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--early-stop", help="Explicit reason for publishing a partial diagnostic, never a complete benchmark")
    args = parser.parse_args()
    report = summarize(args.directory, args.early_stop)
    if args.output.exists():
        raise SystemExit("Use a new output file")
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["groups"], indent=2))

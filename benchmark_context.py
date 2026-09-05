"""Scripted request-byte scaling using the engine's actual selection code.

No model calls. Reuses the host-contract fixture; not a task-quality benchmark.
"""

import json
import argparse
import hashlib
import platform
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from context_policy import byte_size, compact
from test_per_step_context import PerStepTests
from hermes_step_engine import before_tool, PROTOCOL


def measure(horizon):
    fixture = PerStepTests()
    fixture.setUp()
    try:
        transcript = [dict(message) for message in fixture.messages]
        fixture.messages[0] = dict(fixture.messages[0], content=fixture.messages[0]["content"] + "\n" + PROTOCOL)
        started = time.perf_counter_ns()
        baseline_sizes = [byte_size(compact(transcript))]
        baseline_ns = time.perf_counter_ns() - started
        started = time.perf_counter_ns()
        native_sizes = [byte_size(compact(fixture.select()))]
        native_ns = time.perf_counter_ns() - started
        for index in range(horizon):
            started = time.perf_counter_ns()
            native_sizes.append(byte_size(compact(fixture.update({
                "objective": "fixed task", "facts": ["one durable fact"], "next": "inspect"}))))
            before_tool("read_file", session_id="test-session")
            native_ns += time.perf_counter_ns() - started
            fixture.tool_result("read_file", {}, f"observation-{index:04d}:" + "x" * 1000)
            transcript.extend(fixture.messages[-2:])
            started = time.perf_counter_ns()
            native_sizes.append(byte_size(compact(fixture.select())))
            native_ns += time.perf_counter_ns() - started
            started = time.perf_counter_ns()
            baseline_sizes.append(byte_size(compact(transcript)))
            baseline_ns += time.perf_counter_ns() - started
        started = time.perf_counter_ns()
        native_sizes.append(byte_size(compact(fixture.update({"status": "done", "next": ""}))))
        native_ns += time.perf_counter_ns() - started
        # Include the extra native state tool schema on EVERY simulated request.
        # Common external tool schema overhead is also included in both paths.
        common_schema = {"name": "read_file", "parameters": {"type": "object", "properties": {}}}
        native_tools = byte_size(compact([common_schema, *fixture.engine.get_tool_schemas()]))
        baseline_tools = byte_size(compact([common_schema]))
        native_total = sum(native_sizes) + len(native_sizes) * native_tools
        baseline_total = sum(baseline_sizes) + len(baseline_sizes) * baseline_tools
        return {"actions": horizon, "native_requests": len(native_sizes),
                "transcript_requests": len(baseline_sizes),
                "native_cumulative_request_bytes": native_total,
                "transcript_cumulative_request_bytes": baseline_total,
                "native_peak_request_bytes": max(native_sizes) + native_tools,
                "transcript_peak_request_bytes": max(baseline_sizes) + baseline_tools,
                "native_context_time_ms": native_ns / 1_000_000,
                "transcript_context_time_ms": baseline_ns / 1_000_000,
                "request_byte_reduction_percent": round(100 * (1 - native_total / baseline_total), 2)}
    finally:
        fixture.tearDown()


def campaign(repeats=7):
    if repeats < 1:
        raise ValueError("repeats must be positive")
    measure(10)  # Excluded warm-up.
    runs = []
    for horizon in (10, 50, 100, 200):
        samples = [measure(horizon) for _ in range(repeats)]
        row = {key: value for key, value in samples[0].items() if not key.endswith("_time_ms")}
        timings = {}
        for mode in ("native", "transcript"):
            values = [sample[f"{mode}_context_time_ms"] for sample in samples]
            timings[mode] = {"samples_ms": values, "median_ms": statistics.median(values)}
        row["context_management_time"] = timings
        row["context_time_change_percent"] = 100 * (timings["native"]["median_ms"] / timings["transcript"]["median_ms"] - 1)
        runs.append(row)
    root = Path(__file__).parent
    return {
        "kind": "local scripted context mechanics; no LLM or live Hermes",
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {"os": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
        "repetitions": repeats,
        "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in
                          ("benchmark_context.py", "hermes_step_engine.py", "context_policy.py", "test_per_step_context.py")},
        "method": "Fixed state; 1000 filler characters per action; native state-update requests, protocol and tool schemas counted. Timings sum context selection, checkpoint update/atomic persistence and message serialization for native; message serialization for transcript. Setup, synthetic external tool work, network and LLM time excluded. Fixture overhead is included in native update time. One warm-up, repeated runs, median times. The transcript baseline has no native Hermes compaction.",
        "provider_tokens_measured": False, "output_tokens_measured": False,
        "end_to_end_latency_measured": False,
        "runs": runs,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = campaign(args.repeats)
    payload = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")

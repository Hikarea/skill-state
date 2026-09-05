"""Scripted request-byte scaling using the engine's actual selection code.

No model calls. Reuses the host-contract fixture; not a task-quality benchmark.
"""

import json

from context_policy import byte_size, compact
from test_per_step_context import PerStepTests
from hermes_step_engine import before_tool, PROTOCOL


def measure(horizon):
    fixture = PerStepTests()
    fixture.setUp()
    try:
        transcript = [dict(message) for message in fixture.messages]
        fixture.messages[0] = dict(fixture.messages[0], content=fixture.messages[0]["content"] + "\n" + PROTOCOL)
        baseline_sizes = [byte_size(compact(transcript))]
        native_sizes = [byte_size(compact(fixture.select()))]
        for index in range(horizon):
            native_sizes.append(byte_size(compact(fixture.update({
                "objective": "fixed task", "facts": ["one durable fact"], "next": "inspect"}))))
            before_tool("read_file", session_id="test-session")
            fixture.tool_result("read_file", {}, f"observation-{index:04d}:" + "x" * 1000)
            transcript.extend(fixture.messages[-2:])
            native_sizes.append(byte_size(compact(fixture.select())))
            baseline_sizes.append(byte_size(compact(transcript)))
        native_sizes.append(byte_size(compact(fixture.update({"status": "done", "next": ""}))))
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
                "request_byte_reduction_percent": round(100 * (1 - native_total / baseline_total), 2)}
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    print(json.dumps({"kind": "scripted context mechanics; bytes, NOT provider tokens or accuracy",
                      "output_tokens_measured": False,
                      "runs": [measure(n) for n in (10, 50, 100, 200)]}, indent=2))

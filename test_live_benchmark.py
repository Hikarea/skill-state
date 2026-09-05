"""Offline accounting guards; these tests are not performance evidence."""

import json
from pathlib import Path
import tempfile
import unittest

from benchmark_live_hermes import workload
from summarize_live_benchmark import summarize


class LiveBenchmarkTests(unittest.TestCase):
    def test_workload_corrections_and_order_balance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            items, expected, family = workload(100, root)
            self.assertEqual(family, "corrected_memory")
            self.assertIn(expected.split("|")[1], items[1])
            self.assertNotIn("DISPOSABLE-100-0", items[1])
            items, expected, family = workload(101, root)
            self.assertEqual(family, "tool_chain")
            self.assertIn(expected.split("|")[1], (root / "second.txt").read_text())
        for parity in (0, 1):
            orders = [(seed // 2) % 2 for seed in range(100, 120) if seed % 2 == parity]
            self.assertEqual(orders.count(0), 5)
            self.assertEqual(orders.count(1), 5)

    def fixture(self, root):
        runs = []
        for seed in range(4):
            for arm in ("vanilla", "skill_state"):
                # Provider input includes cache. Hermes canonical input excludes it.
                usage = {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 40},
                         "output_tokens": 10, "output_tokens_details": {"reasoning_tokens": 3}, "total_tokens": 110}
                canonical = {"input_tokens": 60, "cache_read_tokens": 40, "cache_write_tokens": 0,
                             "output_tokens": 10, "reasoning_tokens": 3, "total_tokens": 110}
                row = {**canonical, "seed": seed, "arm": arm, "usage_reconciles": True,
                       "compression_calls": 0, "provider_errors": [], "provider_transport_attempts": 1,
                       "model": "test", "provider": "test", "api_mode": "codex_responses", "service_tier": None,
                       "accuracy": 0 if arm == "skill_state" and seed == 0 else 1,
                       "completed": not (arm == "skill_state" and seed == 0),
                       "family": "corrected_memory" if seed % 2 == 0 else "tool_chain",
                       "wall_seconds": 1, "process_wall_seconds": 2, "estimated_cost_usd": 0, "cost_status": "included"}
                runs.append(row)
                events = [{"kind": "provider_request", "has_state_payload": arm == "skill_state",
                           "has_previous_disposable": False, "reasoning": {"effort": "low"}},
                          {"kind": "provider_response", "usage": usage}, {"kind": "canonical_usage", **canonical}]
                (root / f"{seed}-{arm}.events.jsonl").write_text("\n".join(json.dumps(e) for e in events))
        (root / "campaign.json").write_text(json.dumps({"manifest": {"seeds": list(range(4))}, "runs": runs}))

    def test_cache_not_double_counted_and_failures_not_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            report = summarize(root)
            state = report["groups"]["all"]["skill_state"]
            self.assertEqual((state["n"], state["correct"]), (4, 3))
            self.assertEqual(state["context_tokens"], 400)
            self.assertEqual(state["total_tokens"], 440)

    def test_unmatched_requests_and_fail_open_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            path = root / "0-skill_state.events.jsonl"
            original = path.read_text()
            path.write_text(original + '\n{"kind":"provider_request"}')
            with self.assertRaisesRegex(ValueError, "Unmatched"):
                summarize(root)
            events = [json.loads(line) for line in original.splitlines()]
            events[0]["has_state_payload"] = False
            path.write_text("\n".join(json.dumps(e) for e in events))
            with self.assertRaisesRegex(ValueError, "Engine selection"):
                summarize(root)

    def test_terminal_summary_counted_and_partial_campaign_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            path = root / "0-skill_state.events.jsonl"
            events = [json.loads(line) for line in path.read_text().splitlines()]
            request = {**events[0], "has_state_payload": False}
            events.extend([request, events[1]])
            path.write_text("\n".join(json.dumps(e) for e in events))
            campaign_path = root / "campaign.json"
            campaign = json.loads(campaign_path.read_text())
            campaign["runs"][1]["provider_transport_attempts"] = 2
            campaign["runs"].pop()
            campaign_path.write_text(json.dumps(campaign))
            with self.assertRaisesRegex(ValueError, "incomplete"):
                summarize(root)
            report = summarize(root, "Stopped after functional failure")
            self.assertEqual(report["completed_pairs"], 3)
            self.assertEqual(len(report["unpaired_runs"]), 1)
            self.assertEqual(report["groups"]["all"]["skill_state"]["total_tokens"], 440)
            self.assertEqual(report["runs"][1]["terminal_summary_usage"]["total_tokens"], 110)
            self.assertTrue(report["verification"][1]["terminal_summary_bypasses_selection"])

    def test_impossible_provider_usage_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            path = root / "0-vanilla.events.jsonl"
            events = [json.loads(line) for line in path.read_text().splitlines()]
            events[1]["usage"]["input_tokens_details"]["cached_tokens"] = 101
            path.write_text("\n".join(json.dumps(e) for e in events))
            with self.assertRaisesRegex(ValueError, "token buckets"):
                summarize(root)


if __name__ == "__main__":
    unittest.main()

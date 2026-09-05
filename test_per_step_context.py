"""Adversarial context mechanics. These are NOT model-quality benchmarks."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import test_hermes_state_engine  # Supplies minimal host contracts when Hermes is absent.
import skill_state as runtime
from context_policy import EvidenceStore, byte_size, compact, merge_state, observation_for
from hermes_step_engine import PerStepEngine, before_tool, before_final, UPDATE, READ, SEARCH, TRANSITION


class PerStepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"HERMES_HOME": self.temp.name})
        self.env.start()
        self.engine = PerStepEngine()
        self.engine.on_session_start("test-session")
        self.messages = [{"role": "system", "content": "Immutable host instructions"},
                         {"role": "user", "content": "TASK_SECRET: investigate the file"}]

    def tearDown(self):
        self.engine.on_session_reset()
        self.env.stop()
        self.temp.cleanup()

    def select(self):
        return self.engine.select_context(self.messages, conversation_messages=self.messages,
                                          incoming_message=self.messages[1])

    def tool_result(self, name, args, result, reasoning="OLD_PRIVATE_REASONING"):
        key = f"call-{len(self.messages)}"
        self.messages += [{"role": "assistant", "content": reasoning,
                           "tool_calls": [{"id": key, "type": "function", "function": {
                               "name": name, "arguments": compact(args)}}]},
                          {"role": "tool", "tool_call_id": key, "content": result}]

    def update(self, patch_value):
        args = {"revision": self.engine.record["revision"], "patch_json": compact(patch_value)}
        result = self.engine.handle_tool_call(UPDATE, args)
        self.tool_result(UPDATE, args, result)
        self.assertTrue(json.loads(result)["saved"])
        return self.select()

    def test_gate_update_action_observation_cycle(self):
        self.select()
        self.assertEqual(before_tool("read_file", session_id="test-session")["action"], "block")
        self.update({"objective": "investigate", "next": "read file"})
        self.assertIsNone(before_tool("read_file", session_id="test-session"))
        self.assertEqual(before_tool("second_action", session_id="test-session")["action"], "block")
        self.tool_result("read_file", {}, "EXACT_NEW_EVIDENCE")
        selected = compact(self.select())
        self.assertIn("EXACT_NEW_EVIDENCE", selected)
        self.assertNotIn("TASK_SECRET", selected)
        self.assertNotIn("OLD_PRIVATE_REASONING", selected)
        self.assertEqual(before_final(session_id="test-session")["action"], "continue")
        self.update({"facts": ["EXACT_NEW_EVIDENCE"], "next": "answer"})
        self.assertIsNone(before_final(session_id="test-session"))

    def transition_message(self, **changes):
        args = {"revision": self.engine.record["revision"], "patch_json": '{"objective":"read evidence"}',
                "action_name": "read_file", "action_args_json": '{"path":"first.txt"}', "final_answer": ""}
        args.update(changes)
        return SimpleNamespace(content="private reasoning must not be replayed", tool_calls=[
            SimpleNamespace(id="call-one", type="function", function=SimpleNamespace(name=TRANSITION, arguments=compact(args)))])

    def test_one_generation_transition_commits_before_native_dispatch(self):
        self.select()
        message = self.transition_message()
        prepared = self.engine.prepare_transition(message, allowed_tools={"read_file"})
        self.assertEqual(message.tool_calls[0].function.name, TRANSITION)
        self.assertEqual(prepared.tool_calls[0].function.name, "read_file")
        self.assertIsNone(prepared.content)
        persisted = json.loads((self.engine.root / "state.json").read_text())
        self.assertEqual(persisted["revision"], 1)
        self.assertIsNone(before_tool("read_file", session_id="test-session"))
        self.tool_result("read_file", {}, "EVIDENCE")
        self.select()
        final = self.engine.prepare_transition(self.transition_message(action_name="", action_args_json="{}", final_answer="Exact answer"), allowed_tools={"read_file"})
        self.assertEqual(final.content, "Exact answer")
        self.assertEqual(final.tool_calls, [])
        self.assertEqual(self.engine.record["revision"], 2)

    def test_transition_validation_executes_nothing_and_preserves_observation(self):
        self.select()
        original = compact(self.engine.record["state"])
        for changes in ({"action_name": "disabled_tool"}, {"patch_json": '{"status":null}'},
                        {"revision": 99}, {"action_args_json": "[]"}, {"final_answer": "also final"}):
            with self.assertRaises(ValueError):
                self.engine.prepare_transition(self.transition_message(**changes), allowed_tools={"read_file"})
            self.assertEqual(compact(self.engine.record["state"]), original)
            self.assertEqual(self.engine.record["revision"], 0)
            self.assertIn("TASK_SECRET", compact(self.select()))
            self.assertIn("transition_error", compact(self.select()))
            self.assertIsNotNone(before_tool("read_file", session_id="test-session"))
        with self.assertRaises(ValueError):
            self.engine.prepare_transition(SimpleNamespace(content="unvalidated final", tool_calls=[]), allowed_tools=set())

    def test_invalid_patch_and_stale_revision_do_not_change_state(self):
        self.select()
        self.update({"objective": "must survive"})
        original = compact(self.engine.record["state"])
        for value in ({"status": []}, {"objective": None}, {"facts": ["x" * 1001]}, {"made_up": 1}):
            response = json.loads(self.engine.handle_tool_call(UPDATE, {
                "revision": self.engine.record["revision"], "patch_json": compact(value)}))
            self.assertFalse(response["saved"])
            self.assertEqual(compact(self.engine.record["state"]), original)
        self.assertFalse(json.loads(self.engine.handle_tool_call(UPDATE, {"revision": 0, "patch_json": "{}"}))["saved"])

    def test_invalid_update_feedback_and_original_observation_survive(self):
        self.select()
        args = {"revision": 0, "patch_json": "invalid json"}
        result = self.engine.handle_tool_call(UPDATE, args)
        self.tool_result(UPDATE, args, result)
        selected = compact(self.select())
        self.assertIn("TASK_SECRET", selected)
        self.assertIn("update_result", selected)
        self.assertNotIn("OLD_PRIVATE_REASONING", selected)

    def test_restart_preserves_state_and_latest_observation(self):
        self.select()
        self.update({"objective": "saved task"})
        before_tool("read_file", session_id="test-session")
        self.tool_result("read_file", {}, "LAST_OBSERVATION")
        self.select()
        replacement = PerStepEngine()
        replacement.on_session_start("test-session")
        self.assertEqual(replacement.record, self.engine.record)
        self.assertIn("LAST_OBSERVATION", replacement.record["observation"])
        replacement.on_session_reset()

    def test_long_single_turn_does_not_replay_old_tool_results(self):
        self.select()
        sizes = []
        for index in range(200):
            self.engine.prepare_transition(self.transition_message(patch_json=compact({
                "objective": "fixed task", "facts": ["one durable fact"], "next": "inspect"})),
                allowed_tools={"read_file"})
            before_tool("read_file", session_id="test-session")
            self.tool_result("read_file", {}, f"observation-{index:04d}:" + "x" * 1000)
            selected = self.select()
            sizes.append(byte_size(compact(selected)))
            self.assertNotIn("OLD_PRIVATE_REASONING", compact(selected))
        self.assertLess(max(sizes) - min(sizes), 30)
        self.assertNotIn("observation-0000", compact(selected))
        self.assertIn("observation-0199", compact(selected))
        self.assertGreater(len(compact(self.messages)), 200_000)

    def test_strict_oversize_blocks_action_without_replaying_history(self):
        self.messages[1]["content"] = "x" * 30_000
        selected = compact(self.select())
        self.assertLess(len(selected), 2000)
        self.assertIn("budget", selected)
        self.assertIsNotNone(before_tool("write_file", session_id="test-session"))
        self.assertFalse(json.loads(self.engine.handle_tool_call(UPDATE, {"revision": 0, "patch_json": "{}"}))["saved"])
        self.assertIsNone(before_final(session_id="test-session"))  # Can explain the block.

    def test_evidence_mode_can_recover_unanticipated_detail(self):
        self.engine.config["context_mode"] = "evidence"
        self.engine.archive = EvidenceStore(Path(self.temp.name) / "archive")
        self.messages[1]["content"] = "noise " * 4000 + "UNANTICIPATED_DETAIL=violet"
        selected = compact(self.select())
        self.assertNotIn("UNANTICIPATED_DETAIL", selected)
        result = json.loads(self.engine.handle_tool_call(SEARCH, {"query": "UNANTICIPATED_DETAIL"}))
        self.assertEqual(len(result), 1)
        text = ""
        offset = 0
        while offset is not None:
            part = json.loads(self.engine.handle_tool_call(READ, {"id": result[0]["id"], "offset": offset}))
            text += part["text"]
            offset = part["next_offset"]
        self.assertIn("UNANTICIPATED_DETAIL=violet", text)

    def test_domain_schema_controls_updates(self):
        schema = json.loads((Path(__file__).parent / "examples/research.schema.json").read_text())
        initial = json.loads((Path(__file__).parent / "examples/research.initial.json").read_text())
        self.engine.config["state_schema"] = schema
        self.engine.record["state"] = initial
        self.select()
        self.update({"claims": {"c1": {"statement": "candidate claim", "status": "unverified", "source": ""}}})
        original = compact(self.engine.record["state"])
        response = self.engine.handle_tool_call(UPDATE, {
            "revision": self.engine.record["revision"], "patch_json": '{"claims":{"c1":{"status":"proved_by_json"}}}'})
        self.assertFalse(json.loads(response)["saved"])
        self.assertEqual(compact(self.engine.record["state"]), original)

    def test_repeated_user_content_after_turn_boundary_requires_new_update(self):
        self.select()
        self.update({"objective": "repeatable task"})
        self.engine.on_turn_complete(self.messages)
        self.messages = self.messages[:2]
        self.select()
        self.assertFalse(self.engine.record["ready"])

    def test_multimodal_input_blocks_instead_of_becoming_unreadable_json(self):
        self.messages[1]["content"] = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]
        selected = compact(self.select())
        self.assertIn("text only", selected)
        self.assertNotIn("base64", selected)
        self.assertIsNotNone(before_tool("write_file", session_id="test-session"))


class ContextPolicyTests(unittest.TestCase):
    def test_utf8_budget_and_literal_search(self):
        with tempfile.TemporaryDirectory() as root:
            store = EvidenceStore(Path(root))
            text = "😀" * 1000 + "quote ' ; DROP TABLE evidence;"
            bounded = observation_for(text, limit=600, archive=store)
            self.assertLessEqual(byte_size(bounded), 600)
            self.assertEqual(len(store.search("' ; DROP TABLE")), 1)
            self.assertEqual(len(store.search("%")), 0)
            with self.assertRaises(ValueError):
                observation_for(text, limit=600)

    def test_recursive_null_deletion_even_under_new_object(self):
        self.assertEqual(merge_state({}, {"new": {"delete": None, "keep": 1}}), {"new": {"keep": 1}})
        self.assertEqual(runtime.merge_patch({}, {"new": {"delete": None}}), {"new": {}})

    def test_evidence_is_scoped_and_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            a, b = EvidenceStore(Path(root) / "a"), EvidenceStore(Path(root) / "b")
            key = a.put("secret")
            with self.assertRaises(ValueError): b.read(key)
            with self.assertRaises(ValueError): a.read(key, limit=2001)
            with self.assertRaises(ValueError): a.read("../../file")

    def test_full_prompt_budget_precedes_model_call(self):
        with patch.object(runtime.subprocess, "run") as call:
            with self.assertRaises(ValueError):
                runtime.invoke({"harness": "command", "max_prompt_bytes": 100}, "x" * 101)
            call.assert_not_called()

    def test_successful_observation_is_replayed_once_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "spec.md").write_text("Read and remember")
            schema = {"type": "object"}
            runtime.write_json(root / "schema.json", schema)
            runtime.write_json(root / "integrity.json", {n: runtime.sha256_file(root/n) for n in ("spec.md", "schema.json")})
            runtime.write_json(root / "state.json", {"revision": 0, "state": {}})
            runtime.write_json(root / "config.json", {"workspace": d, "allowed_capabilities": ["read_text"]})
            (root / "fact.txt").write_text("RECOVERY_SENTINEL")
            envelope = dict(state_patch_json="{}", action_argv=["read_text", "fact.txt"], action_cwd="", status="continue", message="")
            with patch.object(runtime, "propose", return_value=(envelope, {}, None)):
                runtime.transition(root, "read", True)
            done = dict(envelope, action_argv=[], status="done")
            prompts = []
            def propose(config, prompt, state, schema):
                prompts.append(prompt)
                return done, state, None
            with patch.object(runtime, "propose", side_effect=propose):
                runtime.transition(root, "restart", True)
                runtime.transition(root, "next", False)
            self.assertEqual(prompts[0].count("RECOVERY_SENTINEL"), 1)
            self.assertNotIn("RECOVERY_SENTINEL", prompts[1])


if __name__ == "__main__":
    unittest.main()

"""Small contract checks for standalone Hermes SKILL.state plugin."""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


try:
    import agent.context_engine  # noqa: F401
except ImportError:
    agent = types.ModuleType("agent")
    agent.__path__ = []
    context_engine = types.ModuleType("agent.context_engine")

    class ContextEngine:
        pass

    context_engine.ContextEngine = ContextEngine
    sys.modules["agent"] = agent
    sys.modules["agent.context_engine"] = context_engine

from hermes_state_engine import (
    MAX_ITEM_CHARS,
    MAX_LIST_ITEMS,
    SkillStateEngine,
    checkpoint_tool,
    state_prompt,
)


class HermesStateEngineTest(unittest.TestCase):
    def test_checkpoint_replaces_completed_turns_and_bounds_tool_loops(self):
        state = {
            "objective": "proof",
            "status": "active",
            "completed": ["kept"],
            "pending": ["next"],
            "facts": ["unique-state-fact"],
            "blockers": [],
            "next": "continue",
        }
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = home
            try:
                engine = SkillStateEngine()
                engine.on_session_start("session")
                first_turn = [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "old"},
                ]
                self.assertIsNone(engine.select_context(
                    first_turn,
                    conversation_messages=first_turn,
                    incoming_message=first_turn[-1],
                ))
                self.assertEqual(engine.handle_tool_call("skill_state_checkpoint", state), '{"saved":true}')
                engine.on_turn_complete(first_turn + [{"role": "assistant", "content": "done"}])
                second_turn = first_turn + [
                    {"role": "assistant", "content": "done"},
                    {"role": "user", "content": "fresh"},
                ]
                selected = engine.select_context(
                    second_turn,
                    conversation_messages=second_turn,
                    incoming_message=second_turn[-1],
                )
                tool_loop = second_turn + [
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
                    {"role": "tool", "tool_call_id": "call-1", "content": "tool result"},
                ]
                selected_tool_loop = engine.select_context(
                    tool_loop,
                    conversation_messages=tool_loop,
                    incoming_message=second_turn[-1],
                )
                synthetic_loop = tool_loop + [{
                    "role": "user",
                    "content": "verify before stopping",
                    "_verification_stop_synthetic": True,
                }]
                selected_synthetic_loop = engine.select_context(
                    synthetic_loop,
                    conversation_messages=synthetic_loop,
                    incoming_message=second_turn[-1],
                )
                retry_loop = synthetic_loop + [{
                    "role": "user",
                    "content": "continue after output limit",
                    "_length_continuation_nudge": True,
                }]
                selected_retry_loop = engine.select_context(
                    retry_loop,
                    conversation_messages=retry_loop,
                    incoming_message=second_turn[-1],
                )
            finally:
                if previous is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = previous
        self.assertEqual(selected[0]["content"], "system")
        self.assertIn("unique-state-fact", selected[-1]["content"])
        self.assertNotIn("old", selected[-1]["content"])
        self.assertEqual([message["role"] for message in selected_tool_loop], ["system", "user", "assistant", "tool"])
        self.assertIn("unique-state-fact", selected_tool_loop[1]["content"])
        self.assertNotIn("old", selected_tool_loop[1]["content"])
        self.assertEqual(selected_tool_loop[-1]["content"], "tool result")
        self.assertIsNotNone(selected_synthetic_loop)
        self.assertNotIn("old", selected_synthetic_loop[1]["content"])
        self.assertTrue(selected_synthetic_loop[-1]["_verification_stop_synthetic"])
        self.assertIsNotNone(selected_retry_loop)
        self.assertNotIn("old", selected_retry_loop[1]["content"])
        self.assertTrue(selected_retry_loop[-1]["_length_continuation_nudge"])

    def test_interrupted_or_failed_turn_does_not_promote_checkpoint(self):
        state = {
            "objective": "discard", "status": "active", "completed": [],
            "pending": [], "facts": ["must not survive"], "blockers": [], "next": "retry",
        }
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = home
            try:
                for flag in ("interrupted", "failed"):
                    with self.subTest(flag=flag):
                        session = f"{flag}-session"
                        engine = SkillStateEngine()
                        engine.on_session_start(session)
                        first = [{"role": "user", "content": "one"}]
                        engine.select_context(first, conversation_messages=first, incoming_message=first[-1])
                        self.assertEqual(checkpoint_tool(state, session), '{"saved":true}')
                        engine.on_turn_complete(
                            first + [{"role": "assistant", "content": "partial"}],
                            **{flag: True},
                        )
                        second = first + [
                            {"role": "assistant", "content": "partial"},
                            {"role": "user", "content": "two"},
                        ]
                        self.assertIsNone(engine.select_context(
                            second,
                            conversation_messages=second,
                            incoming_message=second[-1],
                        ))
            finally:
                if previous is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = previous

    def test_missed_checkpoint_fails_open_on_the_following_turn(self):
        state = {
            "objective": "freshness", "status": "active", "completed": [],
            "pending": [], "facts": ["revision one"], "blockers": [], "next": "continue",
        }
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = home
            try:
                engine = SkillStateEngine()
                engine.on_session_start("freshness-session")
                first = [{"role": "user", "content": "one"}]
                self.assertIsNone(engine.select_context(first, conversation_messages=first, incoming_message=first[-1]))
                self.assertEqual(checkpoint_tool(state, "freshness-session"), '{"saved":true}')
                engine.on_turn_complete(first + [{"role": "assistant", "content": "answer one"}])
                second = first + [
                    {"role": "assistant", "content": "answer one"},
                    {"role": "user", "content": "two"},
                ]
                self.assertIsNotNone(engine.select_context(second, conversation_messages=second, incoming_message=second[-1]))
                engine.on_turn_complete(second + [{"role": "assistant", "content": "answer two"}])
                third = second + [
                    {"role": "assistant", "content": "answer two"},
                    {"role": "user", "content": "three"},
                ]
                self.assertIsNone(engine.select_context(third, conversation_messages=third, incoming_message=third[-1]))
            finally:
                if previous is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = previous

    def test_internal_tool_persists_without_visible_marker_protocol(self):
        state = {
            "objective": "tool proof", "status": "active", "completed": [],
            "pending": ["continue"], "facts": ["hidden transport"],
            "blockers": [], "next": "reply",
        }
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = home
            try:
                engine = SkillStateEngine()
                engine.on_session_start("session")
                initial = [{"role": "user", "content": "old"}]
                self.assertIsNone(engine.select_context(initial, conversation_messages=initial, incoming_message=initial[-1]))
                self.assertEqual(checkpoint_tool(state, "session"), '{"saved":true}')
                engine.on_turn_complete(initial + [{"role": "assistant", "content": "done"}])
                request = initial + [
                    {"role": "assistant", "content": "done"},
                    {"role": "user", "content": "fresh"},
                ]
                selected = engine.select_context(
                    request,
                    conversation_messages=request,
                    incoming_message=request[-1],
                )
            finally:
                if previous is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = previous
        self.assertIn("hidden transport", selected[-1]["content"])
        self.assertNotIn("marker", state_prompt({}).lower())

    def test_invalid_checkpoint_supersedes_valid_checkpoint(self):
        state = {
            "objective": "old valid", "status": "active", "completed": [],
            "pending": [], "facts": ["OLD_VALID"], "blockers": [], "next": "continue",
        }
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = home
            try:
                engine = SkillStateEngine()
                engine.on_session_start("invalid-session")
                first = [{"role": "user", "content": "one"}]
                engine.select_context(first, conversation_messages=first, incoming_message=first[-1])
                self.assertEqual(checkpoint_tool(state, "invalid-session"), '{"saved":true}')
                self.assertIn("invalid SKILL.state", checkpoint_tool({"objective": "bad"}, "invalid-session"))
                engine.on_turn_complete(first + [{"role": "assistant", "content": "done"}])
                second = first + [
                    {"role": "assistant", "content": "done"},
                    {"role": "user", "content": "two"},
                ]
                selected = engine.select_context(second, conversation_messages=second, incoming_message=second[-1])
            finally:
                if previous is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = previous
        self.assertIsNone(selected)

    def test_checkpoint_fingerprint_rejects_rewritten_history(self):
        state = {
            "objective": "fingerprint", "status": "active", "completed": [],
            "pending": [], "facts": ["original history"], "blockers": [], "next": "continue",
        }
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = home
            try:
                engine = SkillStateEngine()
                engine.on_session_start("fingerprint-session")
                first = [{"role": "user", "content": "original"}]
                engine.select_context(first, conversation_messages=first, incoming_message=first[-1])
                self.assertEqual(checkpoint_tool(state, "fingerprint-session"), '{"saved":true}')
                engine.on_turn_complete(first + [{"role": "assistant", "content": "done"}])
                rewritten = [
                    {"role": "user", "content": "rewritten"},
                    {"role": "assistant", "content": "done"},
                    {"role": "user", "content": "two"},
                ]
                selected = engine.select_context(
                    rewritten,
                    conversation_messages=rewritten,
                    incoming_message=rewritten[-1],
                )
            finally:
                if previous is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = previous
        self.assertIsNone(selected)

    def test_multimodal_user_turn_is_compacted(self):
        state = {
            "objective": "multimodal", "status": "active", "completed": [],
            "pending": [], "facts": ["prior state"], "blockers": [], "next": "inspect image",
        }
        image_part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = home
            try:
                engine = SkillStateEngine()
                engine.on_session_start("multimodal-session")
                first = [{"role": "user", "content": "one"}]
                engine.select_context(first, conversation_messages=first, incoming_message=first[-1])
                self.assertEqual(checkpoint_tool(state, "multimodal-session"), '{"saved":true}')
                engine.on_turn_complete(first + [{"role": "assistant", "content": "done"}])
                second = first + [
                    {"role": "assistant", "content": "done"},
                    {"role": "user", "content": [{"type": "text", "text": "inspect\ud800"}, image_part]},
                ]
                selected = engine.select_context(second, conversation_messages=second, incoming_message=second[-1])
            finally:
                if previous is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = previous
        self.assertIsNotNone(selected)
        self.assertIn("prior state", selected[0]["content"][0]["text"])
        self.assertEqual(selected[0]["content"][-1], image_part)

    def test_checkpoint_bounds_are_enforced(self):
        state = {
            "objective": "bounded", "status": "active",
            "completed": ["item"] * (MAX_LIST_ITEMS + 1),
            "pending": [], "facts": [], "blockers": [], "next": "continue",
        }
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = home
            try:
                engine = SkillStateEngine()
                engine.on_session_start("bounded-session")
                request = [{"role": "user", "content": "start"}]
                engine.select_context(request, conversation_messages=request, incoming_message=request[-1])
                result = checkpoint_tool(state, "bounded-session")
                state["completed"] = ["x" * 200] * MAX_LIST_ITEMS
                state["pending"] = ["x" * 200] * MAX_LIST_ITEMS
                state["facts"] = ["x" * 200] * MAX_LIST_ITEMS
                state["blockers"] = ["x" * 200] * MAX_LIST_ITEMS
                oversized_result = checkpoint_tool(state, "bounded-session")
                state["completed"] = ["x" * (MAX_ITEM_CHARS + 1)]
                state["pending"] = state["facts"] = state["blockers"] = []
                oversized_item_result = checkpoint_tool(state, "bounded-session")
            finally:
                if previous is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = previous
        self.assertIn("invalid SKILL.state", result)
        self.assertIn("invalid SKILL.state", oversized_result)
        self.assertIn("invalid SKILL.state", oversized_item_result)

    def test_plugin_has_no_output_transform_hook(self):
        root = Path(__file__).parent
        self.assertNotIn("provides_hooks", (root / "plugin.yaml").read_text(encoding="utf-8"))
        entrypoint = (root / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("transform_llm_output", entrypoint)
        self.assertNotIn("register_tool", entrypoint)


if __name__ == "__main__":
    unittest.main()

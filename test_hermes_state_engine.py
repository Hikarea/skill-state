"""Small contract checks for standalone Hermes SKILL.state plugin."""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


if "agent.context_engine" not in sys.modules:
    agent = types.ModuleType("agent")
    context_engine = types.ModuleType("agent.context_engine")

    class ContextEngine:
        pass

    context_engine.ContextEngine = ContextEngine
    sys.modules["agent"] = agent
    sys.modules["agent.context_engine"] = context_engine

from hermes_state_engine import SkillStateEngine, checkpoint_tool, state_prompt


class HermesStateEngineTest(unittest.TestCase):
    def test_checkpoint_replaces_completed_turns(self):
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
                self.assertEqual(checkpoint_tool(state, "session"), '{"saved":true}')
                engine = SkillStateEngine()
                engine.on_session_start("session")
                selected = engine.select_context(
                    [{"role": "system", "content": "system"}, {"role": "user", "content": "old"}],
                    incoming_message={"role": "user", "content": "fresh"},
                )
            finally:
                if previous is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = previous
        self.assertEqual(selected[0]["content"], "system")
        self.assertIn("unique-state-fact", selected[-1]["content"])
        self.assertNotIn("old", selected[-1]["content"])
        self.assertIsNone(
            engine.select_context(
                [{"role": "user", "content": "work"}, {"role": "assistant", "content": "tool call"}],
                incoming_message={"role": "user", "content": "fresh"},
            )
        )

    def test_internal_tool_persists_without_marker_protocol(self):
        state = {
            "objective": "tool proof", "status": "active", "completed": [],
            "pending": ["continue"], "facts": ["hidden transport"],
            "blockers": [], "next": "reply",
        }
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = home
            try:
                self.assertEqual(checkpoint_tool(state, "session"), '{"saved":true}')
                engine = SkillStateEngine()
                engine.on_session_start("session")
                selected = engine.select_context(
                    [{"role": "user", "content": "old"}],
                    incoming_message={"role": "user", "content": "fresh"},
                )
            finally:
                if previous is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = previous
        self.assertIn("hidden transport", selected[-1]["content"])
        self.assertNotIn("marker", state_prompt({}).lower())

    def test_plugin_has_no_output_transform_hook(self):
        root = Path(__file__).parent
        self.assertNotIn("provides_hooks", (root / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("transform_llm_output", (root / "__init__.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

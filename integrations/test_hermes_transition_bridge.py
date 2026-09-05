"""Run: python -m unittest discover -s integrations -p test_hermes_transition_bridge.py"""
import logging
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

from hermes_transition_bridge import (
    FINALIZE_ANCHOR, FINALIZE_HOOK, TRANSITION_ANCHOR, TRANSITION_HOOK,
    install_bridge, transform_source,
)


FIXTURE = (
    "def run():\n    while True:\n        try:\n"
    "            assistant_message = normalized\n"
    + TRANSITION_ANCHOR
    + '            try:\n                if has_hook("post_api_request"):\n                    pass\n'
    + "            except Exception:\n                pass\n"
    + "        except Exception:\n            pass\n"
    + FINALIZE_ANCHOR
    + "    return None\n"
)


def execute_hook(engine, tools_for_api=None):
    # Execute the exact installed block in a bounded host loop. The append
    # represents both lifecycle observers and native action/final dispatch.
    body = textwrap.indent(textwrap.dedent(TRANSITION_HOOK), "        ")
    script = "dispatched = []\nfailed = False\nfor attempt in range(2):\n    if True:\n" + body
    script += "        dispatched.append(assistant_message)\n"
    message = SimpleNamespace(content="envelope", finish_reason="tool_calls")
    namespace = {
        "agent": SimpleNamespace(context_compressor=engine, tools=[
            {"type": "function", "function": {"name": "read_file"}},
        ]),
        "assistant_message": message, "logger": logging.getLogger("bridge-test"),
        "tools_for_api": tools_for_api,
    }
    exec(script, namespace)
    return namespace


class BridgeTests(unittest.TestCase):
    def test_install_idempotent_and_reversible_preserves_other_edits(self):
        installed = transform_source(FIXTURE)
        self.assertEqual(transform_source(installed), installed)
        changed = installed + "\n# unrelated user edit\n"
        self.assertEqual(transform_source(changed, uninstall=True), FIXTURE + "\n# unrelated user edit\n")

    def test_incompatible_or_partial_source_rejected(self):
        for source in (FIXTURE.replace(TRANSITION_ANCHOR, ""),
                       FIXTURE + TRANSITION_ANCHOR,
                       FIXTURE.replace(TRANSITION_ANCHOR, TRANSITION_HOOK + TRANSITION_ANCHOR),
                       FIXTURE.replace("assistant_message = normalized", "assistant_message = other")):
            with self.assertRaises(ValueError):
                transform_source(source)

    def test_file_install_uninstall_preserves_crlf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "agent" / "conversation_loop.py"
            target.parent.mkdir()
            original = FIXTURE.replace("\n", "\r\n").encode()
            target.write_bytes(original)
            install_bridge(root)
            install_bridge(root, uninstall=True)
            self.assertEqual(target.read_bytes(), original)

    def test_valid_message_reaches_native_dispatch_with_allowlist(self):
        class Engine:
            def prepare_transition(self, message, *, allowed_tools):
                self.allowed = allowed_tools
                return message
        engine = Engine()
        result = execute_hook(engine)
        self.assertEqual(engine.allowed, {"read_file"})
        self.assertEqual(len(result["dispatched"]), 2)

    def test_validation_retries_are_bounded_and_never_dispatch(self):
        class Engine:
            def prepare_transition(self, message, *, allowed_tools):
                raise ValueError("bad revision")
        result = execute_hook(Engine())
        self.assertEqual(result["dispatched"], [])
        self.assertEqual(result["attempt"], 1)

    def test_request_subset_cannot_dispatch_hidden_known_tool(self):
        class Engine:
            def prepare_transition(self, message, *, allowed_tools):
                if "read_file" not in allowed_tools:
                    raise ValueError("hidden tool")
                return message
        result = execute_hook(Engine(), tools_for_api=[
            {"type": "function", "function": {"name": "visible_tool"}},
        ])
        self.assertEqual(result["dispatched"], [])

    def test_explicit_empty_request_tools_deny_all(self):
        class Engine:
            def prepare_transition(self, message, *, allowed_tools):
                self.allowed = allowed_tools
                if "read_file" not in allowed_tools:
                    raise ValueError("no tools enabled")
                return message
        engine = Engine()
        result = execute_hook(engine, tools_for_api=[])
        self.assertEqual(engine.allowed, set())
        self.assertEqual(result["dispatched"], [])

    def test_none_request_tools_falls_back_to_agent_registry(self):
        class Engine:
            def prepare_transition(self, message, *, allowed_tools):
                self.allowed = allowed_tools
                return message
        engine = Engine()
        result = execute_hook(engine, tools_for_api=None)
        self.assertEqual(engine.allowed, {"read_file"})
        self.assertEqual(len(result["dispatched"]), 2)

    def test_unexpected_failure_stops_without_dispatch(self):
        class Engine:
            def prepare_transition(self, message, *, allowed_tools):
                raise RuntimeError("storage failure")
        with self.assertLogs("bridge-test", level="ERROR"):
            result = execute_hook(Engine())
        self.assertTrue(result["failed"])
        self.assertEqual(result["attempt"], 0)
        self.assertEqual(result["dispatched"], [])

    def test_budget_exhaustion_cannot_enter_summary_fallback(self):
        namespace = {"agent": SimpleNamespace(context_compressor=SimpleNamespace(
            prepare_transition=lambda message: message)), "final_response": None, "failed": False}
        exec(textwrap.dedent(FINALIZE_HOOK), namespace)
        self.assertTrue(namespace["failed"])
        self.assertIsNotNone(namespace["final_response"])

    def test_other_engines_unchanged(self):
        result = execute_hook(object())
        self.assertEqual(len(result["dispatched"]), 2)


if __name__ == "__main__":
    unittest.main()

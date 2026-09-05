"""Optional, reversible prepare_transition host hook for compatible Hermes loops.

The context engine owns validation and bounded retry feedback. Only Hermes'
native dispatcher executes the returned tool call, including its approval gates.
"""
from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path


TRANSITION_ANCHOR = "            # ── Agent-as-provider projection ──────────────────────────────\n"
FINALIZE_ANCHOR = "    # Post-loop turn finalization extracted to agent/turn_finalizer.finalize_turn\n"
TRANSITION_HOOK = '''            # Optional explicit context-engine transition (before observers/dispatch).
            _transition_engine = getattr(agent, "context_compressor", None)
            if callable(getattr(_transition_engine, "prepare_transition", None)):
                try:
                    assistant_message = _transition_engine.prepare_transition(
                        assistant_message,
                        allowed_tools={
                            tool["function"]["name"] for tool in (agent.tools or [])
                            if isinstance(tool, dict) and tool.get("type") == "function"
                        },
                    )
                    if assistant_message is None:
                        raise RuntimeError("prepare_transition returned no message")
                    finish_reason = assistant_message.finish_reason
                except ValueError:
                    # The engine retains only the current bounded validation error.
                    # This attempt still consumes the normal iteration budget.
                    continue
                except Exception:
                    logger.exception("Context engine transition failed; action blocked")
                    failed = True
                    final_response = "Incomplete: context engine transition failed."
                    _turn_exit_reason = "context_engine_transition_failed"
                    break

'''
FINALIZE_HOOK = '''    # Transition engines may finish only through their validated final envelope.
    if final_response is None and callable(getattr(
        getattr(agent, "context_compressor", None), "prepare_transition", None
    )):
        failed = True
        final_response = "Incomplete: no validated final transition before the turn ended."
        _turn_exit_reason = "context_engine_transition_incomplete"

'''
PATCHES = ((TRANSITION_ANCHOR, TRANSITION_HOOK), (FINALIZE_ANCHOR, FINALIZE_HOOK))


def transform_source(source: str, *, uninstall: bool = False) -> str:
    """Fail closed on missing/duplicate anchors or a partially installed bridge."""
    source = source.replace("\r\n", "\n")
    present = [source.count(hook + anchor) == 1 for anchor, hook in PATCHES]
    if any(present) and not all(present):
        raise ValueError("Partial/incompatible Hermes transition bridge; no files changed")
    for anchor, hook in PATCHES:
        if source.count(anchor) != 1 or source.count(hook) > 1:
            raise ValueError("Incompatible Hermes conversation loop; no files changed")
    if all(present):
        if uninstall:
            for anchor, hook in PATCHES:
                source = source.replace(hook + anchor, anchor, 1)
    elif not uninstall:
        # Require the expected normalized-message flow, not just a comment match.
        normalize = "            assistant_message = normalized\n"
        dispatch = '                if has_hook("post_api_request"):\n'
        if source.count(normalize) != 1 or source.count(dispatch) != 1:
            raise ValueError("Incompatible Hermes response flow; no files changed")
        if not source.index(normalize) < source.index(TRANSITION_ANCHOR) < source.index(dispatch) < source.index(FINALIZE_ANCHOR):
            raise ValueError("Incompatible Hermes response order; no files changed")
        for anchor, hook in PATCHES:
            source = source.replace(anchor, hook + anchor, 1)
    ast.parse(source)
    return source


def install_bridge(hermes_root: Path, *, uninstall: bool = False) -> Path:
    target = Path(hermes_root) / "agent" / "conversation_loop.py"
    original = target.read_bytes()
    source = original.decode("utf-8")
    updated = transform_source(source, uninstall=uninstall)
    newline = "\r\n" if b"\r\n" in original else "\n"
    replacement = updated.replace("\n", newline).encode("utf-8")
    if original != replacement:
        temporary = target.with_suffix(".skill-state.tmp")
        created = False
        try:
            with temporary.open("xb") as stream:
                created = True
                stream.write(replacement)
            if target.read_bytes() != original:
                raise RuntimeError("Hermes changed during bridge installation; retry")
            os.replace(temporary, target)
        finally:
            if created:
                temporary.unlink(missing_ok=True)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hermes_root", type=Path)
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    print(install_bridge(args.hermes_root, uninstall=args.uninstall))

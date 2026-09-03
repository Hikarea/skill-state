"""Standalone Hermes context plugin for bounded SKILL.state checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from agent.context_engine import ContextEngine


_STATUSES = {"active", "done", "blocked"}
_LOCK = threading.Lock()


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home()
    except ImportError:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _path(session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return _home() / "skill-state" / "sessions" / f"{digest}.json"


def _valid(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "objective", "status", "completed", "pending", "facts", "blockers", "next"
    }:
        return False
    if not isinstance(value["objective"], str) or not isinstance(value["next"], str):
        return False
    if value["status"] not in _STATUSES:
        return False
    return all(isinstance(value[key], list) and all(isinstance(item, str) for item in value[key])
               for key in ("completed", "pending", "facts", "blockers"))


def _read(session_id: str) -> dict[str, Any] | None:
    try:
        value = json.loads(_path(session_id).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if _valid(value) else None


def _write(session_id: str, state: dict[str, Any]) -> None:
    destination = _path(session_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    temporary = destination.with_suffix(".tmp")
    with _LOCK:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, destination)


def checkpoint_tool(args: dict[str, Any], session_id: str = "", **_: Any) -> str:
    """Persist a model-created state without exposing it in streamed text."""
    if not session_id or not _valid(args):
        return json.dumps({"saved": False, "error": "invalid SKILL.state"})
    _write(session_id, args)
    return '{"saved":true}'


def state_prompt(_: dict[str, Any]) -> str:
    return """SKILL.state protocol: Before each final user-facing answer, call the internal skill_state_checkpoint tool once with a compact, complete JSON state: objective, status (active|done|blocked), completed, pending, facts, blockers, next. Do not put state or protocol text in the user-facing answer."""


class SkillStateEngine(ContextEngine):
    """Replace prior-turn transcript with the current validated checkpoint."""

    emit_automatic_compaction_status = False

    def __init__(self) -> None:
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.threshold_tokens = 0
        self.context_length = 0
        self.compression_count = 0
        self._session_id: str | None = None

    @property
    def name(self) -> str:
        return "skill-state"

    def __deepcopy__(self, memo: dict[int, Any]) -> "SkillStateEngine":
        copy = type(self)()
        copy.context_length = self.context_length
        return copy

    def on_session_start(self, session_id: str, **_: Any) -> None:
        self._session_id = session_id

    def update_from_response(self, usage: dict[str, Any]) -> None:
        self.last_prompt_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
        self.last_completion_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
        self.last_total_tokens = int(usage.get("total_tokens", self.last_prompt_tokens + self.last_completion_tokens) or 0)

    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        return False

    def compress(self, messages: list[dict[str, Any]], **_: Any) -> list[dict[str, Any]]:
        return messages

    def select_context(
        self,
        request_messages: list[dict[str, Any]],
        *,
        incoming_message: dict[str, Any] | None = None,
        **_: Any,
    ) -> list[dict[str, Any]] | None:
        if not self._session_id or not isinstance(incoming_message, dict):
            return None
        non_system = [message for message in request_messages if message.get("role") not in {"system", "developer"}]
        if not non_system or non_system[-1].get("role") != "user":
            return None  # Preserve the current tool loop exactly.
        state = _read(self._session_id)
        if state is None:
            return None
        stable = [message for message in request_messages if message.get("role") in {"system", "developer"}]
        content = incoming_message.get("content", "")
        if not isinstance(content, str):
            return None
        return stable + [{
            "role": "user",
            "content": "Canonical state from prior turns:\n" + json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            + "\n\nLatest user observation:\n" + content,
        }]


ENGINE = SkillStateEngine()

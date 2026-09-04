"""Standalone Hermes context plugin for bounded SKILL.state checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from agent.context_compressor import ContextCompressor


_STATUSES = {"active", "done", "blocked"}
MAX_STATE_BYTES = 16_384
MAX_LIST_ITEMS = 32
MAX_TEXT_CHARS = 2_000
MAX_ITEM_CHARS = 1_000
_LOCK = threading.RLock()
_ACTIVE_TURNS: dict[str, str] = {}
CHECKPOINT_SCHEMA = {
    "name": "skill_state_checkpoint",
    "description": "Save the compact canonical SKILL.state for continuation. Call internally before the final user-facing reply.",
    "parameters": {
        "type": "object",
        "properties": {
            "objective": {"type": "string", "maxLength": MAX_TEXT_CHARS},
            "status": {"type": "string", "enum": ["active", "done", "blocked"]},
            "completed": {"type": "array", "maxItems": MAX_LIST_ITEMS, "items": {"type": "string", "maxLength": MAX_ITEM_CHARS}},
            "pending": {"type": "array", "maxItems": MAX_LIST_ITEMS, "items": {"type": "string", "maxLength": MAX_ITEM_CHARS}},
            "facts": {"type": "array", "maxItems": MAX_LIST_ITEMS, "items": {"type": "string", "maxLength": MAX_ITEM_CHARS}},
            "blockers": {"type": "array", "maxItems": MAX_LIST_ITEMS, "items": {"type": "string", "maxLength": MAX_ITEM_CHARS}},
            "next": {"type": "string", "maxLength": MAX_TEXT_CHARS},
        },
        "required": ["objective", "status", "completed", "pending", "facts", "blockers", "next"],
        "additionalProperties": False,
    },
}


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
    if not all(isinstance(value[key], str) and len(value[key]) <= MAX_TEXT_CHARS
               for key in ("objective", "next")):
        return False
    if value["status"] not in _STATUSES:
        return False
    if not all(
        isinstance(value[key], list)
        and len(value[key]) <= MAX_LIST_ITEMS
        and all(isinstance(item, str) and len(item) <= MAX_ITEM_CHARS for item in value[key])
        for key in ("completed", "pending", "facts", "blockers")
    ):
        return False
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= MAX_STATE_BYTES
    except UnicodeEncodeError:
        return False


def _read_record(session_id: str) -> dict[str, Any] | None:
    try:
        value = json.loads(_path(session_id).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if _valid(value):  # Safe migration: legacy checkpoints are retained but not fresh.
        return {
            "version": 1, "state": value, "pending_turn": None,
            "fresh_turn": None, "completed_user_count": None,
        }
    if not isinstance(value, dict) or value.get("version") != 1 or not _valid(value.get("state")):
        return None
    if value.get("pending_turn") is not None and not isinstance(value["pending_turn"], str):
        return None
    if value.get("fresh_turn") is not None and not isinstance(value["fresh_turn"], str):
        return None
    count = value.get("completed_user_count")
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 1):
        return None
    return value


def _write_record(session_id: str, record: dict[str, Any]) -> None:
    destination = _path(session_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    temporary = destination.with_suffix(".tmp")
    with _LOCK:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, destination)


def _synthetic_user(message: dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    if any(key.endswith("_synthetic") and value is True for key, value in message.items()):
        return True
    if message.get("_length_continuation_nudge") is True or message.get("_dropped_toolcall_nudge") is True:
        return True
    try:
        from agent.context_compressor import ContextCompressor
        return ContextCompressor._is_synthetic_compression_user_turn(message)
    except (ImportError, AttributeError):
        return False


def _turn_marker(messages: list[dict[str, Any]] | None) -> tuple[str, int] | None:
    if not isinstance(messages, list):
        return None
    user_count = 0
    content: Any = None
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user" and not _synthetic_user(message):
            user_count += 1
            content = message.get("content")
    if not user_count:
        return None
    try:
        payload = json.dumps([user_count, content], ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest(), user_count


def checkpoint_tool(args: dict[str, Any], session_id: str = "", **_: Any) -> str:
    """Persist a model-created state without exposing it in streamed text."""
    if not session_id:
        return json.dumps({"saved": False, "error": "invalid SKILL.state"})
    if not _valid(args):
        with _LOCK:
            turn = _ACTIVE_TURNS.get(session_id)
            record = _read_record(session_id)
            if turn is not None and record is not None and record.get("pending_turn") == turn:
                record.update(pending_turn=None, fresh_turn=None, completed_user_count=None)
                _write_record(session_id, record)
        return json.dumps({"saved": False, "error": "invalid SKILL.state"})
    with _LOCK:
        turn = _ACTIVE_TURNS.get(session_id)
        if turn is None:
            return json.dumps({"saved": False, "error": "checkpoint outside an active turn"})
        _write_record(session_id, {
            "version": 1,
            "state": args,
            "pending_turn": turn,
            "fresh_turn": None,
            "completed_user_count": None,
        })
    return '{"saved":true}'


def state_prompt(_: dict[str, Any]) -> str:
    return """SKILL.state protocol: Before each final user-facing answer, call the internal skill_state_checkpoint tool once with a compact, complete JSON state: objective, status (active|done|blocked), completed, pending, facts, blockers, next. A missed or invalid checkpoint disables compaction for the following turn. Do not put state or protocol text in the user-facing answer."""


class SkillStateEngine(ContextCompressor):
    """Layer checkpoint selection over Hermes' native compressor."""

    emit_automatic_compaction_status = False

    def __init__(self) -> None:
        super().__init__(model="", quiet_mode=True)
        self._session_id: str | None = None
        self._active_turn: str | None = None
        self._active_state: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return "skill-state"

    def __deepcopy__(self, memo: dict[int, Any]) -> "SkillStateEngine":
        return type(self)()

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        super().on_session_start(session_id, **kwargs)
        self._session_id = session_id
        self._active_turn = None
        self._active_state = None
        with _LOCK:
            _ACTIVE_TURNS.pop(session_id, None)

    def on_session_reset(self) -> None:
        session_id = self._session_id
        super().on_session_reset()
        if session_id:
            with _LOCK:
                _ACTIVE_TURNS.pop(session_id, None)
        self._session_id = None
        self._active_turn = None
        self._active_state = None

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [CHECKPOINT_SCHEMA]

    def handle_tool_call(self, name: str, args: dict[str, Any], **_: Any) -> str:
        if name != CHECKPOINT_SCHEMA["name"]:
            return json.dumps({"saved": False, "error": f"unknown context engine tool: {name}"})
        return checkpoint_tool(args, self._session_id or "")

    def select_context(
        self,
        request_messages: list[dict[str, Any]],
        *,
        conversation_messages: list[dict[str, Any]] | None = None,
        incoming_message: dict[str, Any] | None = None,
        **_: Any,
    ) -> list[dict[str, Any]] | None:
        if not self._session_id or not isinstance(incoming_message, dict):
            return None
        source_messages = conversation_messages or request_messages
        marker = _turn_marker(source_messages)
        if marker is None:
            return None
        turn, user_count = marker
        real_user_indexes = [
            index for index, message in enumerate(source_messages)
            if isinstance(message, dict) and message.get("role") == "user" and not _synthetic_user(message)
        ]
        previous_marker = (
            _turn_marker(source_messages[:real_user_indexes[-1]]) if len(real_user_indexes) > 1 else None
        )
        with _LOCK:
            _ACTIVE_TURNS[self._session_id] = turn

        if self._active_turn != turn:
            self._active_turn = turn
            self._active_state = None
            record = _read_record(self._session_id)
            if (
                record is not None
                and previous_marker is not None
                and record.get("fresh_turn") == previous_marker[0]
                and record.get("completed_user_count") == user_count - 1
            ):
                self._active_state = record["state"]
                record["fresh_turn"] = None
                record["completed_user_count"] = None
                _write_record(self._session_id, record)
        if self._active_state is None:
            return None

        current_index = next(
            (index for index in range(len(request_messages) - 1, -1, -1)
             if request_messages[index].get("role") == "user"
             and not _synthetic_user(request_messages[index])),
            None,
        )
        if current_index is None:
            return None
        content = request_messages[current_index].get("content", "")
        state_text = "Canonical state from prior turns:\n" + json.dumps(
            self._active_state, ensure_ascii=False, separators=(",", ":")
        )
        if isinstance(content, str):
            selected_content: Any = state_text + "\n\nLatest user observation:\n" + content
        elif isinstance(content, list):
            selected_content = [
                {"type": "text", "text": state_text + "\n\nLatest user observation follows."},
                *content,
            ]
        else:
            return None
        stable = [message for message in request_messages[:current_index]
                  if message.get("role") in {"system", "developer"}]
        current_turn = request_messages[current_index + 1:]
        return stable + [{
            "role": "user",
            "content": selected_content,
        }] + current_turn

    def on_turn_complete(
        self,
        messages: list[dict[str, Any]],
        usage: dict[str, Any] | None = None,
        **meta: Any,
    ) -> None:
        del usage
        if not self._session_id:
            return
        marker = _turn_marker(messages)
        with _LOCK:
            record = _read_record(self._session_id)
            if record is not None:
                successful = not meta.get("interrupted", False) and not meta.get("failed", False)
                if marker is not None and successful and record.get("pending_turn") == marker[0]:
                    record["pending_turn"] = None
                    record["fresh_turn"] = marker[0]
                    record["completed_user_count"] = marker[1]
                else:
                    record["pending_turn"] = None
                    record["fresh_turn"] = None
                    record["completed_user_count"] = None
                _write_record(self._session_id, record)
            _ACTIVE_TURNS.pop(self._session_id, None)
        self._active_turn = None
        self._active_state = None


ENGINE = SkillStateEngine()

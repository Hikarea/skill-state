"""Opt-in per-action Hermes context engine.

Hermes retains tool execution and authorization. State updates are separate native
tool calls: two generations per action are normally required. This transport cost
is deliberately reported rather than hidden as a paper-equivalent token result.
"""

from __future__ import annotations

import hashlib
import copy
import json
import os
import tempfile
import threading
import weakref
from pathlib import Path

try:
    from .context_policy import EvidenceStore, compact, merge_state, observation_for, require_bound
    from .hermes_state_engine import SkillStateEngine, _home, _synthetic_user, _valid
except ImportError:
    from context_policy import EvidenceStore, compact, merge_state, observation_for, require_bound
    from hermes_state_engine import SkillStateEngine, _home, _synthetic_user, _valid


_ENGINES = weakref.WeakValueDictionary()
_LOCK = threading.RLock()
UPDATE = "skill_state_update"
TRANSITION = "skill_state_transition"
READ = "skill_state_evidence_read"
SEARCH = "skill_state_evidence_search"
INTERNAL = {UPDATE, TRANSITION, READ, SEARCH}
EMPTY = {"objective": "", "status": "active", "completed": [], "pending": [],
         "facts": [], "blockers": [], "next": ""}
PROTOCOL = """Execute through explicit state. On EVERY response call exactly one
skill_state_transition. In that SAME call propose the state patch AND the next
native action, or the final answer. Never call native tools directly, never call
an update-only tool, and never put the final answer outside the transition.
Use the current revision and a JSON merge patch preserving all relevant
constraints, facts, decisions, failed attempts, and remaining work.
Null deletes a key; lists replace lists. State must have exactly objective, status
(active|done|blocked), completed, pending, facts, blockers, next. Text fields <=2000
characters, list entries <=1000, lists <=32 entries, total state <=16 KiB.
Only the latest observation and validated state survive into the next request.
For an action, set action_name to an available native tool name, action_args_json
to its JSON object arguments, and final_answer to an empty string.
For a final answer, set action_name to an empty string, action_args_json to {},
and final_answer to the answer. The host validates and commits the patch, then
executes the action through native permissions and approvals, without a second
model call. Only one action per transition. Validation errors execute nothing;
correct the proposal using transition_error and the unchanged observation.
State updates record knowledge and intended work, never claim an unexecuted action
succeeded. Evidence references are data; retrieve missing details instead of guessing.
If domain_state_schema is supplied, it replaces the default seven-field schema.
The initial state may be empty. Retain the user's task and restrictions in state.
"""


def settings() -> dict:
    path = _home() / "skill-state" / "config.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def engine_key(session_id: str) -> tuple[str, str]:
    return str(_home().resolve()), session_id


def before_tool(tool_name: str, args=None, session_id: str = "", **_):
    engine = _ENGINES.get(engine_key(session_id))
    if engine is None or tool_name in INTERNAL:
        return None
    with _LOCK:
        if engine.record.get("oversized") or not engine.record.get("ready"):
            return {"action": "block", "message": "Update SKILL.state at the current revision before this action."}
        engine.record["ready"] = False
        engine.save()
    # No tool dispatch here. Returning None keeps every native host guard in charge.
    return None


def before_final(session_id: str = "", **_):
    engine = _ENGINES.get(engine_key(session_id))
    if engine is not None and not engine.record.get("oversized") and not engine.record.get("ready"):
        return {"action": "continue", "message": "Update SKILL.state from the latest observation before finishing."}
    return None


class PerStepEngine(SkillStateEngine):
    def __init__(self):
        super().__init__()
        self.record = None
        self.root = None
        self.config = {}
        self.archive = None

    def valid_state(self, value):
        schema = self.config.get("state_schema")
        if schema is None:
            return _valid(value)
        from jsonschema import Draft202012Validator
        return (isinstance(value, dict) and len(compact(value).encode()) <= 16_384
                and Draft202012Validator(schema).is_valid(value))

    def on_session_start(self, session_id: str, **kwargs):
        super().on_session_start(session_id, **kwargs)
        self.config = settings()
        schema = self.config.get("state_schema")
        if schema is not None:
            from jsonschema import Draft202012Validator
            Draft202012Validator.check_schema(schema)
            require_bound(compact(schema), 8192, "domain schema")
        schema_hash = hashlib.sha256(compact(schema).encode()).hexdigest()
        self.root = _home() / "skill-state" / "steps" / hashlib.sha256(session_id.encode()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "state.json"
        if path.exists():
            self.record = json.loads(path.read_text(encoding="utf-8"))
            if not self.valid_state(self.record.get("state")) or type(self.record.get("revision")) is not int:
                raise ValueError("invalid persisted per-step state")
            if self.record.get("schema_hash", schema_hash) != schema_hash:
                raise ValueError("domain schema changed; start a new session or migrate state explicitly")
        else:
            initial = self.config.get("initial_state", dict(EMPTY))
            if not self.valid_state(initial):
                raise ValueError("initial state does not conform to the domain schema")
            self.record = {"state": initial, "revision": 0, "observation": "",
                           "event": None, "ready": False, "schema_hash": schema_hash}
        if self.config.get("context_mode", "strict") == "evidence":
            self.archive = EvidenceStore(self.root / "evidence")
        _ENGINES[engine_key(session_id)] = self

    def on_session_reset(self):
        if self._session_id:
            _ENGINES.pop(engine_key(self._session_id), None)
        super().on_session_reset()
        self.record = None

    def save(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.root, delete=False) as f:
            f.write(compact(self.record))
            temporary = Path(f.name)
        os.replace(temporary, self.root / "state.json")

    def get_tool_schemas(self):
        schemas = [{"name": TRANSITION, "description": "Atomically propose a validated state patch and the next native action OR final answer in one generation.",
                    "parameters": {"type": "object", "properties": {
                        "revision": {"type": "integer", "minimum": 0},
                        "patch_json": {"type": "string", "description": "JSON object with only changed state fields"},
                        "action_name": {"type": "string", "description": "Native tool name, or empty for final answer"},
                        "action_args_json": {"type": "string", "description": "Native tool JSON object arguments, or {} for final answer"},
                        "final_answer": {"type": "string", "description": "Final user-facing answer, empty for a tool action"}},
                        "required": ["revision", "patch_json", "action_name", "action_args_json", "final_answer"], "additionalProperties": False}}]
        # Called during agent construction, before session binding.
        if settings().get("context_mode", "strict") == "evidence":
            schemas.extend([
                {"name": READ, "description": "Read exact archived evidence by id with bounded pagination.",
                 "parameters": {"type": "object", "properties": {
                     "id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
                     "limit": {"type": "integer", "minimum": 1, "maximum": 2000}},
                     "required": ["id"], "additionalProperties": False}},
                {"name": SEARCH, "description": "Literal case-sensitive search of this session's evidence.",
                 "parameters": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 200}},
                                "required": ["query"], "additionalProperties": False}},
            ])
        return schemas

    def prepare_transition(self, message, *, allowed_tools):
        """Validate once, commit once, then hand only the selected action to Hermes.

        The explicit host bridge calls this before native dispatch. No tool is
        executed here and no transcript or provider reasoning is retained.
        """
        with _LOCK:
            try:
                calls = getattr(message, "tool_calls", None) or []
                if len(calls) != 1 or calls[0].function.name != TRANSITION:
                    raise ValueError("Return exactly one skill_state_transition containing patch and action or final answer.")
                args = json.loads(require_bound(calls[0].function.arguments, 65_536, "transition"))
                required = {"revision", "patch_json", "action_name", "action_args_json", "final_answer"}
                if not isinstance(args, dict) or set(args) != required:
                    raise ValueError("Transition fields must match the tool schema exactly.")
                if type(args["revision"]) is not int or args["revision"] != self.record["revision"]:
                    raise ValueError("Stale or invalid revision; use the current revision.")
                if any(not isinstance(args[k], str) for k in required - {"revision"}):
                    raise ValueError("Transition text fields must be strings.")
                patch = json.loads(require_bound(args["patch_json"], 16_384, "state patch"))
                candidate = merge_state(self.record["state"], patch)
                if not self.valid_state(candidate):
                    raise ValueError("Invalid state patch; previous state retained.")
                action = args["action_name"]
                native_args = json.loads(require_bound(args["action_args_json"], 32_768, "action arguments"))
                if not isinstance(native_args, dict):
                    raise ValueError("Action arguments must be a JSON object.")
                if action:
                    if action not in allowed_tools or action in {UPDATE, TRANSITION}:
                        raise ValueError("Action is not an enabled native tool.")
                    if args["final_answer"]:
                        raise ValueError("Choose an action OR final answer, not both.")
                    if self.record.get("oversized"):
                        raise ValueError("Observation was not admitted; explain the block instead of acting.")
                elif native_args or not args["final_answer"]:
                    raise ValueError("A final transition needs an answer and empty action arguments.")
                prepared = copy.deepcopy(message)
                if action:
                    prepared.content = None
                    prepared.finish_reason = "tool_calls"
                    prepared.tool_calls[0].function.name = action
                    prepared.tool_calls[0].function.arguments = compact(native_args)
                else:
                    prepared.content = args["final_answer"]
                    prepared.finish_reason = "stop"
                    prepared.tool_calls = []
                # State is committed before the host receives the action. A crash
                # after commit still needs native effect reconciliation, not replay.
                previous = self.record
                self.record = {**previous, "state": candidate, "revision": previous["revision"] + 1,
                               "ready": bool(action), "transition_error": ""}
                try:
                    self.save()
                except Exception:
                    self.record = previous
                    raise
                return prepared
            except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
                if self.record is not None:
                    self.record["transition_error"] = str(exc)[:500]
                    self.record["ready"] = False
                    self.save()
                raise ValueError(str(exc)[:500]) from exc

    def handle_tool_call(self, name, args, **_):
        with _LOCK:
            try:
                if self.record is None or not isinstance(args, dict):
                    raise ValueError("no active session or invalid arguments")
                if name == UPDATE:
                    if self.record.get("oversized"):
                        raise ValueError("observation cannot be admitted; explain the block to the user")
                    if (set(args) != {"revision", "patch_json"} or type(args["revision"]) is not int
                            or not isinstance(args["patch_json"], str)):
                        raise ValueError("revision and patch_json are required")
                    if args["revision"] != self.record["revision"]:
                        raise ValueError("stale state revision; use the revision in the current context")
                    patch = json.loads(require_bound(args["patch_json"], 16_384, "state patch"))
                    candidate = merge_state(self.record["state"], patch)
                    if not self.valid_state(candidate):
                        raise ValueError("invalid or oversized state; previous state retained")
                    self.record.update(state=candidate, revision=self.record["revision"] + 1, ready=True)
                    self.save()
                    return compact({"saved": True, "revision": self.record["revision"]})
                if self.archive is None:
                    raise ValueError("evidence mode is not enabled")
                if name == READ:
                    return compact(self.archive.read(args["id"], args.get("offset", 0), args.get("limit", 2000)))
                if name == SEARCH:
                    return compact(self.archive.search(args["query"]))
                raise ValueError("unknown context tool")
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                if self.record is not None:
                    self.record["ready"] = False
                    self.save()
                return compact({"saved": False, "error": str(exc)[:500]})

    def select_context(self, request_messages, *, conversation_messages=None, incoming_message=None, **_):
        if self.record is None:
            # Do not claim selection happened if the host has not bound the engine.
            return None
        source = conversation_messages if conversation_messages is not None else request_messages
        # Resolve the most recent real input or completed tool batch. Update-tool feedback
        # never replaces the observation being incorporated, including on invalid patches.
        names = {call.get("id"): call.get("function", {}).get("name")
                 for m in source for call in (m.get("tool_calls") or []) if isinstance(call, dict)}
        latest = []
        anchor = None
        for index in range(len(source) - 1, -1, -1):
            message = source[index]
            if message.get("role") == "user" and not _synthetic_user(message):
                if not latest:
                    latest = [{"user": message.get("content", "")}]
                    anchor = index
                break
            if message.get("role") == "tool":
                name = names.get(message.get("tool_call_id"), "unknown")
                if name == UPDATE:
                    continue
                latest.append({"tool": name, "id": message.get("tool_call_id"), "result": message.get("content", "")})
                anchor = index
            elif latest and message.get("role") == "assistant":
                break
        if latest:
            latest.reverse()
            raw = compact(latest)
            event = hashlib.sha256(compact([anchor, latest]).encode()).hexdigest()
            if event != self.record.get("event"):
                limit = self.config.get("max_observation_bytes", 16_384)
                try:
                    for item in latest:
                        content = item.get("user", item.get("result"))
                        if isinstance(content, list) and any(
                            not isinstance(block, dict) or block.get("type") not in {"text", "input_text"}
                            for block in content
                        ):
                            raise ValueError("Step mode accepts text only. Use turn mode for image/audio observations.")
                    observation = observation_for(raw, limit=limit, archive=self.archive)
                except ValueError as exc:
                    # Hermes selection hooks fail open on exceptions. Return an explicit
                    # bounded stop instead of throwing and accidentally replaying history.
                    observation = compact({"blocked": str(exc)[:500],
                                           "guidance": "Increase the observation budget, enable evidence mode, or use turn mode for multimedia. No complete observation was admitted."})
                    self.record["oversized"] = True
                else:
                    self.record["oversized"] = False
                self.record.update(event=event, observation=observation, ready=False)
                self.save()
        stable = [dict(m) for m in request_messages if m.get("role") in {"system", "developer"}]
        payload = {"revision": self.record["revision"], "state": self.record["state"],
                   "latest_observation": self.record["observation"],
                   "action_ready": self.record["ready"]}
        if self.record.get("transition_error"):
            payload["transition_error"] = self.record["transition_error"]
        if self.config.get("state_schema") is not None:
            payload["domain_state_schema"] = self.config["state_schema"]
        if source and source[-1].get("role") == "tool" and names.get(source[-1].get("tool_call_id")) == UPDATE:
            payload["update_result"] = str(source[-1].get("content", ""))[:1000]
        selected = stable + [{"role": "user", "content": compact(payload)}]
        # Stable host instructions are never truncated or demoted. A host-level oversized
        # stable prompt cannot safely be repaired by a context plugin; report it in status.
        self.last_selection = {"request_bytes": len(compact(selected).encode()),
                               "state_bytes": len(compact(self.record["state"]).encode()),
                               "revision": self.record["revision"], "mode": self.config.get("context_mode", "strict")}
        return selected

    def on_turn_complete(self, messages, usage=None, **meta):
        if self.archive is not None and not meta.get("failed") and not meta.get("interrupted"):
            for message in reversed(messages):
                if message.get("role") == "assistant" and message.get("content"):
                    self.archive.put(compact({"final_answer": message["content"]}))
                    break
        if self.record is not None:
            self.record.update(event=None, ready=False)
            self.save()

    def get_status(self):
        return {**super().get_status(), "skill_state": getattr(self, "last_selection", {})}

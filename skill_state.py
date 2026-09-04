#!/usr/bin/env python3
"""Tiny SKILL.state runtime for fresh-context agent harness calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

HOME = Path.home() / ".skillstate"
CAPABILITIES = {"list_files", "read_text", "write_text", "mkdir"}
MAX_ACTION_BYTES = 100_000
MAX_STATE_BYTES = 16_384
ENVELOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "state_patch_json": {"type": "string"},
        "action_argv": {"type": "array", "items": {"type": "string"}},
        "action_cwd": {"type": "string"},
        "status": {"type": "string", "enum": ["continue", "done", "blocked"]},
        "message": {"type": "string"},
    },
    "required": ["state_patch_json", "action_argv", "action_cwd", "status", "message"],
    "additionalProperties": False,
}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        tmp = Path(f.name)
    os.replace(tmp, path)


def merge_patch(base, patch):
    if not isinstance(patch, dict):
        raise ValueError("state patch must be an object")
    out = dict(base)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_patch(out[key], value)
        else:
            out[key] = value
    return out


def validate(value, schema, path="state") -> None:
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda error: list(error.path))
    if errors:
        location = ".".join(str(part) for part in errors[0].path)
        raise ValueError(f"{path}{'.' + location if location else ''}: {errors[0].message}")


def validate_state(value, schema, path="state") -> None:
    validate(value, schema, path)
    try:
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{path}: invalid Unicode") from exc
    if size > MAX_STATE_BYTES:
        raise ValueError(f"{path}: exceeds {MAX_STATE_BYTES} bytes")


@contextmanager
def run_lock(run_dir: Path):
    lock = run_dir / ".lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch(exist_ok=True)
    file = lock.open("r+b")
    try:
        if not lock.stat().st_size:
            file.write(b"\0")
            file.flush()
        file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        file.close()
        raise RuntimeError(f"run locked: {lock}") from exc
    try:
        file.seek(0)
        file.truncate()
        file.write(str(os.getpid()).encode())
        file.flush()
        yield
    finally:
        file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        file.close()


def prompt_for(spec: str, schema: dict, state: dict, observation: str, capabilities: list[str] | None = None) -> str:
    available = ", ".join(capabilities or []) or "none"
    return f"""You are one transition in a SKILL.state runtime.

Immutable procedure:
{spec}

State schema:
{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}

Current execution state:
{json.dumps(state, ensure_ascii=False, separators=(',', ':'))}

Latest observation:
{observation}

Use no prior conversation. Propose one bounded next action only. Do not execute it.
Return exactly one JSON object with all five keys: state_patch_json, action_argv, action_cwd, status, message.
Return state_patch_json as a JSON object encoded in a string; null deletes a key.
Patches merge recursively. Preserve every schema-required field; use its empty value rather than null when completed.
Return action_argv as [capability, ...arguments], or [] when no action is needed. Available capabilities: {available}.
Return action_cwd as a workspace-relative directory, or "" for workspace root.
Set status to exactly continue, done, or blocked. Never use running. Set done only when procedure is complete, blocked only when user input is required.
Always include message as a concise user-facing string.
"""


def harness_command(config: dict, schema_path: Path, output_path: Path, prompt: str = "", usage_path: Path | None = None) -> list[str]:
    harness = config["harness"]
    if harness == "codex":
        return ["codex", "exec", "--ephemeral", "--disable", "hooks", "--skip-git-repo-check", "--sandbox", "read-only", "--output-schema", str(schema_path), "-o", str(output_path), "-"]
    if harness == "claude":
        return ["claude", "-p", "--no-session-persistence", "--tools", "", "--json-schema", json.dumps(ENVELOPE_SCHEMA, separators=(",", ":")), "--output-format", "json"]
    if harness == "hermes":
        command = ["hermes", "--ignore-rules", "--reasoning", "minimal", "--toolsets", "state-only"]
        if usage_path is not None:
            command += ["--usage-file", str(usage_path)]
        return [*command, "chat", "--query-file", "-", "--oneshot"]
    return config["command"]


def parse_response(harness: str, stdout: str, output_path: Path) -> dict:
    if harness == "codex":
        payload = output_path.read_text(encoding="utf-8")
    elif harness == "claude":
        wrapper = json.loads(stdout)
        payload = wrapper.get("structured_output")
        if payload is None:
            payload = wrapper.get("result", wrapper)
    elif harness == "hermes":
        payload = stdout.strip()
        if payload.startswith("```json") and payload.endswith("```"):
            payload = payload[7:-3].strip()
    else:
        payload = stdout
    envelope = json.loads(payload) if isinstance(payload, str) else payload
    validate(envelope, ENVELOPE_SCHEMA, "response")
    return envelope


def invoke(config: dict, prompt: str) -> tuple[dict, dict | None]:
    harness = config["harness"]
    with tempfile.TemporaryDirectory(prefix="skillstate-") as temp:
        temp_dir = Path(temp)
        schema_path = temp_dir / "envelope.schema.json"
        output_path = temp_dir / "output.json"
        usage_path = temp_dir / "usage.json"
        write_json(schema_path, ENVELOPE_SCHEMA)
        result = subprocess.run(harness_command(config, schema_path, output_path, prompt, usage_path), input=prompt, text=True, encoding="utf-8", capture_output=True, cwd=config["workspace"], timeout=config["model_timeout_seconds"])
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip()[-2000:])
        usage = read_json(usage_path) if usage_path.exists() else None
        return parse_response(harness, result.stdout, output_path), usage


def propose(config: dict, prompt: str, state: dict, schema: dict) -> tuple[dict, dict, dict | None]:
    usages = []
    attempts = config.get("validation_retries", 2) + 1
    for attempt in range(attempts):
        try:
            envelope, usage = invoke(config, prompt)
            usages.append(usage)
            patch = json.loads(envelope["state_patch_json"])
            candidate = merge_patch(state, patch)
            validate_state(candidate, schema)
            return envelope, candidate, usage if len(usages) == 1 else {"attempts": usages}
        except (ValueError, json.JSONDecodeError) as exc:
            if attempt + 1 == attempts:
                raise
            prompt += (
                "\nYour previous response was rejected without changing state: "
                f"{str(exc)[:1000]}\nReturn a corrected five-key JSON object now."
            )
    raise AssertionError("unreachable")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_integrity(run_dir: Path) -> None:
    integrity = read_json(run_dir / "integrity.json")
    for name in ("spec.md", "schema.json"):
        if integrity.get(name) != sha256_file(run_dir / name):
            raise RuntimeError(f"immutable runtime input changed: {name}")


def confined_path(workspace: Path, base: Path, value: str, *, existing: bool) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        raise RuntimeError(f"absolute path denied: {value}")
    resolved = (base / candidate).resolve()
    if not resolved.is_relative_to(workspace):
        raise RuntimeError(f"path outside workspace: {value}")
    runtime_home = HOME.resolve()
    if resolved == runtime_home or resolved.is_relative_to(runtime_home):
        raise RuntimeError("runtime state path denied")
    if existing and not resolved.exists():
        raise RuntimeError(f"path not found: {value}")
    return resolved


def execute_action(config: dict, argv: list[str], cwd_text: str) -> str | None:
    if not argv:
        return None
    capability, *arguments = argv
    if capability not in config["allowed_capabilities"]:
        raise RuntimeError(f"capability denied: allow with --allow {capability}")
    workspace = Path(config["workspace"]).resolve()
    cwd = confined_path(workspace, workspace, cwd_text or ".", existing=True)
    if not cwd.is_dir():
        raise RuntimeError(f"action cwd is not a directory: {cwd_text}")
    if capability == "list_files":
        if len(arguments) > 1:
            raise RuntimeError("list_files expects zero or one path")
        target = confined_path(workspace, cwd, arguments[0] if arguments else ".", existing=True)
        if not target.is_dir():
            raise RuntimeError("list_files target is not a directory")
        output = "\n".join(sorted(item.name for item in target.iterdir())[:200])
    elif capability == "read_text":
        if len(arguments) != 1:
            raise RuntimeError("read_text expects one path")
        target = confined_path(workspace, cwd, arguments[0], existing=True)
        if not target.is_file():
            raise RuntimeError("read_text target is not a file")
        with target.open("rb") as file:
            data = file.read(MAX_ACTION_BYTES + 1)
        if len(data) > MAX_ACTION_BYTES:
            raise RuntimeError(f"read_text exceeds {MAX_ACTION_BYTES} bytes")
        output = data.decode("utf-8", errors="replace")
    elif capability == "write_text":
        if len(arguments) != 2:
            raise RuntimeError("write_text expects path and content")
        data = arguments[1].encode("utf-8")
        if len(data) > MAX_ACTION_BYTES:
            raise RuntimeError(f"write_text exceeds {MAX_ACTION_BYTES} bytes")
        target = confined_path(workspace, cwd, arguments[0], existing=False)
        if not target.parent.is_dir():
            raise RuntimeError("write_text parent directory does not exist")
        with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False) as file:
            file.write(data)
            temporary = Path(file.name)
        os.replace(temporary, target)
        output = f"wrote {len(data)} bytes to {target.relative_to(workspace)}"
    elif capability == "mkdir":
        if len(arguments) != 1:
            raise RuntimeError("mkdir expects one path")
        target = confined_path(workspace, cwd, arguments[0], existing=False)
        target.mkdir()
        output = f"created {target.relative_to(workspace)}"
    else:  # Config is validated at initialization; keep corrupted configs fail-closed.
        raise RuntimeError(f"unknown capability: {capability}")
    return json.dumps({"capability": capability, "ok": True, "output": output}, ensure_ascii=False)


def repair_jsonl_tail(path: Path) -> None:
    if not path.exists() or not path.stat().st_size:
        return
    with path.open("rb+") as file:
        file.seek(-1, os.SEEK_END)
        if file.read(1) == b"\n":
            return
        size = file.tell()
        start = 0
        while size > 0:
            length = min(65536, size)
            size -= length
            file.seek(size)
            chunk = file.read(length)
            index = chunk.rfind(b"\n")
            if index >= 0:
                start = size + index + 1
                break
        file.seek(start)
        tail = file.read()
        try:
            json.loads(tail.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            file.truncate(start)
        else:
            file.seek(0, os.SEEK_END)
            file.write(b"\n")


def last_jsonl_record(path: Path) -> dict | None:
    repair_jsonl_tail(path)
    if not path.exists() or not path.stat().st_size:
        return None
    with path.open("rb") as file:
        position = file.seek(0, os.SEEK_END)
        buffer = b""
        while position:
            length = min(65536, position)
            position -= length
            file.seek(position)
            buffer = file.read(length) + buffer
            stripped = buffer.rstrip(b"\n")
            index = stripped.rfind(b"\n")
            if index >= 0 or position == 0:
                return json.loads(stripped[index + 1:].decode("utf-8"))
    return None


def append_audit(run_dir: Path, revision: int, observation: str, envelope: dict, usage: dict | None, transition_id: str | None = None) -> None:
    record = {"time": time.time(), "transition_id": transition_id, "revision": revision, "observation": observation, "response": envelope, "usage": usage, "committed": True}
    path = run_dir / "audit.jsonl"
    repair_jsonl_tail(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def finish_intent(run_dir: Path, intent: dict) -> None:
    record = {key: intent.get(key) for key in ("id", "phase", "revision", "response", "action_observation", "error")}
    write_json(run_dir / "actions" / f"{intent['id']}.json", record)
    if intent.get("phase") == "failed":
        action = intent.get("response", {}).get("action_argv", [])
        write_json(run_dir / "action-feedback.json", {
            "transition_id": intent["id"],
            "capability": action[0] if action else None,
            "ok": False,
            "error": intent.get("error") or "action failed",
        })
    (run_dir / "pending-action.json").unlink(missing_ok=True)


def prepare_intent(run_dir: Path, schema: dict, intent: dict) -> dict:
    validate_state(intent["next_state"], schema)
    state_doc = read_json(run_dir / "state.json")
    if state_doc["revision"] == intent["base_revision"]:
        write_json(run_dir / "state.json", {"revision": intent["revision"], "state": intent["next_state"]})
    elif state_doc != {"revision": intent["revision"], "state": intent["next_state"]}:
        raise RuntimeError("pending action does not match durable semantic state")
    intent["phase"] = "state_committed"
    write_json(run_dir / "pending-action.json", intent)
    audit_path = run_dir / "audit.jsonl"
    if (last_jsonl_record(audit_path) or {}).get("transition_id") != intent["id"]:
        append_audit(
            run_dir, intent["revision"], intent["observation"], intent["response"],
            intent.get("usage"), intent["id"],
        )
    intent["phase"] = "prepared"
    write_json(run_dir / "pending-action.json", intent)
    feedback_path = run_dir / "action-feedback.json"
    if intent.get("feedback_transition_id") is not None and feedback_path.exists():
        feedback = read_json(feedback_path)
        if feedback.get("transition_id") == intent["feedback_transition_id"]:
            feedback_path.unlink()
    return intent


def transition(run_dir: Path, observation: str, execute: bool) -> tuple[dict, str | None]:
    config = read_json(run_dir / "config.json")
    verify_integrity(run_dir)
    schema = read_json(run_dir / "schema.json")
    pending_path = run_dir / "pending-action.json"
    if pending_path.exists():
        pending = read_json(pending_path)
        if pending["phase"] in {"succeeded", "failed"}:
            finish_intent(run_dir, pending)
        elif pending["phase"] in {"state_pending", "state_committed"}:
            pending = prepare_intent(run_dir, schema, pending)
            if execute:
                return pending["response"], execute_intent(run_dir, config, pending)
            return pending["response"], None
        elif pending["phase"] == "prepared":
            if execute:
                return pending["response"], execute_intent(run_dir, config, pending)
            return pending["response"], None
        else:
            raise RuntimeError("unresolved action outcome; run skill-state recover")
    state_doc = read_json(run_dir / "state.json")
    validate_state(state_doc["state"], schema)
    feedback_path = run_dir / "action-feedback.json"
    feedback = read_json(feedback_path) if feedback_path.exists() else None
    effective_observation = observation
    if feedback is not None:
        effective_observation = (
            "Previous action result (durable runtime observation):\n"
            + json.dumps(feedback, ensure_ascii=False, separators=(",", ":"))
            + "\n\nLatest external observation:\n"
            + observation
        )
    prompt = prompt_for(
        (run_dir / "spec.md").read_text(encoding="utf-8"), schema,
        state_doc["state"], effective_observation, config["allowed_capabilities"],
    )
    envelope, candidate, usage = propose(config, prompt, state_doc["state"], schema)
    if not execute:
        return envelope, None
    if envelope["status"] == "continue" and not envelope["action_argv"]:
        raise ValueError("continue response requires an action")
    next_doc = {"revision": state_doc["revision"] + 1, "state": candidate}
    intent = {
        "id": f"r{next_doc['revision']}-{time.time_ns()}", "phase": "state_pending",
        "base_revision": state_doc["revision"], "revision": next_doc["revision"],
        "next_state": candidate, "observation": effective_observation, "usage": usage, "response": envelope,
        "feedback_transition_id": feedback.get("transition_id") if feedback else None,
        "action_observation": None, "error": None,
    }
    write_json(pending_path, intent)
    intent = prepare_intent(run_dir, schema, intent)
    return envelope, execute_intent(run_dir, config, intent)


def execute_intent(run_dir: Path, config: dict, intent: dict) -> str | None:
    pending_path = run_dir / "pending-action.json"
    intent["phase"] = "started"
    write_json(pending_path, intent)
    try:
        action_observation = execute_action(
            config, intent["response"]["action_argv"], intent["response"]["action_cwd"]
        )
    except Exception as exc:
        intent.update(phase="ambiguous" if isinstance(exc, subprocess.TimeoutExpired) else "failed", error=str(exc))
        write_json(pending_path, intent)
        if intent["phase"] == "failed":
            finish_intent(run_dir, intent)
        raise
    intent.update(phase="succeeded", action_observation=action_observation)
    write_json(pending_path, intent)
    finish_intent(run_dir, intent)
    return action_observation


def command_init(args) -> None:
    run_dir = HOME / "runs" / args.name
    if run_dir.exists():
        raise RuntimeError(f"run exists: {run_dir}")
    schema = read_json(Path(args.schema))
    state = read_json(Path(args.state))
    validate_state(state, schema)
    run_dir.mkdir(parents=True)
    (run_dir / "spec.md").write_text(Path(args.spec).read_text(encoding="utf-8"), encoding="utf-8")
    write_json(run_dir / "schema.json", schema)
    write_json(run_dir / "integrity.json", {name: sha256_file(run_dir / name) for name in ("spec.md", "schema.json")})
    write_json(run_dir / "state.json", {"revision": 0, "state": state})
    capabilities = list(dict.fromkeys(args.allow))
    unknown = set(capabilities) - CAPABILITIES
    if unknown:
        raise ValueError(f"unknown capabilities: {', '.join(sorted(unknown))}")
    config = {"harness": args.harness, "command": args.command, "workspace": str(Path(args.workspace).resolve()), "allowed_capabilities": capabilities, "validation_retries": args.validation_retries, "model_timeout_seconds": args.model_timeout}
    if args.harness == "command" and not args.command:
        raise ValueError("--command is required for command harness")
    write_json(run_dir / "config.json", config)
    print(run_dir)


def command_step(args) -> None:
    run_dir = HOME / "runs" / args.name
    observation = args.observation if args.observation is not None else sys.stdin.read()
    with run_lock(run_dir):
        envelope, action_observation = transition(run_dir, observation, args.execute)
    print(json.dumps({"response": envelope, "action_observation": action_observation}, ensure_ascii=False, indent=2))


def command_run(args) -> None:
    run_dir = HOME / "runs" / args.name
    observation = args.observation if args.observation is not None else sys.stdin.read()
    with run_lock(run_dir):
        for _ in range(args.max_steps):
            envelope, action_observation = transition(run_dir, observation, True)
            print(json.dumps(envelope, ensure_ascii=False))
            if envelope["status"] != "continue":
                return
            if action_observation is None:
                raise RuntimeError("continue response requires an action")
            observation = action_observation
    raise RuntimeError(f"max steps reached: {args.max_steps}")


def recover_intent(run_dir: Path, result: str) -> str:
    path = run_dir / "pending-action.json"
    if not path.exists():
        raise RuntimeError("no pending action")
    intent = read_json(path)
    phase = intent["phase"]
    if phase in {"succeeded", "failed"}:
        finish_intent(run_dir, intent)
        return f"recovery: recorded known {phase}; state remains committed"
    intent["phase"] = "succeeded" if result == "succeeded" else "failed"
    intent["error"] = None if result == "succeeded" else "operator marked action unsuccessful"
    write_json(path, intent)
    finish_intent(run_dir, intent)
    return f"recovery: action recorded {result}; state remains committed"


def command_recover(args) -> None:
    run_dir = HOME / "runs" / args.name
    with run_lock(run_dir):
        verify_integrity(run_dir)
        pending = read_json(run_dir / "pending-action.json")
        if pending["phase"] in {"state_pending", "state_committed"}:
            prepare_intent(run_dir, read_json(run_dir / "schema.json"), pending)
        print(recover_intent(run_dir, args.result))


def self_test() -> None:
    schema = {"type": "object", "properties": {"todo": {"type": "array", "items": {"type": "string"}}, "meta": {"type": "object", "additionalProperties": {"type": "string"}}}, "required": ["todo", "meta"], "additionalProperties": False}
    state = {"todo": ["a"], "meta": {"old": "x"}}
    merged = merge_patch(state, {"todo": [], "meta": {"old": None, "new": "y"}})
    validate_state(merged, schema)
    assert merged == {"todo": [], "meta": {"new": "y"}}
    try:
        validate_state({"todo": ["x" * MAX_STATE_BYTES], "meta": {}}, schema)
        raise AssertionError("oversized state accepted")
    except ValueError as exc:
        assert "exceeds" in str(exc)
    prompt = prompt_for("do work", schema, merged, "latest only")
    assert "latest only" in prompt and "old observation" not in prompt
    codex_command = harness_command({"harness": "codex"}, Path("schema"), Path("output"))
    claude_command = harness_command({"harness": "claude"}, Path("schema"), Path("output"))
    hermes_command = harness_command({"harness": "hermes"}, Path("schema"), Path("output"), "latest only")
    assert "--ephemeral" in codex_command and "--output-schema" in codex_command
    assert "--no-session-persistence" in claude_command and "--json-schema" in claude_command
    assert hermes_command[-4:] == ["chat", "--query-file", "-", "--oneshot"]
    assert "state-only" in hermes_command and "latest only" not in hermes_command
    envelope = {"state_patch_json": json.dumps({"todo": []}), "action_argv": ["write_text", "result.txt", "next observation"], "action_cwd": "", "status": "continue", "message": "ok"}
    assert parse_response("claude", json.dumps({"structured_output": envelope}), Path("unused")) == envelope
    assert parse_response("claude", json.dumps({"result": json.dumps(envelope)}), Path("unused")) == envelope
    with tempfile.TemporaryDirectory(prefix="skillstate-test-") as temp:
        run_dir = Path(temp)
        (run_dir / "spec.md").write_text("do work", encoding="utf-8")
        write_json(run_dir / "schema.json", schema)
        write_json(run_dir / "integrity.json", {name: sha256_file(run_dir / name) for name in ("spec.md", "schema.json")})
        write_json(run_dir / "state.json", {"revision": 0, "state": state})
        config = {"harness": "command", "command": [sys.executable, "-c", f"print({json.dumps(json.dumps(envelope))})"], "workspace": temp, "allowed_capabilities": ["write_text"], "validation_retries": 2, "model_timeout_seconds": 10}
        write_json(run_dir / "config.json", config)
        transition(run_dir, "preview only", False)
        assert read_json(run_dir / "state.json")["revision"] == 0
        assert not (run_dir / "audit.jsonl").exists()
        response, action_observation = transition(run_dir, "latest only", True)
        assert response["status"] == "continue" and "next observation" in (run_dir / "result.txt").read_text()
        assert json.loads(action_observation)["ok"] is True
        assert read_json(run_dir / "state.json")["revision"] == 1
        assert len(list((run_dir / "actions").glob("*.json"))) == 1
        done = dict(envelope, action_argv=[], status="done")
        config["command"] = [sys.executable, "-c", f"print({json.dumps(json.dumps(done))})"]
        write_json(run_dir / "config.json", config)
        response, action_observation = transition(run_dir, "finish without action", True)
        assert response["status"] == "done" and action_observation is None
        assert read_json(run_dir / "state.json")["revision"] == 2
        assert len(list((run_dir / "actions").glob("*.json"))) == 2
        assert all(
            json.loads(line)["transition_id"]
            for line in (run_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        )
        denied = dict(envelope, state_patch_json=json.dumps({"todo": ["retained"]}), action_argv=["not_allowed"], status="done")
        config["command"] = [sys.executable, "-c", f"print({json.dumps(json.dumps(denied))})"]
        write_json(run_dir / "config.json", config)
        try:
            transition(run_dir, "state must survive action failure", True)
            raise AssertionError("denied action was accepted")
        except RuntimeError as exc:
            assert "capability denied" in str(exc)
        assert read_json(run_dir / "state.json") == {"revision": 3, "state": {"todo": ["retained"], "meta": {"old": "x"}}}
        assert not (run_dir / "pending-action.json").exists()
        assert any(read_json(path)["phase"] == "failed" for path in (run_dir / "actions").glob("*.json"))
        assert read_json(run_dir / "action-feedback.json")["capability"] == "not_allowed"

        retried = dict(done, state_patch_json=json.dumps({"todo": ["retried"]}))
        marker = run_dir / "retry.marker"
        captured = run_dir / "captured-prompt.txt"
        retry_code = (
            f"import sys; from pathlib import Path; p=Path({str(marker)!r}); "
            f"Path({str(captured)!r}).write_text(sys.stdin.read(), encoding='utf-8'); "
            f"print({json.dumps(json.dumps(retried))} if p.exists() else '{{}}'); p.touch()"
        )
        config["command"] = [sys.executable, "-c", retry_code]
        write_json(run_dir / "config.json", config)
        response, _ = transition(run_dir, "retry malformed output", True)
        assert response == retried and read_json(run_dir / "state.json")["revision"] == 4
        captured_prompt = captured.read_text(encoding="utf-8")
        assert '"capability":"not_allowed"' in captured_prompt and '"ok":false' in captured_prompt
        assert "retry malformed output" in captured_prompt
        assert not (run_dir / "action-feedback.json").exists()

        crash_state = {"todo": ["crash recovered"], "meta": {"old": "x"}}
        crash_intent = {
            "id": "state-window-test", "phase": "state_pending", "base_revision": 4,
            "revision": 5, "next_state": crash_state, "observation": "recover state window",
            "usage": None, "response": envelope, "action_observation": None, "error": None,
        }
        write_json(run_dir / "pending-action.json", crash_intent)
        prepared = prepare_intent(run_dir, schema, crash_intent)
        audit_lines = (run_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        prepare_intent(run_dir, schema, prepared)
        assert (run_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines() == audit_lines
        assert read_json(run_dir / "state.json") == {"revision": 5, "state": crash_state}
        assert recover_intent(run_dir, "failed") == "recovery: action recorded failed; state remains committed"

        recovered = {"id": "recovery-test", "phase": "ambiguous", "revision": 5, "response": done, "action_observation": None, "error": "timeout"}
        write_json(run_dir / "pending-action.json", recovered)
        with (run_dir / "audit.jsonl").open("ab") as f:
            f.write('{"torn":"é'.encode("utf-8")[:-1])
        append_audit(run_dir, 5, "tail repair", done, None)
        for line in (run_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines():
            json.loads(line)
        assert recover_intent(run_dir, "succeeded") == "recovery: action recorded succeeded; state remains committed"
        assert read_json(run_dir / "state.json")["revision"] == 5
        assert read_json(run_dir / "actions" / "recovery-test.json")["phase"] == "succeeded"

        try:
            execute_action(config, ["write_text", str(Path("..") / "escape.txt"), "bad"], "")
            raise AssertionError("workspace escape accepted")
        except RuntimeError as exc:
            assert "outside workspace" in str(exc)
        (run_dir / "spec.md").write_text("tampered", encoding="utf-8")
        try:
            transition(run_dir, "must reject tampering", False)
            raise AssertionError("immutable procedure tampering accepted")
        except RuntimeError as exc:
            assert "immutable runtime input changed" in str(exc)
        with run_lock(run_dir):
            try:
                with run_lock(run_dir):
                    raise AssertionError("concurrent writer accepted")
            except RuntimeError:
                pass
        (run_dir / ".lock").write_text("stale pid", encoding="utf-8")
        with run_lock(run_dir):
            pass
    print("self-test: ok")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="skill-state")
    sub = root.add_subparsers(dest="subcommand", required=True)
    init = sub.add_parser("init")
    init.add_argument("name")
    init.add_argument("--spec", required=True)
    init.add_argument("--schema", required=True)
    init.add_argument("--state", required=True)
    init.add_argument("--workspace", default=".")
    init.add_argument("--harness", choices=["codex", "claude", "hermes", "command"], default="codex")
    init.add_argument("--command", action="append", default=[])
    init.add_argument("--allow", action="append", choices=sorted(CAPABILITIES), default=[])
    init.add_argument("--validation-retries", type=int, choices=range(0, 4), default=2)
    init.add_argument("--model-timeout", type=int, default=900)
    init.set_defaults(func=command_init)
    step = sub.add_parser("step")
    step.add_argument("name")
    step.add_argument("--observation")
    step.add_argument("--execute", action="store_true")
    step.set_defaults(func=command_step)
    run = sub.add_parser("run")
    run.add_argument("name")
    run.add_argument("--observation")
    run.add_argument("--max-steps", type=int, default=50)
    run.set_defaults(func=command_run)
    recover = sub.add_parser("recover")
    recover.add_argument("name")
    recover.add_argument("--result", choices=["succeeded", "failed"], required=True)
    recover.set_defaults(func=command_recover)
    sub.add_parser("self-test").set_defaults(func=lambda _: self_test())
    return root


def main() -> None:
    try:
        args = parser().parse_args()
        args.func(args)
    except (ValueError, RuntimeError, OSError, SchemaError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"skill-state: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

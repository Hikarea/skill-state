"""Live installed-Hermes benchmark; never substitutes a simulated context engine."""

import argparse
from datetime import datetime, timezone
import faulthandler
import hashlib
import json
import os
import random
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


COUNTS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens", "total_tokens")


def workload(seed, directory):
    """Generated ground truth; corrections and exact detail are not preloaded into state."""
    tag = f"project-{seed * 137 + 91}"
    old = f"release-{seed * 43 + 7}"
    new = f"release-{seed * 73 + 11}"
    region = f"region-{seed * 31 + 5}"
    noise = "\n".join(f"DISPOSABLE-{seed}-{i}: cedar orbit canvas checksum {seed * 991 + i * 37}" for i in range(70))
    later_noise = noise.replace("DISPOSABLE-", "LATER-DIAGNOSTIC-")
    if seed % 2 == 0:
        return [
            f"Remember project {tag}, release {old}, region {region}. Reply only ACK. The following diagnostic noise is disposable:\n{noise}",
            f"Correction: release is now {new}; the previous release is obsolete. Project and region are unchanged. Reply only ACK. Disposable diagnostic noise:\n{later_noise}",
            "Return only the project, current release, and region joined by |. Use the latest correction; do not guess.",
        ], f"{tag}|{new}|{region}", "corrected_memory"
    (directory / "first.txt").write_text(f"Project: {tag}\nRelease: {old}\nRegion: {region}\nNext: second.txt\n{noise}", encoding="utf-8")
    (directory / "second.txt").write_text(f"Correction: release is {new}.\nProject and region unchanged.\n{later_noise}", encoding="utf-8")
    return ["Read first.txt, then the file named in its Next field. Return only the project, corrected release, and region joined by |. Do not guess. Diagnostic noise is disposable."], f"{tag}|{new}|{region}", "tool_chain"


def emit(path, record):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=True) + "\n")


def campaign(args):
    if args.output.exists():
        raise SystemExit("Use a new output directory; never overwrite or mix benchmark runs")
    args.output.mkdir(parents=True)
    root = Path(__file__).resolve().parent
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    hermes_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.hermes_source, text=True).strip()
    seeds = list(range(100, 100 + args.pairs))
    random.Random(731).shuffle(seeds)
    manifest = {"skill_state_revision": revision, "hermes_revision": hermes_revision,
                "pairs": args.pairs, "order_seed": 731, "seeds": seeds,
                "policy": "serial; balanced order within each family; fresh isolated profiles; 16 iterations per turn; 120s turn budget; 420s worker timeout; failures retained",
                "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "bridge_sha256": hashlib.sha256((root / "integrations/hermes_transition_bridge.py").read_bytes()).hexdigest(),
                "hermes_conversation_loop_sha256": hashlib.sha256((args.hermes_source / "agent/conversation_loop.py").read_bytes()).hexdigest()}
    from integrations.install_hermes import FILES
    manifest.update(plugin_hashes={name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in FILES},
                    hermes_dirty=bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=args.hermes_source)),
                    hermes_diff_sha256=hashlib.sha256(subprocess.check_output(["git", "diff", "HEAD"], cwd=args.hermes_source)).hexdigest(),
                    started_utc=datetime.now(timezone.utc).isoformat())
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    rows = []
    for seed in seeds:
        order = ["vanilla", "skill_state"] if (seed // 2) % 2 == 0 else ["skill_state", "vanilla"]
        for arm in order:
            target = args.output / f"{seed}-{arm}.json"
            command = [sys.executable, str(Path(__file__).resolve()), "--hermes-source", str(args.hermes_source),
                       "--hermes-home", str(args.hermes_home), "--arm", arm, "--seed", str(seed), "--output", str(target.resolve())]
            begin = time.perf_counter()
            # Logs can contain local paths. Keep these local, never publish blindly.
            with target.with_suffix(".log").open("w", encoding="utf-8") as log:
                run = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, text=True)
            if run.returncode == 0 and target.exists():
                row = json.loads(target.read_text(encoding="utf-8"))
            else:
                row = {"seed": seed, "arm": arm, "accuracy": 0, "harness_error": True, "exit_code": run.returncode}
            row["process_wall_seconds"] = time.perf_counter()-begin
            rows.append(row)
            (args.output / "campaign.json").write_text(json.dumps({"manifest": manifest, "runs": rows}, indent=2), encoding="utf-8")
            print(json.dumps({k: row.get(k) for k in ("seed", "arm", "accuracy", "total_tokens", "api_calls", "wall_seconds", "harness_error")}), flush=True)


def worker(args):
    sys.path.insert(0, str(args.hermes_source))
    faulthandler.dump_traceback_later(45, repeat=True)
    from hermes_cli.config import load_config
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from run_agent import AIAgent

    config = load_config()
    model_config = config.get("model") or {}
    model = model_config if isinstance(model_config, str) else model_config.get("default") or model_config.get("model")
    if not isinstance(model, str):
        raise RuntimeError("Benchmark requires an explicit string default model")
    runtime = resolve_runtime_provider(
        requested=None, target_model=model,
    )
    import toolsets
    toolsets.TOOLSETS["benchmark_readonly"] = {"description": "Benchmark native file reads", "tools": ["read_file"]}
    agent = AIAgent(
        model=model, api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"), provider=runtime.get("provider"),
        requested_provider=runtime.get("requested_provider"),
        api_mode=runtime.get("api_mode"), credential_pool=runtime.get("credential_pool"),
        enabled_toolsets=["context_engine", "benchmark_readonly"], max_iterations=16, quiet_mode=True,
        reasoning_config={"effort": "minimal"}, skip_context_files=True,
        skip_memory=True, skip_background_review=True, load_soul_identity=False,
        save_trajectories=False, run_budget_seconds=120,
    )
    engine = type(agent.context_compressor).__name__
    expected_engine = "PerStepEngine" if args.arm == "skill_state" else "ContextCompressor"
    if engine != expected_engine:
        raise RuntimeError(f"Wrong engine: {engine}; expected {expected_engine}")
    events = args.output.with_suffix(".events.jsonl")
    if events.exists() or args.output.exists():
        raise RuntimeError("Output already exists; use a fresh output path")
    calls = []
    attempts, responses, errors, compressions = [], [], [], []
    compress = agent.context_compressor.compress
    def traced_compress(*positional, **keywords):
        compressions.append(True)
        emit(events, {"kind": "compression_called"})
        return compress(*positional, **keywords)
    agent.context_compressor.compress = traced_compress
    # Observe the provider transport boundary without changing messages or results.
    if agent.api_mode != "codex_responses":
        raise RuntimeError("This audited boundary currently supports Hermes codex_responses only")
    send = agent._run_codex_stream
    def traced_send(kwargs, *positional, **keywords):
        attempts.append(True)
        body = json.dumps({key: kwargs.get(key) for key in ("model", "input", "messages", "instructions", "tools", "reasoning")}, sort_keys=True, default=str)
        emit(events, {"kind": "provider_request", "sha256": hashlib.sha256(body.encode()).hexdigest(),
                      "selected_kwargs_bytes": len(body.encode()), "has_state_payload": 'latest_observation' in body,
                      "has_previous_disposable": f"DISPOSABLE-{args.seed}-0" in body,
                      "model": kwargs.get("model"), "reasoning": kwargs.get("reasoning")})
        begin = time.perf_counter()
        try:
            response = send(kwargs, *positional, **keywords)
        except Exception as exc:
            errors.append(type(exc).__name__)
            emit(events, {"kind": "provider_error", "type": type(exc).__name__})
            raise
        raw = getattr(response, "usage", None)
        raw = raw.model_dump() if hasattr(raw, "model_dump") else raw
        responses.append(raw)
        emit(events, {"kind": "provider_response", "wall_seconds": time.perf_counter()-begin, "usage": raw})
        return response
    agent._run_codex_stream = traced_send
    account = agent.context_compressor.update_from_response
    def traced_usage(usage, *positional, **keywords):
        calls.append({key: usage.get(key) for key in COUNTS})
        emit(events, {"kind": "canonical_usage", **calls[-1]})
        return account(usage, *positional, **keywords)
    agent.context_compressor.update_from_response = traced_usage
    items, expected, family = workload(args.seed, Path.cwd())
    started = time.perf_counter()
    history, turns = [], []
    try:
        keep = ("final_response", "input_tokens", "cache_read_tokens", "cache_write_tokens",
                "output_tokens", "reasoning_tokens", "total_tokens", "api_calls",
                "estimated_cost_usd", "cost_status", "cost_source", "model", "provider",
                "failed", "completed", "turn_exit_reason")
        for index, prompt in enumerate(items):
            begin = time.perf_counter()
            previous_history_length = len(history)
            result = agent.run_conversation(prompt, conversation_history=history)
            history = result.get("messages", [])
            row = {key: result.get(key) for key in keep}
            row.update(turn=index, wall_seconds=time.perf_counter()-begin)
            turns.append(row)
            emit(events, {"kind": "turn", **row})
            for message in history[previous_history_length:]:
                if message.get("role") == "tool":
                    emit(events, {"kind": "tool_result", "turn": index, "name": message.get("name"),
                                  "content": message.get("content", "")})
            if not result.get("completed") or result.get("failed") or result.get("interrupted"):
                break
        report = {key: result.get(key) for key in keep}
        report.update(engine=engine, wall_seconds=time.perf_counter()-started, turns=turns,
                      seed=args.seed, arm=args.arm, family=family, expected=expected,
                      accuracy=int(len(turns) == len(items) and all(t["completed"] and not t["failed"] for t in turns) and result.get("final_response", "").strip() == expected),
                      provider_usage_calls=len(calls), api_calls=sum(t["api_calls"] for t in turns))
        report.update(provider_transport_attempts=len(attempts), provider_responses=len(responses),
                      provider_errors=errors, compression_calls=len(compressions), api_mode=agent.api_mode,
                      requested_reasoning="minimal", service_tier=result.get("service_tier"))
        # Hermes result counters are session-cumulative, NOT per-turn. Never sum them.
        report["usage_reconciles"] = all(sum(c.get(k) or 0 for c in calls) == (result.get(k) or 0) for k in COUNTS)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({k: report[k] for k in ("arm", "seed", "accuracy", "api_calls", "total_tokens", "wall_seconds", "usage_reconciles")}), flush=True)
    finally:
        agent.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--hermes-home", type=Path)
    parser.add_argument("--arm", choices=["vanilla", "skill_state"], default="vanilla")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--pairs", type=int, default=0)
    args = parser.parse_args()
    if args.pairs:
        if args.pairs < 4 or args.pairs % 4:
            parser.error("--pairs must be a positive multiple of four for family/order balance")
        if args.hermes_home is None:
            parser.error("--hermes-home is required")
        return campaign(args)
    if args.worker:
        return worker(args)
    import yaml
    if args.hermes_home is None:
        parser.error("--hermes-home is required")
    with tempfile.TemporaryDirectory(prefix="skill-state-live-") as temporary:
        profile = Path(temporary)
        config = yaml.safe_load((args.hermes_home / "config.yaml").read_text(encoding="utf-8"))
        config["context"] = {**config.get("context", {}), "engine": "skill-state" if args.arm == "skill_state" else "compressor"}
        config["plugins"] = {"enabled": ["skill-state"] if args.arm == "skill_state" else []}
        config["mcp_servers"] = {}
        (profile / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        for name in ("auth.json", ".env", "models_dev_cache.json", "context_length_cache.yaml"):
            if (args.hermes_home / name).is_file():
                shutil.copy2(args.hermes_home / name, profile / name)
        if args.arm == "skill_state":
            from integrations.install_hermes import FILES
            plugin = profile / "plugins" / "skill-state"
            plugin.mkdir(parents=True)
            for name in FILES:
                shutil.copy2(Path(__file__).parent / name, plugin / name)
            (profile / "skill-state").mkdir()
            (profile / "skill-state" / "config.json").write_text('{"mode":"step","context_mode":"strict"}', encoding="utf-8")
        env = {**os.environ, "HERMES_HOME": str(profile)}
        env.pop("SKILL_STATE_PROPOSAL_WORKER", None)
        workspace = profile / "workspace"
        workspace.mkdir()
        env["TERMINAL_CWD"] = str(workspace)
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--hermes-source", str(args.hermes_source), "--arm", args.arm, "--output", str(args.output.resolve()), "--seed", str(args.seed)]
        subprocess.run(command, env=env, cwd=workspace, check=True, timeout=420)


if __name__ == "__main__":
    main()

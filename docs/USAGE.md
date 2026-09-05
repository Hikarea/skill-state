# Configuration and alternative execution paths

## Use a domain schema in Hermes

The default state has objective, status, completed, pending, facts, blockers and next fields. The supplied research schema instead tracks sourced claims, their verification status, constraints, open questions and failed attempts.

Run with the Python from your Hermes environment so the `jsonschema` dependency is available there:

```bash
python -m pip install -e .
python integrations/install_hermes.py --mode step --context-mode evidence --schema examples/research.schema.json --state examples/research.initial.json
```

Restart Hermes and start a new session. The schema is fixed within a session; changing it requires a new session or an explicit state migration. The installer saves settings to the selected Hermes home's `skill-state/config.json`. Reinstalling without schema arguments preserves an existing domain schema. To restore the generic schema, remove `state_schema` and `initial_state` from that file before starting a new session.

Use `--home` when Hermes home cannot be discovered automatically. If Hermes itself is not on PATH, pass its executable through `--hermes`.

## Restore ordinary Hermes context management

```bash
hermes config set context.engine compressor
hermes plugins disable skill-state
```

Restart Hermes and start a new session. Stored state/evidence is retained. To use the earlier checkpoint-per-user-turn implementation instead, reinstall with `--mode turn` and restart; `--context-mode evidence` is a step-mode feature and does not add retrieval to turn mode.

## Standalone runtime

This path combines the proposed state delta and action in one model response. Unlike the native plugin, its executor exposes only the explicitly allowed workspace capabilities: file listing, text reads/writes, directory creation and optional evidence reads/search. It is not a replacement for Hermes' complete tool collection.

Example using an installed and authenticated Codex CLI:

```bash
skill-state init inspect-project --spec examples/research.procedure.md --schema examples/research.schema.json --state examples/research.initial.json --workspace . --harness codex --allow list_files --allow read_text
skill-state step inspect-project --observation "Inspect README.md and report the project's implementation status."
skill-state run inspect-project --observation "Inspect README.md and report the project's implementation status." --max-steps 20
```

`step` previews unless `--execute` is supplied. `run` executes. Replace `--harness codex` with `--harness claude` for an installed Claude Code CLI. For Hermes, choose `--harness hermes` and pass the path of Hermes' installed Python executable via `--hermes-python`. The worker loads Hermes' configured provider, supplies fresh history, disables optional memory/context-file loading, and exposes no model tools; the outer runtime executes the selected capability. No custom `state-only` CLI toolset is needed by this new worker.

To enable standalone retrieval, add `--context-mode evidence --allow evidence_read --allow evidence_search` at initialization. To change input limits, use `--max-prompt-bytes` and `--max-observation-bytes`. The defaults are 65,536 and 16,384 UTF-8 bytes, respectively. They bound the assembled runtime input, not additional provider or harness framing.

## Interrupted actions

Successful action feedback survives a standalone restart until the next committed transition consumes it. A preview does not consume it. If an action's outcome is ambiguous, inspect the affected system first, then record the actual outcome:

```bash
skill-state recover inspect-project --result succeeded
```

Use `--result failed` if it did not succeed. This records the result; it does not rerun or undo the external action.

# skill-state

Experimental, harness-neutral implementation of the execution architecture introduced in [*SKILL.state: Scalable Long-Horizon Agent Skills*](https://arxiv.org/abs/2608.26263) by Sanket Badhe, Priyanka Tiwari, and Jonghyun Chung (arXiv:2608.26263v3, 2026).

This is an independent proof of concept. It is not the authors' official implementation and is not affiliated with their institutions.

## Architecture

At step `t`, the model receives an immutable procedure `P`, validated structured state `S_t`, and the latest observation `O_t`. It proposes a state update and action; transient reasoning and older transcript messages are not inputs to the next fresh model call. This follows the paper's central abstraction:

```text
A_t = (P, S_t, O_t)
```

Prompt size is bounded only when `P`, `S_t`, and `O_t` are themselves bounded. The schema must also be a sufficient statistic for future decisions. These are assumptions, not guarantees supplied by the package.

The runtime enforces:

- JSON Schema validation before state commits
- 0-3 fresh-call repair attempts after invalid model output (default: two)
- procedure and schema hash checks before transitions
- atomic state-file replacement and one writer per run
- workspace-confined filesystem capabilities; no model-selected subprocesses
- durable action-result and append-only state-audit records

The Python runtime uses the standard library plus `jsonschema`.

## Install and use

```powershell
python -m pip install -e .
skill-state self-test
```

Create `spec.md`, `schema.json`, and a conforming `initial-state.json`, then initialize a run:

```powershell
skill-state init repair-api `
  --spec .\spec.md `
  --schema .\schema.json `
  --state .\initial-state.json `
  --workspace . `
  --harness codex `
  --allow read_text `
  --allow write_text
```

Preview or execute a transition:

```powershell
skill-state step repair-api --observation "Tests fail in auth.test.ts"
skill-state step repair-api --observation "Tests fail in auth.test.ts" --execute
skill-state run repair-api --observation "Tests fail in auth.test.ts" --max-steps 20
```

`step` is a preview unless `--execute` is supplied. `run` is execution consent. State and audit data default to `%USERPROFILE%\.skillstate\runs\<name>`.

After an interrupted capability, inspect the external system before resolving its durable intent:

```powershell
skill-state recover repair-api --result succeeded
skill-state recover repair-api --result failed
```

## Harness adapters

Adapters are implemented for fresh, noninteractive Codex CLI, Claude Code, Hermes, and arbitrary commands that accept a prompt on standard input and return the five-field JSON envelope. Their presence is not a performance claim; this repository's committed experiment used only Hermes.

### Standalone Hermes plugin

The plugin is installed separately into Hermes and uses its context-engine and tool APIs. Ordinary plugin use does not patch the Hermes application:

```powershell
python .\integrations\install_hermes.py
```

Restart Hermes Desktop, its gateway, or the CLI after installation. The plugin saves a validated checkpoint through an internal tool and replaces prior completed-turn messages with that checkpoint at the next user turn. It deliberately preserves system/developer messages and active tool loops. It does not delete Hermes' local audit history.

The plugin contains no marker parser and no output-transformation hook. Its contract tests verify persistence, invalid-state rejection, next-turn context selection, and preservation of an active tool loop.

## Committed experiment

### Question

On one synthetic continuity task family, does state-only fresh-call execution reduce model-context tokens without reducing exact-answer success relative to a resumed transcript?

### Method

- 20 paired cases, seeds 200-219, on Windows with Hermes Agent v0.21.0 and GPT-5.6 Terra through the `openai-codex` provider.
- Each pair used identical durable values and transient notebooks. Cases varied 2-6 notebooks and 40-160 filler lines per notebook.
- Execution order was balanced 10/10 overall and within the notebook-count strata.
- The vanilla condition resumed one Hermes transcript. The state-only condition made fresh calls with only the procedure, schema, current state, and latest observation.
- A small, checksum-recorded Hermes patch enabled resumable one-shot baseline calls and an empty benchmark toolset. It is not required by the standalone plugin.
- Primary descriptive endpoints were exact-answer success and context-input tokens. Secondary endpoints were processed tokens, final-turn context, two sampled safety checks, and wall time.
- The campaign made 300 model calls. Committed per-case reports and state snapshots are under [`results/runs`](results/runs/).

### Results

| Endpoint | Observed result |
|---|---:|
| Exact-answer success, vanilla | 20/20 |
| Exact-answer success, state-only | 20/20 |
| Unknown-fact refusal check | 20/20 |
| Fresh-call isolation check | 20/20 |
| Context-input reduction | 47.1% mean, 12.1% sample SD, 21.7%-65.7% range |
| Processed-token reduction | 46.5% mean, 12.2% sample SD, 21.0%-65.3% range |
| Final-turn context reduction | 72.2% mean, 12.5% sample SD, 43.2%-88.2% range |
| Wall-time change | **17.1% slower** mean; 14.1% slower median |
| State-only faster | 3/20 |
| Composite functional-and-latency gate | 5/20 |

`context-input tokens` includes cache-read tokens because cached tokens still occupy model context. `processed tokens` is input plus cache-read plus output tokens; it is not a billing estimate.

The supported conclusion is narrow: all 20 committed cases preserved the exact answer while using fewer context tokens. The experiment found no speed improvement. Because both variants scored 20/20, it provides no evidence that state-only execution is more accurate. It also does not establish lower cost, production safety, cross-model generality, or performance in Codex Desktop.

No inferential p-value is reported: these are related cases from one synthetic task family, not a random sample from a defined task population.

### Verify or replicate

Recompute every committed aggregate from the per-case reports:

```powershell
python -m unittest -v test_benchmark_results.py
```

Run a new 20-case campaign (requires a configured Hermes provider):

```powershell
python .\benchmark_campaign.py `
  --runs 20 `
  --start-seed 200 `
  --output .\benchmark-output\aggregate.json
```

## Limits

- A state schema can discard a fact whose future relevance was not recognized.
- Historical-provenance tasks need history; the audit log is not injected into prompts automatically.
- The runtime is single-agent. Concurrent writers need explicit conflict semantics.
- The safety checks are sampled behaviors, not privacy or isolation proofs.
- Audit records may contain observations and model responses and therefore require appropriate protection.
- There is no supported Codex Desktop transcript-replacement integration in this repository. Host-level context control is required for that claim.

## Status

Proof of concept. Do not describe it as generally faster, cheaper, or more accurate without workload-specific evidence.

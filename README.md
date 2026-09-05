# SKILL.state

**Explicit execution state for long-running LLM agents.**

SKILL.state replaces repeated conversation history with a compact, validated record of the current task. The goal is to reduce total token consumption while retaining the information needed to continue correctly.

This is an independent implementation of [SKILL.state: Scalable Long-Horizon Agent Skills](https://arxiv.org/abs/2608.26263), with an optional evidence-retrieval extension. It runs with Hermes; a new harness is not required.

**Status:** experimental. The original runtime has a recorded model benchmark. The newer Hermes integration has passing contract tests and local measurements, but has not yet been benchmarked end to end with a model.

## How it works

Each step supplies the model with:

1. **Procedure:** the task's instructions and state schema.
2. **State:** current facts, constraints, decisions and remaining work.
3. **Observation:** the latest user input or tool result.

The model proposes a state update. The runtime validates it before committing it, and execution continues from that state. Older reasoning and tool results are not replayed into every request. Hermes' system instructions, tool definitions and authorization remain in place.

**Optional evidence mode** stores exact observations separately. The model can retrieve omitted details through bounded search and paginated reads. This adds no background summarizer or embedding-model calls; retrieval still consumes tokens when used.

## Measured results

### Recorded model benchmark: original runtime

20 paired synthetic tasks using Hermes v0.21.0 and GPT-5.6 Terra. The task was retaining three designated facts among disposable text. Values below are totals across the 20 cases, excluding separate safety probes.

| Measurement | Transcript baseline | State-only | Change |
|---|---:|---:|---:|
| Context-input tokens | 1,060,932 | 501,825 | **−52.7%** |
| Generated tokens | 3,830 | 8,565 | +123.6% |
| Total processed tokens | 1,064,762 | 510,390 | **−52.1%** |
| Elapsed time | 833.01 s | 978.04 s | **+17.4%** |
| Correct answers | 20/20 | 20/20 | Unchanged |

That is **13m 53s → 16m 18s**: fewer tokens, longer runtime. Context-input includes cache reads; these totals are not billing estimates. The previously reported 46.5% token reduction and 17.1% time increase are averages of per-case percentages, rather than changes in campaign totals.

These results apply to the **original patched-CLI experiment**, not the newer per-step plugin. [Raw cases](results/runs/) · [Method and calculations](BENCHMARKS.md)

### Local measurement: current per-step plugin

A scripted 200-action loop, measured in Linux/Python without an LLM or live Hermes. Extra state-update requests, protocol text and tool-schema sizes are included. Times are medians of seven repetitions.

| Measurement | Transcript replay | Per-step state | Change |
|---|---:|---:|---:|
| Cumulative request bytes | 24,691,044 | 1,187,607 | **−95.19%** |
| Local context-management time | 78.55 ms | 166.67 ms | **+112.2%** |

The plugin spends an additional **88.12 ms across 200 actions** on local context work, including checkpoint persistence. This measures neither provider tokens nor full response time. The comparison is against uncompressed transcript replay, not a live run of ordinary Hermes. [Raw timings and source hashes](results/context-local.json)

## Use with Hermes

Requires Python 3.12+ and an installed, configured Hermes exposing context-engine plugin hooks. The `hermes` command must be available in the shell.

```bash
git clone https://github.com/Hikarea/skill-state.git
cd skill-state
python -m pip install -e .
python integrations/install_hermes.py --mode step --context-mode strict
```

Restart Hermes and start a new session. The installer selects this context engine; it does not patch Hermes source. If you explicitly select Hermes toolsets, include `context_engine` so its state-update tool is available.

To enable recovery of omitted historical details:

```bash
python integrations/install_hermes.py --mode step --context-mode evidence
```

The native integration currently requires a separate state-update generation before each external action. Its extra calls must be included when measuring token savings. Short tasks may consume more tokens.

[Domain schemas, standalone runtime and rollback](docs/USAGE.md)

## Limits

- Validation checks state structure, not truth or completeness. The model can still omit an important fact.
- State is limited to 16 KiB. Strict mode blocks oversized observations; evidence mode supplies a bounded excerpt and retrieval reference.
- Step mode supports text. Use `--mode turn` for image/audio tasks and the earlier between-turn checkpoint behavior.
- Keep one writer per session. Native tool execution, approvals and recovery remain Hermes' responsibility.
- The new plugin's real-model token savings and task quality remain unmeasured.

## Verify

```bash
python -m unittest -v
python benchmark_context.py --repeats 7 --output results/context-local.json
```

Tests cover state validation, restart recovery, per-step history isolation, bounded context and evidence retrieval. Without Hermes installed, the tests use a minimal host contract. The local benchmark makes no model calls.

[Architecture and paper fidelity](CONTEXT_DESIGN.md) · [Benchmark methodology](BENCHMARKS.md) · [MIT license](LICENSE)

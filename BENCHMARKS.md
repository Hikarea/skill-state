# Benchmark results and definitions

The datasets below concern different implementations. They must not be combined into one performance claim.

## 1. Recorded model experiment

Sources: [20 case reports](results/runs/), [campaign summary](results/hermes-campaign-20.json), [benchmark](benchmark_hermes.py).

- Hermes Agent v0.21.0, Windows, GPT-5.6 Terra through `openai-codex`.
- Seeds 200–219; 2–6 disposable notebooks of 40–160 lines; three designated durable facts.
- Paired runs with balanced ordering. Vanilla resumes a transcript; state-only uses fresh calls.
- 120 main calls per condition, plus 60 separate safety-probe calls. README totals cover the 240 main calls only. Probe timing/token consumption is not included in the main comparison.
- The old Hermes resume/empty-toolset patch is recorded under `patches/`; these results predate the new native per-step implementation.

| Main-campaign total | Transcript | State-only | Change |
|---|---:|---:|---:|
| Context-input tokens, including cache reads | 1,060,932 | 501,825 | −52.6996% |
| Output tokens | 3,830 | 8,565 | +123.6292% |
| Context-input + output tokens | 1,064,762 | 510,390 | −52.0653% |
| Wall seconds | 833.014 | 978.039 | +17.4097% |
| Mean seconds per case | 41.6507 | 48.9020 | +17.4097% |

Both conditions returned the exact expected answer in 20/20 cases. The two sampled refusal/isolation checks also passed in each case. This is one synthetic task family, not evidence of universal accuracy, security or lower cost.

### Why older percentages differ

The original summary reports the arithmetic mean of paired percentage changes. Its average processed-token reduction is **46.5%**; average time increase is **17.1%**. The README now shows the changes in **campaign totals**, **52.1%** and **17.4%**, because these correspond directly to the displayed totals.

For a metric with baseline values `B_i` and state values `S_i`:

```text
Change in totals (%) = 100 × (sum(S_i) / sum(B_i) − 1)
Mean paired change (%) = mean(100 × (S_i / B_i − 1))
```

They weight cases differently. Neither is a new experiment. Positive time change means slower. Cached tokens occupy context but may have different prices, so processed tokens are not a bill.

Recheck the committed experiment:

```bash
python -m unittest -v test_benchmark_results.py
```

## 2. Historical local per-step context benchmark (version 0.5)

Sources: [raw results](results/context-local.json), [script](benchmark_context.py). Recorded on Linux x86_64, Python 3.12.13. Seven repetitions after one excluded warm-up; timings below are medians.

| Actions | Request bytes: transcript → state | Bytes saved | Local time: transcript → state | Time change |
|---|---:|---:|---:|---:|
| 10 | 69,427 → 62,910 | 9.39% | 0.28 → 7.12 ms | +2,481.3% |
| 50 | 1,572,069 → 299,602 | 80.94% | 5.63 → 38.75 ms | +587.9% |
| 100 | 6,210,894 → 595,507 | 90.41% | 20.77 → 80.15 ms | +286.0% |
| 200 | 24,691,044 → 1,187,607 | 95.19% | 78.55 → 166.67 ms | +112.2% |

The script drives the actual selection code using the minimal Hermes contract from the tests. It supplies fixed-size state and 1,000 filler characters per action. Request sizes include the additional state-update requests, plugin instructions and tool schemas. At 200 actions the native path constructs 402 requests versus 201 for transcript replay.

Time is summed over local operations only. The native path includes state update/validation, atomic checkpoint persistence, selection, and message serialization; the baseline measures serialization of the accumulated transcript. Native fixture overhead is included. Setup, synthetic external tool work, network, model generation and host/provider framing are excluded. The baseline does not apply ordinary Hermes' native compaction. This is not a complete Hermes-versus-Hermes latency comparison.

Percentages are calculated from unrounded values. The large short-run time ratios reflect a sub-millisecond baseline; compare absolute times as well. Raw samples and source-file SHA-256 hashes are committed so the measurement can be inspected. Local timings will vary between runs and machines.

```bash
python benchmark_context.py --repeats 7 --output results/context-local.json
```

This historical dataset did not measure provider tokens, inference time, billing or real-model task success. It was produced without an installed harness or configured model credential. Evidence mode's retrieval tradeoff was not benchmarked. Its two-generation protocol is no longer the version 0.6 execution path.

## 3. Live version 0.5 failure diagnostic

Source: [sanitized provider evidence and all completed cases](results/hermes-live-step-diagnostic.json). This was an unplanned early stop, not a completed 20-pair efficacy benchmark. Seven matched pairs and one additional unpaired state run completed. All completed failures are retained; the table uses only matched pairs.

| Matched-pair total | Vanilla Hermes | Two-generation state |
|---|---:|---:|
| Correct final answers | 7/7 | 0/7 |
| Uncached input tokens | 49,829 | 190,329 |
| Cache-read tokens | 39,936 | 235,520 |
| Cache-write tokens | 0 | 0 |
| Output tokens (includes reasoning) | 400 | 17,402 |
| Total processed tokens | 90,165 | 443,251 |
| Provider responses | 21 | 119 |
| Interaction wall seconds | 63.135 | 542.464 |
| Process wall seconds | 97.563 | 575.748 |

The state arm repeatedly executed `skill_state_update` without reading either required file and hit the 16-iteration cap. Splitting the state update and action into separate generations, then removing the tool continuation, was not a faithful implementation of the paper's one-response transition. These results diagnose that integration failure; they do not estimate the potential efficiency of a working implementation.

The diagnostic also exposed an accounting trap: Hermes issued a seventeenth terminal-summary response outside its session counters. The table includes that response from raw provider usage. Reporting only native session counters would undercount real consumption. The repaired bridge suppresses this fallback when no valid final transition exists.

Both arms reported subscription-included estimated cost of zero. This is neither a provider invoice nor evidence of zero economic cost. The interrupted campaign and earlier development pilots are not part of a complete experiment; the report labels its missing runs and unpaired case explicitly.

## 4. Live one-generation Hermes benchmark (version 0.6 candidate)

Source: [complete 20-pair report with per-call provider usage](results/hermes-live-step-20.json). All 40 runs completed. Windows, Python 3.11 in the installed Hermes environment, model `gpt-5.6-terra`, provider `openai-codex`, actual reasoning effort `low`. Hermes base revision: `63279301bcbdc185c1b07b98a9312eb0c862f26d`; Skill-state base revision: `1a8f56c42fe9ac890463710dd9637f4942f73171`. Both checkouts contained local modifications; the report records the measured plugin, bridge, conversation-loop and runner hashes. A base revision alone is not the measured implementation.

| Campaign total | Vanilla Hermes | One-generation state | Change |
|---|---:|---:|---:|
| Correct final answers | 20/20 | 20/20 | No observed failures |
| Uncached input tokens | 136,065 | 171,766 | +35,701 |
| Cache-read tokens | 122,880 | 74,240 | −48,640 |
| Cache-write tokens | 0 | 0 | 0 |
| Context-input tokens, including cache reads | 258,945 | 246,006 | −5.00% |
| Output tokens, including reasoning | 1,012 | 10,256 | +9,244 |
| Total processed tokens | 259,957 | 256,262 | **−1.42%** |
| Provider responses | 60 | 61 | +1 |
| Interaction wall seconds | 170.826 | 353.489 | +106.93% |
| Process wall seconds | 275.333 | 462.679 | +68.04% |

Reasoning tokens were 137 and 1,406, respectively, already included in output. Every recorded provider response reconciled; there were no provider errors, compression calls or uncounted terminal-summary calls. State seed 103 used four responses; its extra response remains included.

| Synthetic family | Exact answers, vanilla / state | Total tokens, vanilla → state | Change |
|---|---:|---:|---:|
| Corrected memory, ten parameter variations | 10/10 / 10/10 | 139,077 → 124,042 | −10.81% |
| Two-file tool chain, ten parameter variations | 10/10 / 10/10 | 120,880 → 132,220 | +9.38% |

**Conclusion:** the repaired loop works on these cases and slightly lowers aggregate token count, but the workload split matters. A perfect score on these two templates does not guarantee perfect future accuracy. Latency is reported as a secondary tradeoff; the optimization target is lower total resource consumption subject to preserved task accuracy, not faster answers at any cost.

**No demonstrated monetary or compute savings.** Both arms returned estimated cost `0` with `cost_status=included` and no pricing source. Uncached input and output increased while discounted cache reads decreased. With per-token prices `p_input`, `p_cache` and `p_output`, the measured billing difference would be `35,701*p_input - 48,640*p_cache + 9,244*p_output`; no prices were assumed. Token totals alone do not measure FLOPs or energy.

The campaign froze runtime source. [Candidate source commit b4354bd](https://github.com/Hikarea/skill-state/commit/b4354bd) retains the pre-hardening implementation. The runner and bridge byte hashes match the campaign; two plugin files were reconstructed after comment/import cleanup and their recorded raw byte hashes do not match the reconstructed files, so this is not a byte-perfect archive of every measured file. The later exact-offered-tool allowlist hardening is separately regression-tested and smoke-tested, not silently attributed to these measurements. That restriction was not exercised by this read-only workload. Development smoke seed 101 overlaps one campaign parameter variation; server-side caching was not reset. This is descriptive verification, not an unseen confirmatory study or a production certification. The next scientific gate is predeclared, varied long-horizon work with separate input/cache/output accounting and fixed exact-answer or executable-test graders.

Post-hardening functional checks, excluded from all campaign totals: an isolated two-file case (seed 123) returned the exact answer in three provider responses, with 12,964 total tokens and reconciled usage. A real CLI invocation using the installed plugin and ordinary user profile returned exactly `SKILL_STATE_INSTALLED_OK`, without a state marker. These prove the exercised paths, not all Desktop features, recovery or production readiness.

## Live benchmark method and reproduction

The real runner is [benchmark_live_hermes.py](benchmark_live_hermes.py); [summarize_live_benchmark.py](summarize_live_benchmark.py) reconciles every recorded provider response. It uses the installed NousResearch Hermes `AIAgent`, not a simulated engine. The audited transport currently supports `codex_responses` only.

- Same model/provider/settings and native `read_file` tool in both arms. Requested minimal reasoning resolved to low in the provider request.
- Fresh isolated profiles and workspaces per arm, with authentication inherited locally. Personal context, memory, background review, other plugins and MCP are disabled equally. This is not the full default Desktop configuration.
- Twenty paired parameter variations: ten corrected-memory cases and ten two-file tool chains. Ground truth is generated, never preloaded into state. Both families require retaining facts and applying a later correction; only exact final matches pass.
- Seed order is shuffled with 731; arm order is balanced within each family. Runs are serial, with identical 16-iteration and 120-second turn budgets. Worker timeout is 420 seconds.
- Provider input includes cached input. Reports separate uncached input, cache reads, cache writes and output; reasoning is a subset of output and is not added twice. All state output, retries and terminal responses count.
- Interaction time covers the conversation calls; process time additionally includes worker startup and shutdown. Provider caching is observed, not controlled. Server load and serial order remain confounders.
- Engine identity and selected provider payload are checked. Compression must remain unused for this short-workload comparison. Harness errors or unmatched provider attempts prevent publishing a clean comparison.
- Source revisions, local source hashes and per-request hashes are retained. Request hashes describe selected transport arguments, not exact HTTP wire bytes. Internal SDK retries are not separately instrumented. Raw local logs can contain private paths and are not publication artifacts.
- After the recorded campaign, the runner gained exact local source snapshots and a source-drift check before each worker. Future runs retain these snapshots under `source/`; review them for private host edits before publishing. This does not retroactively repair the recorded campaign's source-archive limitation.

```bash
# Run with the configured Hermes environment's Python; use fresh output paths.
python integrations/hermes_transition_bridge.py /path/to/hermes-agent
python benchmark_live_hermes.py --hermes-source /path/to/hermes-agent --hermes-home /path/to/hermes-home --pairs 20 --output /path/to/new-campaign
python summarize_live_benchmark.py /path/to/new-campaign --output /path/to/new-report.json
python -m unittest -v test_live_benchmark.py
```

These are two short synthetic templates below native compaction thresholds, not twenty independent real-world tasks. They cannot establish long-horizon advantage, production accuracy, evidence-mode performance, or monetary savings. A follow-up should predeclare varied long-horizon workloads and a separately priced provider before making those claims.

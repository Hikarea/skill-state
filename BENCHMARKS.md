# Benchmark results and definitions

There are two independent datasets. They must not be combined into one performance claim.

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

## 2. Local per-step context benchmark

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

**Unmeasured for the new plugin:** provider input/cache/output tokens, inference time, billing, and real-model task success. There was no installed harness, local model or configured model API credential in the measurement environment. The user's PC was not accessed. Evidence mode's retrieval tradeoff was not benchmarked by this strict-mode script.

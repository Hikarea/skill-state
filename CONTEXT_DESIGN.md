# Per-step context, version 0.6

Primary objective: minimize cumulative context-input and generated tokens at an acceptable task-success rate. Latency is a separately reported tradeoff, not a correctness gate. Billing must use provider prices and cache buckets, not raw input totals.

## Paper and implementation boundaries

The reference is [SKILL.state v3, section 3 and Algorithm 1](https://arxiv.org/html/2608.26263v3). The strict standalone runtime constructs procedure + schema + validated state + latest observation, receives a state delta and action in one model response, merges/validates/commits the state, then executes the action. Previous responses and reasoning are not replayed. Within-call reasoning is not forcibly reduced by the new Hermes worker.

The paper's constant-context claim is conditional on bounded procedure, schema, state, and observations. A fixed-size schema cannot losslessly encode arbitrarily growing task information. This implementation uses explicit UTF-8 byte limits; bytes are NOT claimed to be model tokens. State is bounded to 16 KiB. Standalone observation defaults to 16 KiB and assembled prompt to 64 KiB, including validation-retry feedback. Oversized strict observations stop execution rather than being silently truncated.

Native Hermes instructions, schemas for enabled tools, provider framing and any host-added material are additional overhead. The standalone assembled-prompt limit is checked before the harness call; it is not an exact limit on a hosted provider's complete serialized request. Native selection preserves system/developer messages unchanged. Configure the host's memory/skill injection separately if it duplicates execution state. This plugin does not erase host policy or impersonate system messages with archived text.

## Three execution paths

| Path | Context boundary | Extra state generation | Tool executor |
|---|---|---|---|
| Strict standalone runtime | Every transition | None: patch + action in one response | Workspace capabilities |
| Hermes `--mode step` with explicit host bridge | Every normal provider request; only current observation and state | None: patch + action in one response | Hermes, with native guards and approvals |
| Compatibility Hermes `--mode turn` | Between completed user turns | One checkpoint before final answer | Hermes |

Step mode exposes `skill_state_transition`, containing a revision-checked merge patch and either one enabled action with JSON arguments or a final answer. The explicit host bridge invokes `prepare_transition` on the normalized response before observers and native dispatch. The engine validates the entire envelope, commits state, then translates it into a native tool call or final answer. It does not execute tools or approve actions. Invalid envelopes retain the prior state and expose bounded correction feedback; retries consume the normal iteration budget. Parallel action batches are unsupported. This remains an integration, not an independent security boundary.

The version 0.5 two-generation adaptation failed the live transition gate by repeatedly saving state without acting. Version 0.6 removes that mismatch with Algorithm 1. Its bridge also prevents Hermes' extra terminal-summary generation when no valid final transition was produced. The original failure data is retained rather than presented as an efficacy estimate. One-response transport does not guarantee speed or savings: state output, schema and protocol overhead must all be counted.

## Optional evidence mode: proposed extension

The active execution state remains canonical. Each observed user/tool payload is also stored as exact text in a session-scoped SQLite archive, addressed by SHA-256. There are no background summarizer or embedding model calls. Oversized observations become a bounded excerpt with an explicit evidence id and truncation notice. The agent can request literal search or paginated exact reads. Retrieval results are new observations and count against the same bound.

This enables recovery when a detail was not recognized as relevant earlier, and supports historical questions without continuously replaying history. It does not guarantee the model will choose the right search or retain the right facts. Search is case-sensitive literal matching, not semantic search. Disk storage and search work can grow with history even while model context stays bounded. Evidence is data, never higher-priority instructions. Native image/audio inputs are explicitly blocked in step mode rather than silently converted into text. Use turn mode for visual/audio tasks until a modality-aware adapter is tested.

## Recovery

Standalone successful and failed action observations remain in a durable feedback record until the next committed transition consumes them. A preview never consumes feedback. A crash during an external effect still requires inspecting the effect and resolving the intent; no exactly-once external execution is claimed.

Native state, revision and latest observation persist atomically within a session. The step adapter does not yet journal a pending native action: a crash between state commit and Hermes recording/dispatching the action requires manual inspection of state and external effects before resuming. Do not assume automatic reconciliation or exactly-once execution. Revision checks protect against stale model patches, not concurrent processes writing the same session; keep one writer per session. Native session rotation/compression can start a new session id and is not a cross-session migration mechanism. Domain schemas must remain fixed within a session.

## Verification and remaining gate

`python -m unittest -v` checks invalid patches, nested null deletion, stable instructions, discarded reasoning, a 200-action single turn, recovery, strict overflow, scoped retrieval and an unanticipated-detail recovery case. When Hermes is unavailable, the host is stubbed; these are context-contract tests, not an end-to-end Hermes certification.

`python benchmark_context.py` retains the historical two-generation fixture for 10/50/100/200 scripted actions. It counts the additional update requests and local context-management work but does not model the version 0.6 transition transport. It makes no LLM calls and does not measure task accuracy, provider tokens, cost or end-to-end latency. See BENCHMARKS.md for the committed results and timing boundaries; use the live runner for the current integration.

The original 20-case campaign and version 0.5 local byte benchmark remain historical evidence, not version 0.6 measurements. The new live Hermes runner captures provider usage at the transport boundary, including calls omitted from native session counters, and compares strict step mode with ordinary context selection. See BENCHMARKS.md for exact outcomes and limitations. Long tool loops, real file-edit tasks, historical evidence retrieval and interrupted-session recovery still require separate end-to-end evaluation; two short synthetic templates cannot certify them.

# Per-step context, version 0.5

Primary objective: minimize cumulative context-input and generated tokens at an acceptable task-success rate. Latency is a separately reported tradeoff, not a correctness gate. Billing must use provider prices and cache buckets, not raw input totals.

## Paper and implementation boundaries

The reference is [SKILL.state v3, section 3 and Algorithm 1](https://arxiv.org/html/2608.26263v3). The strict standalone runtime constructs procedure + schema + validated state + latest observation, receives a state delta and action in one model response, merges/validates/commits the state, then executes the action. Previous responses and reasoning are not replayed. Within-call reasoning is not forcibly reduced by the new Hermes worker.

The paper's constant-context claim is conditional on bounded procedure, schema, state, and observations. A fixed-size schema cannot losslessly encode arbitrarily growing task information. This implementation uses explicit UTF-8 byte limits; bytes are NOT claimed to be model tokens. State is bounded to 16 KiB. Standalone observation defaults to 16 KiB and assembled prompt to 64 KiB, including validation-retry feedback. Oversized strict observations stop execution rather than being silently truncated.

Native Hermes instructions, schemas for enabled tools, provider framing and any host-added material are additional overhead. The standalone assembled-prompt limit is checked before the harness call; it is not an exact limit on a hosted provider's complete serialized request. Native selection preserves system/developer messages unchanged. Configure the host's memory/skill injection separately if it duplicates execution state. This plugin does not erase host policy or impersonate system messages with archived text.

## Three execution paths

| Path | Context boundary | Extra state generation | Tool executor |
|---|---|---|---|
| Strict standalone runtime | Every transition | None: patch + action in one response | Workspace capabilities |
| Native Hermes `--mode step` | Every provider request; only current observation and state | Usually one extra generation per external action | Hermes, with native guards and approvals |
| Compatibility Hermes `--mode turn` | Between completed user turns | One checkpoint before final answer | Hermes |

Native mode uses an internal `skill_state_update` tool and revision-checked merge patches. The `pre_tool_call` hook permits one external action after a valid state update; it does not execute tools or approve actions. Invalid updates retain state and expose a bounded correction message. The `pre_verify` hook requests a state update before a final answer when necessary. Mixed parallel state-update/action batches are unsupported; the protocol requests sequential calls. Hosts may limit verification continuations or fail open when plugin hooks fail, so this is not an independent security boundary.

The extra native generation is a measurable adaptation, not an exact reproduction of the paper's one-response transition transport. For short tasks, it can consume MORE tokens. Do not call native mode an efficiency improvement until total input + output tokens, including these updates, are measured on real tasks.

## Optional evidence mode: proposed extension

The active execution state remains canonical. Each observed user/tool payload is also stored as exact text in a session-scoped SQLite archive, addressed by SHA-256. There are no background summarizer or embedding model calls. Oversized observations become a bounded excerpt with an explicit evidence id and truncation notice. The agent can request literal search or paginated exact reads. Retrieval results are new observations and count against the same bound.

This enables recovery when a detail was not recognized as relevant earlier, and supports historical questions without continuously replaying history. It does not guarantee the model will choose the right search or retain the right facts. Search is case-sensitive literal matching, not semantic search. Disk storage and search work can grow with history even while model context stays bounded. Evidence is data, never higher-priority instructions. Multi-modal payloads are archived as serialized input structures; this implementation does not preserve native image/audio delivery semantics in step mode. Use turn mode for visual/audio tasks until a modality-aware adapter is tested.

## Recovery

Standalone successful and failed action observations remain in a durable feedback record until the next committed transition consumes them. A preview never consumes feedback. A crash during an external effect still requires inspecting the effect and resolving the intent; no exactly-once external execution is claimed.

Native state, revision and latest observation persist atomically within a session. Native Hermes remains responsible for its tool execution recovery. Revision checks protect against stale model patches, not concurrent processes writing the same session; keep one writer per session. Native session rotation/compression can start a new session id and is not a cross-session migration mechanism. Domain schemas must remain fixed within a session.

## Verification and remaining gate

`python -m unittest -v` checks invalid patches, nested null deletion, stable instructions, discarded reasoning, a 200-action single turn, recovery, strict overflow, scoped retrieval and an unanticipated-detail recovery case. When Hermes is unavailable, the host is stubbed; these are context-contract tests, not an end-to-end Hermes certification.

`python benchmark_context.py` measures serialized request bytes for 10/50/100/200 scripted actions, counts the additional update requests, and compares against transcript replay. It makes no LLM calls and does not measure task accuracy, provider tokens, cost or latency.

The existing 20-case campaign remains historical evidence for the old patched-CLI experiment, with the legacy adapter explicitly pinned. It does not validate these new modes. A real deployment gate must compare ordinary Hermes, strict standalone and native step mode on the same model/settings and real tasks: file edits with changing requirements, failed actions and corrections, task switches, long tool loops, historical evidence queries and interrupted sessions. Record all provider input/cache/output usage, task outcomes and latency separately. No new provider benchmark or live Hermes installation was performed in this change.

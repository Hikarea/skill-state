# Codex Desktop: capability boundary

The standalone `codex exec` adapter is not a replacement for the Desktop
conversation context. Do not describe a Desktop checkpoint hook as the paper's
state-only execution loop.

## Established behavior

The inspected legacy Desktop hook injected its full protocol and saved state
on both SessionStart and UserPromptSubmit. It asked for a complete checkpoint
on every answer. Its Stop handler could request another turn to correct an
escaped marker. It did not modify the request history or transcript.

[Official hook documentation](https://learn.chatgpt.com/docs/hooks) describes
`additionalContext` as added context and a blocking Stop result as a new
continuation prompt. Those outputs do not establish a history-replacement API.
Invisible Markdown checkpoint text still has input/output token overhead.

The local corrective policy is passive archival: no startup/prompt state
injection, no request to generate checkpoints, and no formatting retry turns.
An already-supplied valid checkpoint can still be saved; missing or invalid
markers must not overwrite saved state. This is archival, **not** automatic
state maintenance, restoration, compression, or a token-savings measurement.
Existing injected instructions remain in an open conversation until the host
stops including them. Changing a script cannot retract a sent request.

## Requirements for actual state-only execution

1. A supported request-construction boundary that selects execution context,
   rather than only appending hook text. Preserve system policy and tool guards.
2. A fixed domain procedure/schema, validated bounded state and admitted latest
   observation. Specify how omitted but subsequently needed information is
   recovered; schema validity alone does not establish semantic completeness.
3. One generation for a sparse state delta and action/final result; do not add a
   separate mandatory recap generation. Validate before commit and dispatch.
4. Revision checks, durable action intent and observation recovery across
   interruption. Never blindly replay an ambiguous external effect.
5. Contract tests at the request boundary showing superseded history is absent,
   current instructions remain, invalid transitions execute nothing, and
   restarts preserve committed state. Tests do not establish model accuracy.

If Desktop cannot expose requirement 1, use a separately controlled runtime
or implement a supported host integration; do not edit session databases or
delete history to simulate context replacement. The standalone runtime already
constructs fresh execution prompts, though hosted harness additions still need
inspection. Native Desktop integration remains unimplemented.

Performance campaigns are currently deferred. Existing benchmark results remain
historical and apply only to their recorded implementation. No comparative
claim about Graft, RTK or Caveman follows from this hook inspection.

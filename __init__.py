"""Standalone Hermes plugin entry point for SKILL.state."""

from .hermes_state_engine import ENGINE, state_prompt


def register(ctx):
    import os
    if os.environ.get("SKILL_STATE_PROPOSAL_WORKER") == "1":
        return
    from .hermes_step_engine import PerStepEngine, PROTOCOL, before_tool, before_final, settings
    if settings().get("mode", "turn") == "step":
        ctx.register_context_engine(PerStepEngine())
        ctx.register_system_prompt_section("skill-state.protocol", lambda _: PROTOCOL,
                                           position="after_memory", max_chars=2400)
        ctx.register_hook("pre_tool_call", before_tool)
        return
    ctx.register_context_engine(ENGINE)
    ctx.register_system_prompt_section(
        "skill-state.protocol",
        state_prompt,
        position="after_memory",
        max_chars=1800,
    )

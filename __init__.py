"""Standalone Hermes plugin entry point for SKILL.state."""

from .hermes_state_engine import ENGINE, state_prompt


def register(ctx):
    ctx.register_context_engine(ENGINE)
    ctx.register_system_prompt_section(
        "skill-state.protocol",
        state_prompt,
        position="after_memory",
        max_chars=1800,
    )

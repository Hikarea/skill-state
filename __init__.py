"""Standalone Hermes plugin entry point for SKILL.state."""

from .hermes_state_engine import ENGINE, checkpoint_tool, state_prompt


_CHECKPOINT_SCHEMA = {
    "name": "skill_state_checkpoint",
    "description": "Save the compact canonical SKILL.state for continuation. Call internally before the final user-facing reply.",
    "parameters": {
        "type": "object",
        "properties": {
            "objective": {"type": "string"},
            "status": {"type": "string", "enum": ["active", "done", "blocked"]},
            "completed": {"type": "array", "items": {"type": "string"}},
            "pending": {"type": "array", "items": {"type": "string"}},
            "facts": {"type": "array", "items": {"type": "string"}},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "next": {"type": "string"},
        },
        "required": ["objective", "status", "completed", "pending", "facts", "blockers", "next"],
        "additionalProperties": False,
    },
}


def register(ctx):
    ctx.register_context_engine(ENGINE)
    ctx.register_tool(
        name="skill_state_checkpoint",
        toolset="skill-state",
        schema=_CHECKPOINT_SCHEMA,
        handler=checkpoint_tool,
        description=_CHECKPOINT_SCHEMA["description"],
    )
    ctx.register_system_prompt_section(
        "skill-state.protocol",
        state_prompt,
        position="after_memory",
        max_chars=1800,
    )

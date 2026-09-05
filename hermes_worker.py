"""One fresh, tool-free proposal through the user's installed Hermes provider.

Run with the Python executable from Hermes' virtual environment. No Hermes files
are patched. Native provider authentication and mandatory host instructions remain.
"""

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--usage-file", type=Path)
    args = parser.parse_args()
    prompt = sys.stdin.read()
    agent = None
    # Prevent this plugin from wrapping its own already state-managed proposal.
    # Other host plugins and permissions are untouched.
    os.environ["SKILL_STATE_PROPOSAL_WORKER"] = "1"
    with contextlib.redirect_stdout(sys.stderr):
        try:
            from hermes_cli.config import load_config
            from hermes_cli.oneshot import _resolve_model_and_provider
            from hermes_cli.runtime_provider import resolve_runtime_provider
            from run_agent import AIAgent

            choice = _resolve_model_and_provider(load_config(), None, None)
            runtime = resolve_runtime_provider(
                requested=choice.provider, target_model=choice.model or None,
                explicit_base_url=choice.base_url, explicit_api_key=choice.api_key,
            )
            agent = AIAgent(
                model=choice.model, api_key=runtime.get("api_key"), base_url=runtime.get("base_url"),
                provider=runtime.get("provider"), requested_provider=runtime.get("requested_provider"),
                api_mode=runtime.get("api_mode"), credential_pool=runtime.get("credential_pool"),
                enabled_toolsets=[], max_iterations=1, quiet_mode=True,
                skip_context_files=True, skip_memory=True, skip_background_review=True,
                load_soul_identity=False, save_trajectories=False,
            )
            if agent.tools:
                raise RuntimeError("Hermes exposed tools to a proposal-only worker; refusing execution")
            result = agent.run_conversation(prompt, conversation_history=[])
            if result.get("failed") or result.get("interrupted"):
                raise RuntimeError("Hermes proposal failed or was interrupted")
            usage = dict(getattr(agent, "_last_turn_usage", None) or {})
            usage.update(model=choice.model, provider=runtime.get("provider"),
                         api_calls=result.get("api_calls", 0))
            if args.usage_file:
                args.usage_file.write_text(json.dumps(usage), encoding="utf-8")
            answer = result.get("final_response") or ""
        except ImportError as exc:
            raise RuntimeError("Use --hermes-python pointing to Hermes' installed Python environment") from exc
        finally:
            if agent is not None:
                agent.close()
    print(answer)


if __name__ == "__main__":
    main()

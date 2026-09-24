"""The set of tools offered to the model.

Empty in this phase, deliberately. The tools that read platform data arrive in
a later phase; until then the agent must decline rather than answer from the
model's own memory, which is what the system prompt enforces and what the
agent-loop tests will assert once the loop exists.

Tool adapters are written here, in this repository, by the team that owns the
agent -- not declared by each module. That was decided so the AI surface stays
in one replaceable place instead of being spread across every module's repo,
and it is why nothing external can put text in front of the model.
"""

from __future__ import annotations


def tool_specs() -> list[dict]:
    """The OpenAI-style tool schemas handed to the provider.

    Empty on purpose in this phase -- see the module docstring.
    """
    return []


def has_tools() -> bool:
    return bool(tool_specs())

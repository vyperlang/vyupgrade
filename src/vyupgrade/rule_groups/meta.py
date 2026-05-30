from __future__ import annotations

from ..rule_registry import Rule, crossing


RULES = (
    Rule("interface_split", changes=(crossing("VY120", (0, 4, 0)),)),
    Rule(
        "validation",
        changes=(
            crossing("VYD006", (0, 4, 0)),
            crossing("VYD007", (0, 4, 0)),
            crossing("VYD008", (0, 4, 0)),
            crossing("VYD009", (0, 4, 0)),
        ),
    ),
)

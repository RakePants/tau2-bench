"""
MA-V-Hard: Multi-Agent Executor + Verifier (Hard Verification).

Architecture: Executor + Verifier with sequential hard (blocking) verification.
- Executor: Full ReAct agent with tool access
- Verifier: Evaluation-only agent (approve/reject/suggest), no tools
- Hard mode: Verifier BLOCKS actions until approved. Max 5 attempts, then transfer to human.

Usage:
    from tau2.agent.mav_hard import MAVHardAgent

    agent = MAVHardAgent(tools=tools, domain_policy=policy, llm="gpt-4")
"""

from tau2.agent.mav_hard.multi_agent import (
    MAVHardAgent,
    MAVHardState,
    VerifierVerdict,
)

__all__ = [
    "MAVHardAgent",
    "MAVHardState",
    "VerifierVerdict",
]

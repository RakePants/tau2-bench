"""
MA-V-Soft: Multi-Agent Executor + Verifier (Soft Verification).

Architecture: Executor + Verifier with sequential soft verification.
- Executor: Full ReAct agent with tool access
- Verifier: Evaluation-only agent (approve/reject/suggest), no tools
- Soft mode: Verifier feedback is advisory, action always goes through after max 1 retry

Usage:
    from tau2.agent.mav_soft import MAVSoftAgent

    agent = MAVSoftAgent(tools=tools, domain_policy=policy, llm="gpt-4")
"""

from tau2.agent.mav_soft.multi_agent import (
    MAVSoftAgent,
    MAVSoftState,
    VerifierVerdict,
)

__all__ = [
    "MAVSoftAgent",
    "MAVSoftState",
    "VerifierVerdict",
]

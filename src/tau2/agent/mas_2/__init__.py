"""
MA-S-2: Multi-Agent Specialized Roles (2 agents + orchestrator).

Split:
- Infrastructure Agent: cellular service issues (no signal, SIM, suspension)
- Application Agent: data + MMS issues (internet, roaming, picture messaging)

Usage:
    from tau2.agent.mas_2 import MAS2Agent
    agent = MAS2Agent(tools=tools, domain_policy=policy, llm="gpt-4")
"""

from tau2.agent.mas_2.multi_agent import MAS2Agent, MAS2State

__all__ = ["MAS2Agent", "MAS2State"]

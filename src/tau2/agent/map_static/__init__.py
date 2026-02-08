"""
MA-P-Static: Multi-Agent Planner + Executor (Static Planning).

Usage:
    from tau2.agent.map_static import MAPStaticAgent
    agent = MAPStaticAgent(tools=tools, domain_policy=policy, llm="gpt-4")
"""

from tau2.agent.map_static.multi_agent import MAPStaticAgent, MAPStaticState

__all__ = ["MAPStaticAgent", "MAPStaticState"]

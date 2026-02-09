"""
Telecom Multi-Agent System Package.

This package provides a multi-agent implementation for the telecom domain benchmark.

Mas3Agent:
- Uses LLM-based routing with FULL conversation context
- Routes to specialized sub-agents based on issue type
- Supports three issue types: service_issue, mobile_data_issue, mms_issue
- ALL agents receive the complete base policy (main_policy.md + device actions)
- Each agent also receives their specialized troubleshooting guide

Usage:
    from tau2.agent.mas_3 import Mas3Agent
    
    agent = Mas3Agent(tools=tools, domain_policy=policy, llm="gpt-4")
"""

from tau2.agent.mas_3.multi_agent import (
    Mas3Agent,
    MultiAgentState,
    IssueType,
)

__all__ = [
    "Mas3Agent",
    "MultiAgentState",
    "IssueType",
]

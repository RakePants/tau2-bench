"""
Telecom Multi-Agent System Package.

This package provides a multi-agent implementation for the telecom domain benchmark.

PaperMultiAgent:
- Uses LLM-based routing with FULL conversation context
- Routes to specialized sub-agents based on issue type
- Supports three issue types: service_issue, mobile_data_issue, mms_issue
- ALL agents receive the complete base policy (main_policy.md + device actions)
- Each agent also receives their specialized troubleshooting guide

Usage:
    from tau2.agent.paper_multi_agent import PaperMultiAgent
    
    agent = PaperMultiAgent(tools=tools, domain_policy=policy, llm="gpt-4")
"""

from tau2.agent.mas_3.multi_agent import (
    PaperMultiAgent,
    MultiAgentState,
    IssueType,
)

__all__ = [
    "PaperMultiAgent",
    "MultiAgentState",
    "IssueType",
]

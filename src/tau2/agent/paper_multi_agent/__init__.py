"""
Telecom Multi-Agent System Package.

This package provides multi-agent implementations for the telecom domain benchmark.
Two implementations are available:

1. PaperMultiAgent: Uses LLM-based routing to determine issue type
2. TelecomHeuristicMultiAgent: Uses keyword-based heuristics for routing (faster, no extra LLM calls)

Both implementations:
- Inherit from LocalAgent for benchmark compatibility
- Route to specialized sub-agents based on issue type
- Support three issue types: service_issue, mobile_data_issue, mms_issue

Usage:
    from tau2.agent.paper_multi_agent import PaperMultiAgent, TelecomHeuristicMultiAgent
    
    # LLM-based routing
    agent = PaperMultiAgent(tools=tools, domain_policy=policy, llm="gpt-4")
    
    # Keyword-based routing (faster)
    agent = TelecomHeuristicMultiAgent(tools=tools, domain_policy=policy, llm="gpt-4")
"""

from tau2.agent.paper_multi_agent.multi_agent import (
    PaperMultiAgent,
    MultiAgentState,
    IssueType,
)

__all__ = [
    # Main implementations
    "PaperMultiAgent",
    # State classes
    "MultiAgentState",
    # Utilities
    "IssueType",
]

"""
Multi-Agent System for Telecom Domain.

This module implements a multi-agent architecture where:
1. A RouterAgent determines the issue type from the initial user message
2. Specialized agents (Service, MobileData, MMS) handle their respective issues
3. All agents share the same tools but have different policies/instructions
"""

from copy import deepcopy
from enum import Enum
from typing import List, Optional

from loguru import logger
from pydantic import BaseModel, Field

from tau2.agent.base import (
    LocalAgent,
    ValidAgentInputMessage,
    is_valid_agent_history_message,
)
from tau2.data_model.message import (
    APICompatibleMessage,
    AssistantMessage,
    Message,
    MultiToolMessage,
    SystemMessage,
)
from tau2.environment.tool import Tool
from tau2.utils.llm_utils import generate

from tau2.agent.paper_multi_agent.instructions import (
    BASE_POLICY,
    USER_DEVICE_CAPABILITIES,
    ROUTER_SYSTEM_PROMPT,
    SERVICE_ISSUE_POLICY,
    MOBILE_DATA_ISSUE_POLICY,
    MMS_ISSUE_POLICY,
)


class IssueType(str, Enum):
    """Types of issues that can be handled by specialized agents."""
    SERVICE_ISSUE = "service_issue"
    MOBILE_DATA_ISSUE = "mobile_data_issue"
    MMS_ISSUE = "mms_issue"
    UNKNOWN = "unknown"


class MultiAgentState(BaseModel):
    """State for the multi-agent system."""
    
    system_messages: list[SystemMessage] = Field(default_factory=list)
    messages: list[APICompatibleMessage] = Field(default_factory=list)
    current_agent: Optional[IssueType] = Field(default=None)
    is_routed: bool = Field(default=False)


# Agent instruction template
AGENT_INSTRUCTION = """
You are a specialized customer service agent for telecom technical support.
In each turn you can either:
- Send a message to the user.
- Make a tool call.
You cannot do both at the same time.

Try to be helpful and always follow the policy. Always make sure you generate valid JSON only.
""".strip()


def build_system_prompt(base_policy: str, specialized_policy: str) -> str:
    """Build the complete system prompt for an agent."""
    return f"""<instructions>
{AGENT_INSTRUCTION}
</instructions>

<base_policy>
{base_policy}
</base_policy>

<specialized_policy>
{specialized_policy}
</specialized_policy>

<user_device_capabilities>
{USER_DEVICE_CAPABILITIES}
</user_device_capabilities>
""".strip()


class PaperMultiAgent(LocalAgent[MultiAgentState]):
    """
    Multi-agent system for telecom customer service.
    
    This agent uses a router to determine the issue type and delegates
    to specialized sub-agents based on the classification.
    
    Specialized agents:
    - ServiceIssueAgent: Handles no service/connectivity issues
    - MobileDataIssueAgent: Handles mobile data/slow internet issues  
    - MMSIssueAgent: Handles MMS/picture messaging issues
    """
    
    def __init__(
        self,
        tools: List[Tool],
        domain_policy: str,  # Original domain policy (not used directly, we use specialized)
        llm: Optional[str] = None,
        llm_args: Optional[dict] = None,
    ):
        """Initialize the multi-agent system."""
        super().__init__(tools=tools, domain_policy=domain_policy)
        self.llm = llm
        self.llm_args = deepcopy(llm_args) if llm_args is not None else {}
        
        # Build system prompts for each specialized agent
        self._agent_prompts = {
            IssueType.SERVICE_ISSUE: build_system_prompt(BASE_POLICY, SERVICE_ISSUE_POLICY),
            IssueType.MOBILE_DATA_ISSUE: build_system_prompt(BASE_POLICY, MOBILE_DATA_ISSUE_POLICY),
            IssueType.MMS_ISSUE: build_system_prompt(BASE_POLICY, MMS_ISSUE_POLICY),
        }
    
    def _route_to_agent(self, user_message: str) -> IssueType:
        """
        Use the router to determine which specialized agent should handle the issue.
        
        Args:
            user_message: The initial user message describing their issue.
            
        Returns:
            The issue type classification.
        """
        router_messages = [
            SystemMessage(role="system", content=ROUTER_SYSTEM_PROMPT),
            {"role": "user", "content": f"User's issue: {user_message}\n\nClassify this as one of: service_issue, mobile_data_issue, mms_issue"}
        ]
        
        try:
            # Use a simple completion without tools for routing
            response = generate(
                model=self.llm,
                tools=[],  # No tools for router
                messages=router_messages,
                **self.llm_args,
            )
            
            # Parse the response to get issue type
            response_text = response.content.strip().lower() if response.content else ""
            
            if "mms_issue" in response_text or "mms" in response_text:
                return IssueType.MMS_ISSUE
            elif "mobile_data_issue" in response_text or "mobile_data" in response_text or "data" in response_text:
                return IssueType.MOBILE_DATA_ISSUE
            elif "service_issue" in response_text or "service" in response_text:
                return IssueType.SERVICE_ISSUE
            else:
                # Default to service_issue for unclear cases
                logger.warning(f"Router returned unclear response: {response_text}, defaulting to service_issue")
                return IssueType.SERVICE_ISSUE
                
        except Exception as e:
            logger.error(f"Router failed: {e}, defaulting to service_issue")
            return IssueType.SERVICE_ISSUE
    
    def _get_system_prompt_for_agent(self, issue_type: IssueType) -> str:
        """Get the system prompt for a specialized agent."""
        return self._agent_prompts.get(issue_type, self._agent_prompts[IssueType.SERVICE_ISSUE])
    
    def get_init_state(
        self, message_history: Optional[list[Message]] = None
    ) -> MultiAgentState:
        """
        Get the initial state of the multi-agent system.
        
        Args:
            message_history: Optional message history to restore from.
            
        Returns:
            Initial state with empty messages and no routing.
        """
        if message_history is None:
            message_history = []
        
        assert all(is_valid_agent_history_message(m) for m in message_history), (
            "Message history must contain only AssistantMessage, UserMessage, or ToolMessage to Agent."
        )
        
        return MultiAgentState(
            system_messages=[],  # Will be set after routing
            messages=message_history,
            current_agent=None,
            is_routed=False,
        )
    
    def generate_next_message(
        self, message: ValidAgentInputMessage, state: MultiAgentState
    ) -> tuple[AssistantMessage, MultiAgentState]:
        """
        Generate the next message from the multi-agent system.
        
        On first user message, routes to the appropriate specialized agent.
        Subsequent messages are handled by the routed agent.
        
        Args:
            message: User message or tool response.
            state: Current agent state.
            
        Returns:
            Tuple of (assistant message, updated state).
        """
        # Add message to history
        if isinstance(message, MultiToolMessage):
            state.messages.extend(message.tool_messages)
        else:
            state.messages.append(message)
        
        # Route on first user message
        if not state.is_routed:
            # Extract user message content for routing
            if hasattr(message, 'content') and message.content:
                user_content = message.content
            else:
                user_content = str(message)
            
            # Route to specialized agent
            issue_type = self._route_to_agent(user_content)
            state.current_agent = issue_type
            state.is_routed = True
            
            # Set system prompt for the selected agent
            system_prompt = self._get_system_prompt_for_agent(issue_type)
            state.system_messages = [SystemMessage(role="system", content=system_prompt)]
            
            logger.info(f"Routed to {issue_type.value} agent")
        
        # Generate response using the specialized agent
        messages = state.system_messages + state.messages
        
        assistant_message = generate(
            model=self.llm,
            tools=self.tools,
            messages=messages,
            **self.llm_args,
        )
        
        state.messages.append(assistant_message)
        return assistant_message, state
    
    def set_seed(self, seed: int):
        """Set the seed for the LLM."""
        if self.llm is None:
            raise ValueError("LLM is not set")
        cur_seed = self.llm_args.get("seed", None)
        if cur_seed is not None:
            logger.warning(f"Seed is already set to {cur_seed}, resetting it to {seed}")
        self.llm_args["seed"] = seed

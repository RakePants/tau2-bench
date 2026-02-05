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
from tau2.agent.mas_3.instructions import (
    MMS_ISSUE_AGENT_IDENTITY,
    MMS_ISSUE_POLICY,
    MOBILE_DATA_ISSUE_AGENT_IDENTITY,
    MOBILE_DATA_ISSUE_POLICY,
    SERVICE_ISSUE_AGENT_IDENTITY,
    SERVICE_ISSUE_POLICY,
    create_router_user_message,
    get_base_policy,
    get_router_system_prompt,
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


class IssueType(str, Enum):
    """Types of issues that can be handled by specialized agents."""

    SERVICE_ISSUE = "service_issue"
    MOBILE_DATA_ISSUE = "mobile_data_issue"
    MMS_ISSUE = "mms_issue"


agent_identities = {
    IssueType.SERVICE_ISSUE: SERVICE_ISSUE_AGENT_IDENTITY,
    IssueType.MOBILE_DATA_ISSUE: MOBILE_DATA_ISSUE_AGENT_IDENTITY,
    IssueType.MMS_ISSUE: MMS_ISSUE_AGENT_IDENTITY,
}

agent_specialized_policies = {
    IssueType.SERVICE_ISSUE: SERVICE_ISSUE_POLICY,
    IssueType.MOBILE_DATA_ISSUE: MOBILE_DATA_ISSUE_POLICY,
    IssueType.MMS_ISSUE: MMS_ISSUE_POLICY,
}


class MultiAgentState(BaseModel):
    """State for the multi-agent system."""

    system_messages: list[SystemMessage] = Field(default_factory=list)
    messages: list[APICompatibleMessage] = Field(default_factory=list)
    current_agent: Optional[IssueType] = Field(default=None)
    is_routed: bool = Field(default=False)


# Agent instruction template
AGENT_INSTRUCTION = """
You are a specialized support agent.
In each turn you can either:
- Send a message to the user.
- Make a tool call.
You cannot do both at the same time.

Try to be helpful and always follow the policy. Always make sure you generate valid JSON only.
""".strip()


def build_system_prompt(issue_type: IssueType) -> str:
    base_policy = get_base_policy()
    specialized_policy = agent_specialized_policies.get(issue_type)
    agent_identity = agent_identities.get(issue_type)

    return f"""<instructions>
{AGENT_INSTRUCTION}
</instructions>

<agent_identity>
{agent_identity}
</agent_identity>

<policy>
{base_policy}
</policy>

<specialized_troubleshooting_guide>
{specialized_policy}
</specialized_troubleshooting_guide>
""".strip()


class PaperMultiAgent(LocalAgent[MultiAgentState]):
    """
    Multi-agent system for telecom customer service.

    This agent uses a router to determine the issue type and delegates
    to specialized sub-agents based on the classification.

    The router analyzes the FULL conversation history to make accurate decisions.

    Specialized agents:
    - ServiceIssueAgent: Handles no service/connectivity issues
    - MobileDataIssueAgent: Handles mobile data/slow internet issues
    - MMSIssueAgent: Handles MMS/picture messaging issues

    ALL agents receive:
    - Complete main_policy.md (domain basics, business procedures)
    - Device actions reference (what users can do)
    - Their specialized troubleshooting guide
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
        # Each gets: base_policy (full) + their specialized troubleshooting guide
        self._agent_prompts = {
            IssueType.SERVICE_ISSUE: build_system_prompt(
                issue_type=IssueType.SERVICE_ISSUE
            ),
            IssueType.MOBILE_DATA_ISSUE: build_system_prompt(
                issue_type=IssueType.MOBILE_DATA_ISSUE
            ),
            IssueType.MMS_ISSUE: build_system_prompt(issue_type=IssueType.MMS_ISSUE),
        }

    def _route_to_agent(self, conversation_history: list) -> IssueType:
        """
        Use the router to determine which specialized agent should handle the issue.

        The router sees the FULL conversation history for context-aware routing.

        Args:
            conversation_history: All messages in the conversation so far.

        Returns:
            The issue type classification.
        """
        # Format conversation for router
        router_user_message = create_router_user_message(conversation_history)

        router_messages = [
            {"role": "system", "content": get_router_system_prompt()},
            {"role": "user", "content": router_user_message},
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

            # Strict matching - router should return exact values
            if response_text == "mms_issue":
                return IssueType.MMS_ISSUE
            elif response_text == "mobile_data_issue":
                return IssueType.MOBILE_DATA_ISSUE
            elif response_text == "service_issue":
                return IssueType.SERVICE_ISSUE

            # Fallback: check for partial matches
            if "mms" in response_text:
                return IssueType.MMS_ISSUE
            elif "mobile_data" in response_text or "data_issue" in response_text:
                return IssueType.MOBILE_DATA_ISSUE
            elif "service" in response_text:
                return IssueType.SERVICE_ISSUE
            else:
                # Default to service_issue for unclear cases
                logger.warning(
                    f"Router returned unclear response: '{response_text}', defaulting to service_issue"
                )
                return IssueType.SERVICE_ISSUE

        except Exception as e:
            logger.error(f"Router failed: {e}, defaulting to service_issue")
            return IssueType.SERVICE_ISSUE

    def _get_system_prompt_for_agent(self, issue_type: IssueType) -> str:
        """Get the system prompt for a specialized agent."""
        return self._agent_prompts.get(
            issue_type, self._agent_prompts[IssueType.SERVICE_ISSUE]
        )

    def _convert_messages_for_router(self, messages: list) -> list:
        """
        Convert message objects to simple dicts for the router.

        Args:
            messages: List of message objects from state.

        Returns:
            List of dicts with 'role' and 'content' keys.
        """
        result = []
        for msg in messages:
            if hasattr(msg, "role") and hasattr(msg, "content"):
                # Handle regular messages
                if msg.content:  # Skip empty content
                    result.append(
                        {
                            "role": msg.role,
                            "content": msg.content
                            if isinstance(msg.content, str)
                            else str(msg.content),
                        }
                    )
            elif hasattr(msg, "tool_call_id"):
                # Handle tool messages - include tool results for context
                content = getattr(msg, "content", "") or getattr(msg, "result", "")
                if content:
                    result.append(
                        {
                            "role": "tool_result",
                            "content": str(content)[:500],  # Truncate long tool results
                        }
                    )
        return result

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

        Routes on EVERY user message to handle ambiguous cases.

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

        # Route on every user message (not tool responses)
        is_user_message = hasattr(message, "role") and message.role == "user"

        if is_user_message:
            # Convert messages for router (full conversation context)
            conversation_for_router = self._convert_messages_for_router(state.messages)

            # Route to specialized agent based on full conversation
            issue_type = self._route_to_agent(conversation_for_router)

            # Log if agent changed
            if state.current_agent is not None and state.current_agent != issue_type:
                logger.debug(
                    f"Re-routed from {state.current_agent.value} to {issue_type.value}"
                )
            else:
                logger.debug(f"Routed to {issue_type.value} agent")

            state.current_agent = issue_type
            state.is_routed = True

            # Set system prompt for the selected agent
            system_prompt = self._get_system_prompt_for_agent(issue_type)
            state.system_messages = [
                SystemMessage(role="system", content=system_prompt)
            ]

        # Generate response using the specialized agent
        messages = state.system_messages + state.messages

        assistant_message = generate(
            model=self.llm,
            tools=self.tools,
            messages=messages,
            **self.llm_args,
        )

        # Add marker for what agent worked on this turn
        if assistant_message.metadata is None:
            assistant_message.metadata = {}
        assistant_message.metadata["generated_by_agent"] = state.current_agent.value

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

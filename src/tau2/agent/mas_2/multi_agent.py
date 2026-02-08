"""
MA-S-2: Multi-Agent Specialized Roles (2 agents + orchestrator).

Architecture:
    - Orchestrator/Router: Classifies issue type, routes to specialist
    - Infrastructure Agent: Handles cellular service issues (no signal, SIM, suspension)
    - Application Agent: Handles data + MMS issues (internet, roaming, picture messaging)

Coordination: Hierarchical (via Router)
    User → Router → [infrastructure_issue] → InfrastructureAgent → Environment
                  → [application_issue]    → ApplicationAgent    → Environment

The split follows the dependency hierarchy:
    - Infrastructure = physical layer (cellular service)
    - Application = logical layer (data + MMS, which both require cellular service)

All agents receive the COMPLETE base policy. Specialized agents receive
additional troubleshooting guides relevant to their domain.
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
from tau2.agent.mas_2.instructions import (
    MMS_ISSUE_POLICY,
    MOBILE_DATA_ISSUE_POLICY,
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
    UserMessage,
)
from tau2.environment.tool import Tool
from tau2.utils.llm_utils import generate


class IssueType(str, Enum):
    """Issue categories for 2-agent routing."""

    INFRASTRUCTURE_ISSUE = "infrastructure_issue"
    APPLICATION_ISSUE = "application_issue"


class MAS2State(BaseModel):
    """State for the MA-S-2 multi-agent system."""

    system_messages: list[SystemMessage] = Field(default_factory=list)
    messages: list[APICompatibleMessage] = Field(default_factory=list)
    current_agent: Optional[IssueType] = Field(default=None)
    is_routed: bool = Field(default=False)


# ---------------------------------------------------------------------------
# Agent identities and system prompt construction
# ---------------------------------------------------------------------------

INFRASTRUCTURE_AGENT_IDENTITY = """
You are an INFRASTRUCTURE specialist for telecom technical support.

Your PRIMARY expertise is helping customers who have NO cellular service at all:
- Phone shows "No Service" or "No Signal"
- Cannot make or receive calls
- Cannot send or receive SMS text messages
- SIM card problems (missing, locked)
- Line suspension issues (often due to overdue bills)
- Airplane mode problems
- APN settings issues affecting service

You also have complete knowledge of:
- All business operations (billing, suspensions, plan changes, data refueling, roaming)
- Mobile data troubleshooting (for when service works but internet doesn't)
- MMS troubleshooting (for picture/video messaging issues)

Start with your primary specialty (cellular service issues), but use the other
troubleshooting guides if the user's actual problem differs from the initial classification.
""".strip()

APPLICATION_AGENT_IDENTITY = """
You are an APPLICATION SERVICES specialist for telecom technical support.

Your PRIMARY expertise is helping customers whose mobile DATA/internet or MMS
(picture/video messaging) is not working:

Mobile Data issues:
- User can make calls but cannot browse the internet
- Slow mobile data speeds
- Data roaming issues when traveling abroad
- Data plan limits reached (may need refueling or plan change)
- Data saver mode affecting speeds
- VPN connection issues
- Network mode preferences (2G/3G/4G/5G)

MMS issues:
- Cannot send pictures/photos via text
- Cannot receive picture messages
- Group text messages not working
- Video messages failing
- MMSC URL configuration issues
- Wi-Fi Calling interference with MMS
- Messaging app permissions

IMPORTANT: Both mobile data and MMS require cellular service. Always verify
the user has basic service first before troubleshooting data or MMS.

You also have complete knowledge of:
- All business operations (billing, suspensions, plan changes, data refueling, roaming)
- Cellular service troubleshooting (prerequisite for data and MMS)

Start with verifying prerequisites (service), then apply data or MMS-specific
troubleshooting. Use the service troubleshooting guide if needed.
""".strip()


AGENT_INSTRUCTION = """
You are a specialized support agent.
In each turn you can either:
- Send a message to the user.
- Make a tool call.
You cannot do both at the same time.

Try to be helpful and always follow the policy. Always make sure you generate valid JSON only.
""".strip()


def build_system_prompt(issue_type: IssueType) -> str:
    """Build system prompt for a specialized agent."""
    base_policy = get_base_policy()

    if issue_type == IssueType.INFRASTRUCTURE_ISSUE:
        agent_identity = INFRASTRUCTURE_AGENT_IDENTITY
        # Infrastructure agent gets the service troubleshooting guide
        specialized_policy = SERVICE_ISSUE_POLICY
    else:
        agent_identity = APPLICATION_AGENT_IDENTITY
        # Application agent gets BOTH data and MMS troubleshooting guides
        specialized_policy = MOBILE_DATA_ISSUE_POLICY + "\n\n" + MMS_ISSUE_POLICY

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


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class MAS2Agent(LocalAgent[MAS2State]):
    """
    Multi-Agent Specialized Roles (2 agents) system.

    Architecture: Orchestrator + 2 Specialized Agents
    - Router classifies into infrastructure_issue or application_issue
    - Infrastructure Agent: service issues (no signal, SIM, suspension)
    - Application Agent: data + MMS issues (internet, roaming, messaging)

    Routes on every user message. All agents have full base policy.
    """

    def __init__(
        self,
        tools: List[Tool],
        domain_policy: str,
        llm: Optional[str] = None,
        llm_args: Optional[dict] = None,
    ):
        """Initialize the MA-S-2 system."""
        super().__init__(tools=tools, domain_policy=domain_policy)
        self.llm = llm
        self.llm_args = deepcopy(llm_args) if llm_args is not None else {}

        self._agent_prompts = {
            IssueType.INFRASTRUCTURE_ISSUE: build_system_prompt(
                IssueType.INFRASTRUCTURE_ISSUE
            ),
            IssueType.APPLICATION_ISSUE: build_system_prompt(
                IssueType.APPLICATION_ISSUE
            ),
        }

    def _route_to_agent(self, conversation_history: list) -> IssueType:
        """
        Use the router to classify into 2 categories.
        """
        router_user_message = create_router_user_message(conversation_history)

        router_messages = [
            SystemMessage(role="system", content=get_router_system_prompt()),
            UserMessage(role="user", content=router_user_message),
        ]

        try:
            response = generate(
                model=self.llm,
                tools=[],
                messages=router_messages,
                **self.llm_args,
            )

            response_text = response.content.strip().lower() if response.content else ""

            # Strict matching
            if response_text == "infrastructure_issue":
                return IssueType.INFRASTRUCTURE_ISSUE
            elif response_text == "application_issue":
                return IssueType.APPLICATION_ISSUE

            # Fallback: partial matches
            if "infrastructure" in response_text:
                return IssueType.INFRASTRUCTURE_ISSUE
            elif "application" in response_text:
                return IssueType.APPLICATION_ISSUE

            # Secondary fallback: try to match original 3-category names
            if "service_issue" in response_text or "service" in response_text:
                return IssueType.INFRASTRUCTURE_ISSUE
            elif (
                "mobile_data" in response_text
                or "mms" in response_text
                or "data" in response_text
            ):
                return IssueType.APPLICATION_ISSUE

            logger.warning(
                f"Router returned unclear response: '{response_text}', "
                f"defaulting to infrastructure_issue"
            )
            return IssueType.INFRASTRUCTURE_ISSUE

        except Exception as e:
            logger.error(f"Router failed: {e}, defaulting to infrastructure_issue")
            return IssueType.INFRASTRUCTURE_ISSUE

    def _convert_messages_for_router(self, messages: list) -> list:
        """Convert message objects to simple dicts for the router."""
        result = []
        for msg in messages:
            if hasattr(msg, "role") and hasattr(msg, "content"):
                if msg.content:
                    result.append(
                        {
                            "role": msg.role,
                            "content": msg.content
                            if isinstance(msg.content, str)
                            else str(msg.content),
                        }
                    )
            elif hasattr(msg, "tool_call_id"):
                content = getattr(msg, "content", "") or getattr(msg, "result", "")
                if content:
                    result.append(
                        {
                            "role": "tool_result",
                            "content": str(content)[:500],
                        }
                    )
        return result

    def get_init_state(
        self, message_history: Optional[list[Message]] = None
    ) -> MAS2State:
        """Get the initial state."""
        if message_history is None:
            message_history = []

        assert all(is_valid_agent_history_message(m) for m in message_history), (
            "Message history must contain only AssistantMessage, UserMessage, or ToolMessage to Agent."
        )

        return MAS2State(
            system_messages=[],
            messages=message_history,
            current_agent=None,
            is_routed=False,
        )

    def generate_next_message(
        self, message: ValidAgentInputMessage, state: MAS2State
    ) -> tuple[AssistantMessage, MAS2State]:
        """
        Generate the next message. Routes on every user message.
        """
        # Add message to history
        if isinstance(message, MultiToolMessage):
            state.messages.extend(message.tool_messages)
        else:
            state.messages.append(message)

        # Route on every user message
        is_user_message = hasattr(message, "role") and message.role == "user"

        if is_user_message:
            conversation_for_router = self._convert_messages_for_router(state.messages)
            issue_type = self._route_to_agent(conversation_for_router)

            if state.current_agent is not None and state.current_agent != issue_type:
                logger.debug(
                    f"Re-routed from {state.current_agent.value} to {issue_type.value}"
                )
            else:
                logger.debug(f"Routed to {issue_type.value} agent")

            state.current_agent = issue_type
            state.is_routed = True

            system_prompt = self._agent_prompts.get(
                issue_type,
                self._agent_prompts[IssueType.INFRASTRUCTURE_ISSUE],
            )
            state.system_messages = [
                SystemMessage(role="system", content=system_prompt)
            ]

        # Generate response
        messages = state.system_messages + state.messages

        assistant_message = generate(
            model=self.llm,
            tools=self.tools,
            messages=messages,
            **self.llm_args,
        )

        # Safety net for empty responses
        if not assistant_message.content and not (
            assistant_message.tool_calls and len(assistant_message.tool_calls) > 0
        ):
            logger.warning("Agent produced empty message. Falling back.")
            assistant_message = AssistantMessage(
                role="assistant",
                content="I apologize, could you please repeat your question or provide more details?",
                cost=assistant_message.cost,
                usage=assistant_message.usage,
                metadata={},
            )

        # Add metadata
        if assistant_message.metadata is None:
            assistant_message.metadata = {}
        assistant_message.metadata["generated_by_agent"] = (
            state.current_agent.value if state.current_agent else "unrouted"
        )
        assistant_message.metadata["generated_by"] = "mas_2"

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

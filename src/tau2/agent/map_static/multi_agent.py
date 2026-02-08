"""
MA-P-Static: Multi-Agent Planner + Executor (Static Planning).

Architecture:
    - Planner: Generates a full plan once from the first user message. No tools.
    - Executor: Follows the plan, has full tool access + full domain policy.

Coordination:
    User → Planner → [plan] → Executor → Environment

Static mode: The plan is generated ONCE after the first user message and
never revised. The Executor follows it for the rest of the conversation.
"""

from copy import deepcopy
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
    UserMessage,
)
from tau2.environment.tool import Tool
from tau2.utils.llm_utils import generate

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

PLANNER_SYSTEM_PROMPT = """
<instructions>
You are a Planner agent for a customer service system. Your role is to analyze
the customer's request and create a step-by-step plan for the Executor agent
to follow.

You do NOT have access to any tools. You only generate plans.

Given the customer's message, produce a numbered plan. Each step should be
a clear, actionable instruction. Use a mix of:
- Tool action hints: mention which tool to use when relevant
  (e.g. "Look up the customer using get_customer_by_phone with their phone number")
- Communication hints: describe what to tell the user
  (e.g. "Inform the user that roaming has been enabled and ask them to restart their device")
- Conditional logic where needed
  (e.g. "If data usage is over the limit, offer a data refueling package")

Keep the plan concise but complete. Number each step.

Available tools for reference (the Executor has access to these):
{tool_descriptions}
</instructions>
<policy>
{domain_policy}
</policy>
""".strip()

PLANNER_USER_PROMPT = """
The customer says:
"{user_message}"

Create a step-by-step plan to resolve this customer's issue. Number each step.
""".strip()

EXECUTOR_SYSTEM_PROMPT = """
<instructions>
You are a customer service agent (Executor) that helps the user according to the
<policy> and <plan> provided below.

In each turn you can either:
- Send a message to the user.
- Make a tool call.
You cannot do both at the same time.

Follow the plan step by step. The plan was created by a Planner agent to guide
your actions. Use your judgment to adapt if a step doesn't apply or if the
situation requires deviation, but generally stick to the plan.

Try to be helpful and always follow the policy. Always make sure you generate valid JSON only.
</instructions>
<policy>
{domain_policy}
</policy>
<plan>
{plan}
</plan>
""".strip()

EXECUTOR_SYSTEM_PROMPT_NO_PLAN = """
<instructions>
You are a customer service agent (Executor) that helps the user according to the <policy> provided below.
In each turn you can either:
- Send a message to the user.
- Make a tool call.
You cannot do both at the same time.

A plan has not been generated yet. Respond naturally to the user while the
system prepares a plan for you.

Try to be helpful and always follow the policy. Always make sure you generate valid JSON only.
</instructions>
<policy>
{domain_policy}
</policy>
""".strip()


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class MAPStaticState(BaseModel):
    """State for the MA-P-Static multi-agent system."""

    executor_system_messages: list[SystemMessage] = Field(default_factory=list)
    messages: list[APICompatibleMessage] = Field(default_factory=list)
    plan: Optional[str] = Field(default=None, description="The generated plan")
    plan_generated: bool = Field(default=False)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class MAPStaticAgent(LocalAgent[MAPStaticState]):
    """
    Multi-Agent Planner + Executor (Static) system.

    Architecture: Planner + Executor
    - Planner: Generates a one-time plan from the first user message (no tools)
    - Executor: Follows the plan with full tool access + full domain policy

    Static mode: Plan is generated ONCE and never revised.
    """

    def __init__(
        self,
        tools: List[Tool],
        domain_policy: str,
        llm: Optional[str] = None,
        llm_args: Optional[dict] = None,
    ):
        """Initialize the MA-P-Static agent system."""
        super().__init__(tools=tools, domain_policy=domain_policy)
        self.llm = llm
        self.llm_args = deepcopy(llm_args) if llm_args is not None else {}

        # Build tool descriptions for the planner
        self._tool_descriptions = self._format_tool_descriptions(tools)

        # Build planner system prompt (static, doesn't change)
        self._planner_system_prompt = PLANNER_SYSTEM_PROMPT.format(
            domain_policy=domain_policy,
            tool_descriptions=self._tool_descriptions,
        )

        # Store domain policy for building executor prompts dynamically
        self._domain_policy = domain_policy

    @staticmethod
    def _format_tool_descriptions(tools: List[Tool]) -> str:
        """Format tool list for the planner's reference."""
        parts = []
        for tool in tools:
            schema = tool.openai_schema
            func = schema.get("function", schema)
            name = func.get("name", "unknown")
            desc = func.get("description", "")
            params = func.get("parameters", {})
            props = params.get("properties", {})
            param_list = ", ".join(
                f"{k}: {v.get('type', '?')}" for k, v in props.items()
            )
            parts.append(f"- {name}({param_list}): {desc[:150]}")
        return "\n".join(parts)

    def _build_executor_system_prompt(self, plan: Optional[str]) -> str:
        """Build the executor system prompt with or without a plan."""
        if plan:
            return EXECUTOR_SYSTEM_PROMPT.format(
                domain_policy=self._domain_policy,
                plan=plan,
            )
        else:
            return EXECUTOR_SYSTEM_PROMPT_NO_PLAN.format(
                domain_policy=self._domain_policy,
            )

    def get_init_state(
        self, message_history: Optional[list[Message]] = None
    ) -> MAPStaticState:
        """Get the initial state of the agent."""
        if message_history is None:
            message_history = []

        assert all(is_valid_agent_history_message(m) for m in message_history), (
            "Message history must contain only AssistantMessage, UserMessage, or ToolMessage to Agent."
        )

        return MAPStaticState(
            executor_system_messages=[
                SystemMessage(
                    role="system",
                    content=self._build_executor_system_prompt(plan=None),
                )
            ],
            messages=list(message_history),
            plan=None,
            plan_generated=False,
        )

    def _call_planner(
        self, user_message_content: str
    ) -> tuple[str, float, Optional[dict]]:
        """
        Call the Planner to generate a plan from the user's message.

        Returns:
            Tuple of (plan_text, cost, usage).
        """
        planner_messages = [
            SystemMessage(role="system", content=self._planner_system_prompt),
            UserMessage(
                role="user",
                content=PLANNER_USER_PROMPT.format(
                    user_message=user_message_content,
                ),
            ),
        ]

        try:
            response = generate(
                model=self.llm,
                tools=[],  # Planner has no tools
                messages=planner_messages,
                **self.llm_args,
            )

            plan = response.content or ""
            logger.debug(f"Planner generated plan:\n{plan}")
            return plan, response.cost or 0.0, response.usage

        except Exception as e:
            logger.error(f"Planner failed: {e}, proceeding without plan")
            return "", 0.0, None

    def _call_executor(self, state: MAPStaticState) -> AssistantMessage:
        """
        Call the Executor to generate the next action.
        """
        messages = list(state.executor_system_messages) + list(state.messages)

        assistant_message = generate(
            model=self.llm,
            tools=self.tools,
            messages=messages,
            **self.llm_args,
        )
        return assistant_message

    def generate_next_message(
        self, message: ValidAgentInputMessage, state: MAPStaticState
    ) -> tuple[AssistantMessage, MAPStaticState]:
        """
        Generate the next message using the Planner + Executor pipeline.

        Flow:
        1. Add incoming message to state
        2. If first user message and no plan yet → call Planner
        3. Call Executor (with plan in system prompt if available)
        """
        # 1. Add incoming message to history
        if isinstance(message, MultiToolMessage):
            state.messages.extend(message.tool_messages)
        else:
            state.messages.append(message)

        total_planner_cost = 0.0
        total_planner_usage = {"completion_tokens": 0, "prompt_tokens": 0}

        # 2. Generate plan on first user message
        is_user_message = isinstance(message, UserMessage)
        if is_user_message and not state.plan_generated:
            user_content = message.content or ""
            plan, p_cost, p_usage = self._call_planner(user_content)
            total_planner_cost += p_cost
            if p_usage:
                total_planner_usage["completion_tokens"] += p_usage.get(
                    "completion_tokens", 0
                )
                total_planner_usage["prompt_tokens"] += p_usage.get("prompt_tokens", 0)

            state.plan = plan
            state.plan_generated = True

            # Rebuild executor system prompt with the plan
            state.executor_system_messages = [
                SystemMessage(
                    role="system",
                    content=self._build_executor_system_prompt(plan=plan),
                )
            ]

        # 3. Call Executor
        proposed = self._call_executor(state)

        # 4. Finalize: add metadata and commit to state
        if proposed.metadata is None:
            proposed.metadata = {}
        proposed.metadata["generated_by"] = "map_static"
        proposed.metadata["has_plan"] = state.plan_generated
        if total_planner_cost > 0:
            proposed.metadata["planner_cost"] = total_planner_cost
            proposed.metadata["planner_usage"] = total_planner_usage

        # Merge planner cost into the message
        if total_planner_cost > 0:
            if proposed.cost is not None:
                proposed.cost += total_planner_cost
            else:
                proposed.cost = total_planner_cost
            if proposed.usage is not None:
                proposed.usage["completion_tokens"] = (
                    proposed.usage.get("completion_tokens", 0)
                    + total_planner_usage["completion_tokens"]
                )
                proposed.usage["prompt_tokens"] = (
                    proposed.usage.get("prompt_tokens", 0)
                    + total_planner_usage["prompt_tokens"]
                )
            elif (
                total_planner_usage["completion_tokens"] > 0
                or total_planner_usage["prompt_tokens"] > 0
            ):
                proposed.usage = dict(total_planner_usage)

        state.messages.append(proposed)
        return proposed, state

    def set_seed(self, seed: int):
        """Set the seed for the LLM."""
        if self.llm is None:
            raise ValueError("LLM is not set")
        cur_seed = self.llm_args.get("seed", None)
        if cur_seed is not None:
            logger.warning(f"Seed is already set to {cur_seed}, resetting it to {seed}")
        self.llm_args["seed"] = seed

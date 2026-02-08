"""
MA-P-Adaptive: Multi-Agent Planner + Executor (Adaptive Replanning).

Architecture:
    - Planner: Generates and revises plans. No tools.
    - Executor: Follows the plan, has full tool access + full domain policy.

Coordination:
    User → Planner → [plan] → Executor → Environment
                         ↑____________| (replan every 3 steps or on error)

Adaptive mode: The Planner generates the initial plan after the first user message,
then revises the plan:
    - Every 3 Executor steps (fixed interval)
    - Immediately on any tool error
The Planner sees the full execution history when replanning.
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
    ToolCall,
    ToolMessage,
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

PLANNER_USER_PROMPT_INITIAL = """
The customer says:
"{user_message}"

Create a step-by-step plan to resolve this customer's issue. Number each step.
""".strip()

PLANNER_USER_PROMPT_REPLAN = """
The Executor has been working on the customer's issue. Here is what has happened so far:

{execution_summary}

The previous plan was:
{previous_plan}

{replan_reason}

Please create a REVISED plan with the remaining steps needed to resolve the issue.
Take into account what has already been done. Number each step.
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
your actions. The plan may be updated periodically based on progress.
Use your judgment to adapt if a step doesn't apply, but generally stick to the plan.

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


class MAPAdaptiveState(BaseModel):
    """State for the MA-P-Adaptive multi-agent system."""

    executor_system_messages: list[SystemMessage] = Field(default_factory=list)
    messages: list[APICompatibleMessage] = Field(default_factory=list)
    plan: Optional[str] = Field(default=None, description="The current plan")
    plan_generated: bool = Field(default=False)
    steps_since_replan: int = Field(
        default=0, description="Number of Executor steps since last (re)plan"
    )
    last_had_error: bool = Field(
        default=False, description="Whether the last tool result was an error"
    )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

REPLAN_INTERVAL = 3  # Replan every N executor steps


class MAPAdaptiveAgent(LocalAgent[MAPAdaptiveState]):
    """
    Multi-Agent Planner + Executor (Adaptive) system.

    Architecture: Planner + Executor
    - Planner: Generates initial plan + revises it periodically (no tools)
    - Executor: Follows the plan with full tool access + full domain policy

    Adaptive mode: Replanning triggered by:
    - Every 3 Executor steps
    - Immediately on any tool error
    """

    def __init__(
        self,
        tools: List[Tool],
        domain_policy: str,
        llm: Optional[str] = None,
        llm_args: Optional[dict] = None,
    ):
        """Initialize the MA-P-Adaptive agent system."""
        super().__init__(tools=tools, domain_policy=domain_policy)
        self.llm = llm
        self.llm_args = deepcopy(llm_args) if llm_args is not None else {}

        self._tool_descriptions = self._format_tool_descriptions(tools)

        self._planner_system_prompt = PLANNER_SYSTEM_PROMPT.format(
            domain_policy=domain_policy,
            tool_descriptions=self._tool_descriptions,
        )

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

    def _format_execution_summary(self, messages: list[APICompatibleMessage]) -> str:
        """Format the execution history for the Planner's replan context."""
        summary_parts = []
        for msg in messages:
            if isinstance(msg, UserMessage):
                content = msg.content or ""
                summary_parts.append(f"User: {content[:300]}")
            elif isinstance(msg, AssistantMessage):
                if msg.is_tool_call():
                    tool_names = [tc.name for tc in msg.tool_calls]
                    summary_parts.append(
                        f"Agent action: [tool calls: {', '.join(tool_names)}]"
                    )
                else:
                    content = msg.content or ""
                    summary_parts.append(f"Agent message to user: {content[:300]}")
            elif isinstance(msg, ToolMessage):
                error_marker = " [ERROR]" if msg.error else ""
                content = msg.content or ""
                summary_parts.append(f"Tool result{error_marker}: {content[:200]}")
        return "\n".join(summary_parts) if summary_parts else "(no execution history)"

    def get_init_state(
        self, message_history: Optional[list[Message]] = None
    ) -> MAPAdaptiveState:
        """Get the initial state of the agent."""
        if message_history is None:
            message_history = []

        assert all(is_valid_agent_history_message(m) for m in message_history), (
            "Message history must contain only AssistantMessage, UserMessage, or ToolMessage to Agent."
        )

        return MAPAdaptiveState(
            executor_system_messages=[
                SystemMessage(
                    role="system",
                    content=self._build_executor_system_prompt(plan=None),
                )
            ],
            messages=list(message_history),
            plan=None,
            plan_generated=False,
            steps_since_replan=0,
            last_had_error=False,
        )

    def _call_planner_initial(
        self, user_message_content: str
    ) -> tuple[str, float, Optional[dict]]:
        """Call the Planner to generate the initial plan."""
        planner_messages = [
            SystemMessage(role="system", content=self._planner_system_prompt),
            UserMessage(
                role="user",
                content=PLANNER_USER_PROMPT_INITIAL.format(
                    user_message=user_message_content,
                ),
            ),
        ]

        try:
            response = generate(
                model=self.llm,
                tools=[],
                messages=planner_messages,
                **self.llm_args,
            )
            plan = response.content or ""
            logger.debug(f"Planner generated initial plan:\n{plan}")
            return plan, response.cost or 0.0, response.usage
        except Exception as e:
            logger.error(f"Planner failed: {e}, proceeding without plan")
            return "", 0.0, None

    def _call_planner_replan(
        self,
        state: MAPAdaptiveState,
        reason: str,
    ) -> tuple[str, float, Optional[dict]]:
        """Call the Planner to revise the plan based on execution progress."""
        execution_summary = self._format_execution_summary(state.messages)

        planner_messages = [
            SystemMessage(role="system", content=self._planner_system_prompt),
            UserMessage(
                role="user",
                content=PLANNER_USER_PROMPT_REPLAN.format(
                    execution_summary=execution_summary,
                    previous_plan=state.plan or "(no previous plan)",
                    replan_reason=reason,
                ),
            ),
        ]

        try:
            response = generate(
                model=self.llm,
                tools=[],
                messages=planner_messages,
                **self.llm_args,
            )
            plan = response.content or ""
            logger.debug(f"Planner revised plan:\n{plan}")
            return plan, response.cost or 0.0, response.usage
        except Exception as e:
            logger.error(f"Planner replan failed: {e}, keeping previous plan")
            return state.plan or "", 0.0, None

    def _update_plan(self, state: MAPAdaptiveState, plan: str) -> None:
        """Update the plan in state and rebuild executor system prompt."""
        state.plan = plan
        state.plan_generated = True
        state.steps_since_replan = 0
        state.executor_system_messages = [
            SystemMessage(
                role="system",
                content=self._build_executor_system_prompt(plan=plan),
            )
        ]

    def _call_executor(self, state: MAPAdaptiveState) -> AssistantMessage:
        """Call the Executor to generate the next action."""
        messages = list(state.executor_system_messages) + list(state.messages)

        assistant_message = generate(
            model=self.llm,
            tools=self.tools,
            messages=messages,
            **self.llm_args,
        )
        return assistant_message

    def _check_incoming_for_errors(self, message: ValidAgentInputMessage) -> bool:
        """Check if the incoming message contains tool errors."""
        if isinstance(message, MultiToolMessage):
            return any(tm.error for tm in message.tool_messages)
        elif isinstance(message, ToolMessage):
            return message.error
        return False

    def generate_next_message(
        self, message: ValidAgentInputMessage, state: MAPAdaptiveState
    ) -> tuple[AssistantMessage, MAPAdaptiveState]:
        """
        Generate the next message using the Planner + Executor pipeline
        with adaptive replanning.

        Flow:
        1. Add incoming message to state
        2. Check for replan triggers:
           a. First user message → initial plan
           b. Tool error in incoming message → immediate replan
           c. steps_since_replan >= 3 → interval replan
        3. Call Executor
        """
        # 1. Detect errors before adding to state
        has_error = self._check_incoming_for_errors(message)

        # Add incoming message to history
        if isinstance(message, MultiToolMessage):
            state.messages.extend(message.tool_messages)
        else:
            state.messages.append(message)

        total_planner_cost = 0.0
        total_planner_usage = {"completion_tokens": 0, "prompt_tokens": 0}

        is_user_message = isinstance(message, UserMessage)

        # 2. Planning / Replanning logic
        needs_replan = False
        replan_reason = ""

        if is_user_message and not state.plan_generated:
            # 2a. Initial plan on first user message
            user_content = message.content or ""
            plan, p_cost, p_usage = self._call_planner_initial(user_content)
            total_planner_cost += p_cost
            if p_usage:
                total_planner_usage["completion_tokens"] += p_usage.get(
                    "completion_tokens", 0
                )
                total_planner_usage["prompt_tokens"] += p_usage.get("prompt_tokens", 0)
            self._update_plan(state, plan)

        elif state.plan_generated:
            # 2b. Check for replan triggers
            if has_error:
                needs_replan = True
                replan_reason = (
                    "A tool error occurred. Please revise the plan to handle "
                    "this error and continue resolving the customer's issue."
                )
            elif state.steps_since_replan >= REPLAN_INTERVAL:
                needs_replan = True
                replan_reason = (
                    f"The Executor has completed {state.steps_since_replan} steps "
                    f"since the last plan. Please review progress and revise the "
                    f"remaining steps if needed."
                )

            if needs_replan:
                plan, p_cost, p_usage = self._call_planner_replan(
                    state, reason=replan_reason
                )
                total_planner_cost += p_cost
                if p_usage:
                    total_planner_usage["completion_tokens"] += p_usage.get(
                        "completion_tokens", 0
                    )
                    total_planner_usage["prompt_tokens"] += p_usage.get(
                        "prompt_tokens", 0
                    )
                self._update_plan(state, plan)

        # 3. Call Executor
        proposed = self._call_executor(state)

        # Safety net: if Executor produces empty message, fall back
        if not proposed.content and not (
            proposed.tool_calls and len(proposed.tool_calls) > 0
        ):
            logger.warning(
                "Executor produced empty message. Falling back to safe response."
            )
            proposed = AssistantMessage(
                role="assistant",
                content="I apologize, could you please repeat your question or provide more details so I can assist you?",
                cost=proposed.cost,
                usage=proposed.usage,
                metadata={},
            )

        # Increment step counter
        state.steps_since_replan += 1

        # 4. Finalize metadata
        if proposed.metadata is None:
            proposed.metadata = {}
        proposed.metadata["generated_by"] = "map_adaptive"
        proposed.metadata["has_plan"] = state.plan_generated
        proposed.metadata["steps_since_replan"] = state.steps_since_replan
        if needs_replan:
            proposed.metadata["replanned"] = True
            proposed.metadata["replan_reason"] = replan_reason
        if total_planner_cost > 0:
            proposed.metadata["planner_cost"] = total_planner_cost
            proposed.metadata["planner_usage"] = total_planner_usage

        # Merge planner cost
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

"""
MA-V-Hard: Multi-Agent Executor + Verifier (Hard Verification).

Architecture:
    - Executor: ReAct agent with full tool access, generates actions/messages
    - Verifier: Evaluation-only agent (no tools), checks Executor's output

Coordination: Sequential chain with hard (blocking) verification
    User → Executor → Verifier → [approve] → Environment/User
                              → [reject]  → Executor (retry, up to MAX_RETRIES)
                              → [all retries exhausted] → transfer_to_human_agents

Hard mode: Verifier BLOCKS the action. The Executor must keep retrying until
the Verifier approves or MAX_RETRIES is reached. If all retries are exhausted,
the system calls transfer_to_human_agents as a safety fallback.
"""

import json
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
    UserMessage,
)
from tau2.environment.tool import Tool
from tau2.utils.llm_utils import generate

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

EXECUTOR_SYSTEM_PROMPT = """
<instructions>
You are a customer service agent (Executor) that helps the user according to the <policy> provided below.
In each turn you can either:
- Send a message to the user.
- Make a tool call.
You cannot do both at the same time.

After you produce an action, a Verifier agent will review it. The Verifier can BLOCK
your action if it violates policy or is inappropriate. If blocked, you MUST revise
your action based on the Verifier's feedback. Your action will not proceed until
the Verifier approves it.

Try to be helpful and always follow the policy. Always make sure you generate valid JSON only.
</instructions>
<policy>
{domain_policy}
</policy>
""".strip()

VERIFIER_SYSTEM_PROMPT = """
<instructions>
You are a Verifier agent. Your role is to evaluate actions proposed by the Executor agent
in a customer service context. You ensure that the Executor's actions comply with the
policy and are appropriate for the situation.

You do NOT have access to any tools. You can only evaluate and provide feedback.

For each proposed action, you must respond with exactly one of the following verdicts
on the FIRST LINE of your response:
- APPROVE — the action is correct and should proceed
- REJECT — the action violates policy or is inappropriate
- SUGGEST — the action could be improved

After the verdict, provide a brief explanation (1-3 sentences) of your reasoning.

IMPORTANT: In hard verification mode, REJECT and SUGGEST both BLOCK the action.
Only APPROVE allows the action to proceed. Be precise but fair in your evaluation.

Example responses:
APPROVE
The tool call correctly looks up the customer's account before making changes.

REJECT
The agent is attempting to change the plan without first verifying the customer's identity.

SUGGEST
The message to the user is correct but could be more specific about the expected resolution time.

Evaluate based on the policy below and the conversation context.
</instructions>
<policy>
{domain_policy}
</policy>
""".strip()


VERIFIER_USER_PROMPT_TOOL_CALL = """
The Executor proposes the following tool call(s):

{tool_calls_description}

Conversation context so far:
{conversation_summary}

Is this action appropriate given the policy and conversation context?
Respond with APPROVE, REJECT, or SUGGEST on the first line, followed by a brief explanation.
""".strip()


VERIFIER_USER_PROMPT_TEXT_MESSAGE = """
The Executor proposes sending the following message to the customer:

"{message_content}"

Conversation context so far:
{conversation_summary}

Is this message appropriate given the policy and conversation context?
Respond with APPROVE, REJECT, or SUGGEST on the first line, followed by a brief explanation.
""".strip()


EXECUTOR_RETRY_PROMPT = """
[VERIFIER BLOCKED — Attempt {attempt} of {max_attempts}]
The Verifier has BLOCKED your proposed action and provided the following feedback:
Verdict: {verdict}
Feedback: {feedback}

You MUST revise your action to address this feedback. Your action will not proceed
until the Verifier approves it. Remember:
- You can either send a message to the user OR make a tool call, not both.
- Follow the policy strictly.
- Address the specific issue the Verifier identified.
""".strip()


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------


class VerifierVerdict:
    """Parsed verifier response."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    SUGGEST = "SUGGEST"

    def __init__(self, verdict: str, explanation: str):
        self.verdict = verdict
        self.explanation = explanation

    @property
    def is_approved(self) -> bool:
        return self.verdict == self.APPROVE

    @classmethod
    def parse(cls, response_text: str) -> "VerifierVerdict":
        """Parse the verifier's response into a verdict and explanation."""
        if not response_text or not response_text.strip():
            logger.warning("Empty verifier response, defaulting to APPROVE")
            return cls(cls.APPROVE, "No feedback provided.")

        lines = response_text.strip().split("\n", 1)
        first_line = lines[0].strip().upper()
        explanation = lines[1].strip() if len(lines) > 1 else ""

        if first_line.startswith("APPROVE"):
            return cls(cls.APPROVE, explanation)
        elif first_line.startswith("REJECT"):
            return cls(cls.REJECT, explanation)
        elif first_line.startswith("SUGGEST"):
            return cls(cls.SUGGEST, explanation)
        else:
            # Try to find verdict keyword anywhere in first line
            for keyword in [cls.REJECT, cls.SUGGEST, cls.APPROVE]:
                if keyword in first_line:
                    return cls(keyword, explanation)
            logger.warning(
                f"Could not parse verifier verdict from: '{first_line}', defaulting to APPROVE"
            )
            return cls(cls.APPROVE, response_text.strip())


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class MAVHardState(BaseModel):
    """State for the MA-V-Hard multi-agent system."""

    executor_system_messages: list[SystemMessage] = Field(default_factory=list)
    messages: list[APICompatibleMessage] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

TRANSFER_TOOL_NAME = "transfer_to_human_agents"


class MAVHardAgent(LocalAgent[MAVHardState]):
    """
    Multi-Agent Verification (Hard) system.

    Architecture: Executor + Verifier
    - Executor: Full ReAct agent with tool access, handles all actions
    - Verifier: Evaluation-only agent, checks Executor's proposals

    Coordination: Sequential with hard (blocking) verification
    - Executor proposes → Verifier evaluates
    - APPROVE → action proceeds
    - REJECT/SUGGEST → action BLOCKED, Executor retries
    - After MAX_RETRIES exhausted → transfer_to_human_agents

    Both tool calls and text messages are verified.
    """

    MAX_RETRIES = 4  # 4 retries = 5 total attempts

    def __init__(
        self,
        tools: List[Tool],
        domain_policy: str,
        llm: Optional[str] = None,
        llm_args: Optional[dict] = None,
    ):
        """Initialize the MA-V-Hard agent system."""
        super().__init__(tools=tools, domain_policy=domain_policy)
        self.llm = llm
        self.llm_args = deepcopy(llm_args) if llm_args is not None else {}

        # Build system prompts
        self._executor_system_prompt = EXECUTOR_SYSTEM_PROMPT.format(
            domain_policy=domain_policy,
        )
        self._verifier_system_prompt = VERIFIER_SYSTEM_PROMPT.format(
            domain_policy=domain_policy,
        )

        # Verify transfer tool exists
        tool_names = {tool.name for tool in self.tools}
        if TRANSFER_TOOL_NAME not in tool_names:
            logger.warning(
                f"Tool '{TRANSFER_TOOL_NAME}' not found in tools. "
                f"Fallback on max retries will use a text message instead."
            )

    def get_init_state(
        self, message_history: Optional[list[Message]] = None
    ) -> MAVHardState:
        """Get the initial state of the agent."""
        if message_history is None:
            message_history = []

        assert all(is_valid_agent_history_message(m) for m in message_history), (
            "Message history must contain only AssistantMessage, UserMessage, or ToolMessage to Agent."
        )

        return MAVHardState(
            executor_system_messages=[
                SystemMessage(role="system", content=self._executor_system_prompt)
            ],
            messages=list(message_history),
        )

    def _format_tool_calls(self, tool_calls: list[ToolCall]) -> str:
        """Format tool calls for the verifier prompt."""
        parts = []
        for tc in tool_calls:
            parts.append(
                f"Tool: {tc.name}\nArguments: {json.dumps(tc.arguments, indent=2)}"
            )
        return "\n---\n".join(parts)

    def _format_conversation_summary(self, messages: list[APICompatibleMessage]) -> str:
        """Create a brief conversation summary for the verifier."""
        summary_parts = []
        recent = messages[-10:] if len(messages) > 10 else messages
        for msg in recent:
            if isinstance(msg, UserMessage):
                content = msg.content or ""
                summary_parts.append(f"User: {content[:300]}")
            elif isinstance(msg, AssistantMessage):
                if msg.is_tool_call():
                    tool_names = [tc.name for tc in msg.tool_calls]
                    summary_parts.append(
                        f"Agent: [tool calls: {', '.join(tool_names)}]"
                    )
                else:
                    content = msg.content or ""
                    summary_parts.append(f"Agent: {content[:300]}")
            elif hasattr(msg, "role") and msg.role == "tool":
                content = getattr(msg, "content", "") or ""
                summary_parts.append(f"Tool result: {content[:200]}")
        return "\n".join(summary_parts) if summary_parts else "(no prior conversation)"

    def _call_verifier(
        self,
        proposed_message: AssistantMessage,
        conversation_messages: list[APICompatibleMessage],
    ) -> tuple:
        """
        Call the Verifier to evaluate the Executor's proposed action.

        Returns:
            Tuple of (VerifierVerdict, cost, usage).
        """
        conversation_summary = self._format_conversation_summary(conversation_messages)

        if proposed_message.is_tool_call():
            tool_calls_desc = self._format_tool_calls(proposed_message.tool_calls)
            user_prompt = VERIFIER_USER_PROMPT_TOOL_CALL.format(
                tool_calls_description=tool_calls_desc,
                conversation_summary=conversation_summary,
            )
        else:
            user_prompt = VERIFIER_USER_PROMPT_TEXT_MESSAGE.format(
                message_content=proposed_message.content or "",
                conversation_summary=conversation_summary,
            )

        verifier_messages = [
            SystemMessage(role="system", content=self._verifier_system_prompt),
            UserMessage(role="user", content=user_prompt),
        ]

        try:
            response = generate(
                model=self.llm,
                tools=[],  # Verifier has no tools
                messages=verifier_messages,
                **self.llm_args,
            )

            verdict = VerifierVerdict.parse(response.content or "")
            logger.debug(f"Verifier verdict: {verdict.verdict} — {verdict.explanation}")

            return verdict, response.cost or 0.0, response.usage

        except Exception as e:
            logger.error(f"Verifier call failed: {e}, defaulting to APPROVE")
            return (
                VerifierVerdict(VerifierVerdict.APPROVE, f"Verifier error: {e}"),
                0.0,
                None,
            )

    def _call_executor(
        self, state: MAVHardState, retry_prompt: Optional[str] = None
    ) -> AssistantMessage:
        """
        Call the Executor to generate or revise an action.

        Args:
            state: Current agent state.
            retry_prompt: If provided, verifier feedback for retry.

        Returns:
            The Executor's proposed AssistantMessage.
        """
        messages = list(state.executor_system_messages) + list(state.messages)

        # If this is a retry, add the verifier's rejection feedback
        if retry_prompt is not None:
            feedback_msg = UserMessage(
                role="user",
                content=retry_prompt,
            )
            messages.append(feedback_msg)

        assistant_message = generate(
            model=self.llm,
            tools=self.tools,
            messages=messages,
            **self.llm_args,
        )
        return assistant_message

    def _make_transfer_message(self) -> AssistantMessage:
        """
        Create a transfer_to_human_agents tool call message as fallback.
        """
        tool_call = ToolCall(
            id="mav_hard_transfer_fallback",
            name=TRANSFER_TOOL_NAME,
            arguments={
                "reason": "Automated verification could not approve the agent's action after multiple attempts."
            },
            requestor="assistant",
        )
        return AssistantMessage(
            role="assistant",
            content=None,
            tool_calls=[tool_call],
            cost=0.0,
            usage={"completion_tokens": 0, "prompt_tokens": 0},
            metadata={},
        )

    def _make_fallback_text_message(self) -> AssistantMessage:
        """
        Fallback text message if transfer_to_human_agents tool is not available.
        """
        return AssistantMessage(
            role="assistant",
            content=(
                "I apologize for the inconvenience. I'm having difficulty processing "
                "your request correctly. Let me transfer you to a human agent who can "
                "assist you further."
            ),
            cost=0.0,
            usage={"completion_tokens": 0, "prompt_tokens": 0},
            metadata={},
        )

    def generate_next_message(
        self, message: ValidAgentInputMessage, state: MAVHardState
    ) -> tuple[AssistantMessage, MAVHardState]:
        """
        Generate the next message using the Executor + Verifier pipeline
        with hard (blocking) verification.

        Flow:
        1. Add incoming message to state
        2. Executor generates proposed action
        3. Verifier evaluates the proposal
        4. If APPROVE → action proceeds
        5. If REJECT/SUGGEST → Executor retries (up to MAX_RETRIES)
        6. If all retries exhausted → transfer_to_human_agents
        """
        # 1. Add incoming message to history
        if isinstance(message, MultiToolMessage):
            state.messages.extend(message.tool_messages)
        else:
            state.messages.append(message)

        total_extra_cost = 0.0
        total_extra_usage = {"completion_tokens": 0, "prompt_tokens": 0}
        max_attempts = self.MAX_RETRIES + 1  # 5 total attempts

        # 2. Executor generates initial proposal
        proposed = self._call_executor(state)
        attempt = 1

        # 3. Verification loop: keep retrying until approved or max retries
        while attempt <= max_attempts:
            verdict, v_cost, v_usage = self._call_verifier(proposed, state.messages)
            total_extra_cost += v_cost
            if v_usage:
                total_extra_usage["completion_tokens"] += v_usage.get(
                    "completion_tokens", 0
                )
                total_extra_usage["prompt_tokens"] += v_usage.get("prompt_tokens", 0)

            if verdict.is_approved:
                logger.debug(f"Verifier APPROVED on attempt {attempt}/{max_attempts}")
                break

            logger.debug(
                f"Verifier {verdict.verdict} on attempt {attempt}/{max_attempts}: "
                f"{verdict.explanation}"
            )

            # If we've used all attempts, break — will fall through to transfer
            if attempt >= max_attempts:
                logger.warning(
                    f"Verifier rejected all {max_attempts} attempts. "
                    f"Transferring to human agent."
                )
                break

            # Track the rejected proposal's cost
            if proposed.cost:
                total_extra_cost += proposed.cost
            if proposed.usage:
                total_extra_usage["completion_tokens"] += proposed.usage.get(
                    "completion_tokens", 0
                )
                total_extra_usage["prompt_tokens"] += proposed.usage.get(
                    "prompt_tokens", 0
                )

            # Retry with verifier feedback
            retry_prompt = EXECUTOR_RETRY_PROMPT.format(
                attempt=attempt,
                max_attempts=max_attempts,
                verdict=verdict.verdict,
                feedback=verdict.explanation,
            )
            proposed = self._call_executor(state, retry_prompt=retry_prompt)
            attempt += 1

        # 4. If verifier never approved after all attempts → transfer to human
        if not verdict.is_approved:
            # Track the last rejected proposal's cost too
            if proposed.cost:
                total_extra_cost += proposed.cost
            if proposed.usage:
                total_extra_usage["completion_tokens"] += proposed.usage.get(
                    "completion_tokens", 0
                )
                total_extra_usage["prompt_tokens"] += proposed.usage.get(
                    "prompt_tokens", 0
                )

            # Use transfer tool if available, otherwise text fallback
            tool_names = {tool.name for tool in self.tools}
            if TRANSFER_TOOL_NAME in tool_names:
                proposed = self._make_transfer_message()
            else:
                proposed = self._make_fallback_text_message()

        # 5. Finalize: add metadata and commit to state
        if proposed.metadata is None:
            proposed.metadata = {}
        proposed.metadata["verifier_verdict"] = verdict.verdict
        proposed.metadata["verifier_explanation"] = verdict.explanation
        proposed.metadata["verifier_attempts"] = attempt
        proposed.metadata["verifier_cost"] = total_extra_cost
        proposed.metadata["verifier_usage"] = total_extra_usage
        proposed.metadata["generated_by"] = "mav_hard"

        # Merge all extra costs into the message
        if proposed.cost is not None:
            proposed.cost += total_extra_cost
        else:
            proposed.cost = total_extra_cost
        if proposed.usage is not None:
            proposed.usage["completion_tokens"] = (
                proposed.usage.get("completion_tokens", 0)
                + total_extra_usage["completion_tokens"]
            )
            proposed.usage["prompt_tokens"] = (
                proposed.usage.get("prompt_tokens", 0)
                + total_extra_usage["prompt_tokens"]
            )
        elif (
            total_extra_usage["completion_tokens"] > 0
            or total_extra_usage["prompt_tokens"] > 0
        ):
            proposed.usage = dict(total_extra_usage)

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

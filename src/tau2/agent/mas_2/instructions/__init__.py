"""Instructions module for MA-S-2 multi-agent system (2 specialists)."""

from .base_policy import DEVICE_ACTIONS_REFERENCE, MAIN_POLICY, get_base_policy
from .service_issue_policy import SERVICE_ISSUE_AGENT_IDENTITY, SERVICE_ISSUE_POLICY
from .mobile_data_issue_policy import (
    MOBILE_DATA_ISSUE_AGENT_IDENTITY,
    MOBILE_DATA_ISSUE_POLICY,
)
from .mms_issue_policy import MMS_ISSUE_AGENT_IDENTITY, MMS_ISSUE_POLICY
from .router_instructions import (
    ROUTER_SYSTEM_PROMPT,
    create_router_user_message,
    format_conversation_for_router,
    get_router_system_prompt,
)

__all__ = [
    # Base policy
    "get_base_policy",
    "MAIN_POLICY",
    "DEVICE_ACTIONS_REFERENCE",
    # Specialized policies
    "SERVICE_ISSUE_AGENT_IDENTITY",
    "SERVICE_ISSUE_POLICY",
    "MOBILE_DATA_ISSUE_AGENT_IDENTITY",
    "MOBILE_DATA_ISSUE_POLICY",
    "MMS_ISSUE_AGENT_IDENTITY",
    "MMS_ISSUE_POLICY",
    # Router
    "get_router_system_prompt",
    "format_conversation_for_router",
    "create_router_user_message",
    "ROUTER_SYSTEM_PROMPT",
]

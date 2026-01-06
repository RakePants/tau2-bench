"""Instructions package for telecom multi-agent system."""

from tau2.agent.paper_multi_agent.instructions.base_policy import (
    BASE_POLICY,
    USER_DEVICE_CAPABILITIES,
)
from tau2.agent.paper_multi_agent.instructions.router_instructions import (
    ROUTER_SYSTEM_PROMPT,
)
from tau2.agent.paper_multi_agent.instructions.service_issue_policy import (
    SERVICE_ISSUE_POLICY,
)
from tau2.agent.paper_multi_agent.instructions.mobile_data_issue_policy import (
    MOBILE_DATA_ISSUE_POLICY,
)
from tau2.agent.paper_multi_agent.instructions.mms_issue_policy import (
    MMS_ISSUE_POLICY,
)

__all__ = [
    "BASE_POLICY",
    "USER_DEVICE_CAPABILITIES",
    "ROUTER_SYSTEM_PROMPT",
    "SERVICE_ISSUE_POLICY",
    "MOBILE_DATA_ISSUE_POLICY",
    "MMS_ISSUE_POLICY",
]

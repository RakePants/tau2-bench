# Telecom Multi-Agent System

A multi-agent architecture for the tau2 telecom domain benchmark. This system routes customer issues to specialized agents based on the type of problem.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      User Message                               │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Router (LLM or Heuristic)                    │
│   Determines issue type from initial message                    │
└───────────┬─────────────────┬──────────────────┬────────────────┘
            │                 │                  │
            ▼                 ▼                  ▼
┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐
│ Service Issue     │ │ Mobile Data Issue │ │ MMS Issue         │
│ Agent             │ │ Agent             │ │ Agent             │
│                   │ │                   │ │                   │
│ - No service      │ │ - Slow/no data    │ │ - Can't send      │
│ - No signal       │ │ - Roaming issues  │ │   pictures        │
│ - SIM problems    │ │ - Data limits     │ │ - Group text      │
│ - Bill issues     │ │ - VPN issues      │ │ - App permissions │
└───────────────────┘ └───────────────────┘ └───────────────────┘
```

## Issue Types

### 1. Service Issues (`service_issue`)
Problems with basic cellular connectivity:
- No service/signal
- Airplane mode issues
- SIM card problems (missing, locked)
- Line suspension (billing, contract)
- APN settings issues

### 2. Mobile Data Issues (`mobile_data_issue`)
Internet connectivity problems:
- Mobile data disabled
- Slow connection
- Roaming issues when abroad
- Data limit exceeded
- Data saver mode interference
- VPN issues
- Network mode preferences

### 3. MMS Issues (`mms_issue`)
Picture/video messaging problems:
- Cannot send/receive MMS
- MMSC URL configuration
- Wi-Fi calling interference
- App permission issues
- Network technology (2G insufficient)

## Implementations

### PaperMultiAgent (LLM Routing)
Uses an LLM call to analyze the user's message and determine the issue type.

```python
from tau2.agent.paper_multi_agent import PaperMultiAgent

agent = PaperMultiAgent(
    tools=tools,
    domain_policy=policy,
    llm="gpt-4",
    llm_args={"temperature": 0.0}
)
```

**Pros:** More accurate classification for complex/ambiguous messages
**Cons:** Extra LLM call for routing adds latency

### TelecomHeuristicMultiAgent (Keyword Routing)
Uses keyword-based heuristics for faster, deterministic routing.

```python
from tau2.agent.paper_multi_agent import TelecomHeuristicMultiAgent

agent = TelecomHeuristicMultiAgent(
    tools=tools,
    domain_policy=policy,
    llm="gpt-4",
    llm_args={"temperature": 0.0}
)
```

**Pros:** No extra LLM calls, deterministic, faster
**Cons:** May misclassify complex/edge cases

## Usage with tau2 Registry

### Option 1: Import the registration module
```python
# Auto-registers both agents with the registry
import tau2.agent.paper_multi_agent.register
```

### Option 2: Manual registration
```python
from tau2.registry import registry
from tau2.agent.paper_multi_agent import PaperMultiAgent, TelecomHeuristicMultiAgent

registry.register_agent(PaperMultiAgent, "paper_multi_agent")
registry.register_agent(TelecomHeuristicMultiAgent, "telecom_heuristic_multi_agent")
```

### Running with CLI
After registration, use with tau2 CLI:
```bash
# LLM-routed multi-agent
tau2 run --domain telecom --agent paper_multi_agent --agent-llm gpt-4

# Heuristic-routed multi-agent  
tau2 run --domain telecom --agent telecom_heuristic_multi_agent --agent-llm gpt-4
```

## File Structure

```
paper_multi_agent/
├── __init__.py                    # Package exports
├── multi_agent.py                 # LLM-routed multi-agent
├── heuristic_multi_agent.py       # Keyword-routed multi-agent
├── register.py                    # Registry integration
├── README.md                      # This file
└── instructions/
    ├── __init__.py
    ├── base_policy.py             # Shared policy for all agents
    ├── router_instructions.py     # LLM router prompt
    ├── service_issue_policy.py    # Service specialist policy
    ├── mobile_data_issue_policy.py # Data specialist policy
    └── mms_issue_policy.py        # MMS specialist policy
```

## How It Works

1. **First Message**: When the user sends their first message, the router analyzes it to determine the issue type.

2. **Routing**: Based on the classification, the appropriate specialized agent is activated with its specific system prompt.

3. **Conversation**: All subsequent messages are handled by the selected specialized agent.

4. **Shared Tools**: All agents have access to the full set of telecom tools, but their instructions guide them to focus on relevant troubleshooting steps.

## Keyword Routing Logic

The heuristic router uses the following priority:

1. **MMS Keywords** (highest priority, most specific):
   - mms, picture message, photo message, video message, group text, etc.

2. **Mobile Data Keywords**:
   - mobile data, internet, slow data, browsing, roaming, etc.

3. **Service Keywords** (default fallback):
   - no service, no signal, airplane mode, sim card, etc.

If no keywords match, defaults to `service_issue` as it handles the most fundamental connectivity problems.

## Extending

### Adding New Issue Types

1. Create a new policy file in `instructions/`
2. Add the issue type to `IssueType` enum
3. Add routing logic (keywords or LLM prompt update)
4. Build the system prompt in the agent class

### Customizing Routing

For the heuristic router, modify the keyword lists in `heuristic_multi_agent.py`:
- `MMS_KEYWORDS`
- `MOBILE_DATA_KEYWORDS`
- `SERVICE_KEYWORDS`

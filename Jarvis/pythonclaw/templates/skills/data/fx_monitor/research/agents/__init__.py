"""
Phase 9 research agents.

Each agent is a stateless module exposing a single function:

    run(task: ResearchTask) -> AgentOutput

Agents only receive ResearchTask and only return AgentOutput.
Agents never communicate with each other.
The coordinator is the only caller of build_safe_user_context().
"""

from .fx_agent import run as fx_agent
from .news_agent import run as news_agent
from .macro_agent import run as macro_agent
from .risk_agent import run as risk_agent

__all__ = ["fx_agent", "news_agent", "macro_agent", "risk_agent"]

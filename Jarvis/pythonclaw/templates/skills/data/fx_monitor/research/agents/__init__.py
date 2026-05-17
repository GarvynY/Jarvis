"""
Phase 9 research agents.

Each Phase-1 agent exposes:
    agent.agent_name: str
    await agent.run(task: ResearchTask) -> AgentOutput

RiskAgent (Phase-2) extends the protocol with a second argument:
    await agent.run(task: ResearchTask, phase1_outputs: list[AgentOutput]) -> AgentOutput

Agents only receive ResearchTask (and phase1_outputs for RiskAgent).
Agents never communicate with each other.
The coordinator is the only caller of build_safe_user_context().

Usage:
    from research.agents import FXAgent, NewsAgent, MacroAgent, RiskAgent
    fx_agent    = FXAgent()
    news_agent  = NewsAgent()
    macro_agent = MacroAgent()
    risk_agent  = RiskAgent()
    output = await fx_agent.run(task)
    risk   = await risk_agent.run(task, [output, ...])
"""

from .fx_agent    import FXAgent
from .news_agent  import NewsAgent
from .macro_agent import MacroAgent
from .risk_agent  import RiskAgent
from .market_drivers_agent import MarketDriversAgent, _ENABLE_MARKET_DRIVERS_AGENT
from .policy_signal_agent import PolicySignalAgent, _ENABLE_POLICY_AGENT

__all__ = ["FXAgent", "NewsAgent", "MacroAgent", "RiskAgent", "MarketDriversAgent", "PolicySignalAgent"]

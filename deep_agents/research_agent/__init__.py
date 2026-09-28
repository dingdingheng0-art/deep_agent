"""Deep Agents - 深度研究智能体工具集

本包包含用于互联网搜索和研究的智能体实现。

使用示例：
    from deep_agents.research_agent import research
    
    result = await research("your query")
"""

from .research_agent import (
    load_search_tools,
    create_research_agent,
    research,
)

__all__ = [
    "load_search_tools",
    "create_research_agent",
    "research",
]

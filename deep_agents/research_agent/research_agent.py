"""基于 Tavily MCP 搜索工具的研究型智能体。

本模块使用 langchain.mcp.MCPAdapter 连接到远程 Tavily MCP 服务器，
加载搜索工具并通过 create_agent 创建研究智能体。

直接运行可进行演示：python research_agent.py
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.mcp import MCPAdapter

load_dotenv()

# Tavily MCP 服务器地址
TAVILY_MCP_URL = "https://mcp.tavily.com/mcp/"

# 默认使用的模型
DEFAULT_MODEL = "claude-sonnet-5"


def _get_mcp_url() -> str:
    """构建带 API Key 的 Tavily MCP URL。"""
    tavily_api_key = os.environ.get("TAVILY_API_KEY", "")
    return f"{TAVILY_MCP_URL}?tavilyApiKey={tavily_api_key}"


async def load_search_tools():
    """从远程 Tavily MCP 服务器加载搜索工具。
    
    Returns:
        List[BaseTool]: Tavily 搜索工具列表，包含 tavily_search、tavily_extract 等
    """
    url = _get_mcp_url()
    async with MCPAdapter(url) as adapter:
        tools = await adapter.list_tools()
    return tools


async def create_research_agent(tools=None, model: str = DEFAULT_MODEL):
    """创建研究智能体。
    
    Args:
        tools: 工具列表，如果为 None 则自动从 Tavily MCP 加载
        model: 使用的模型名称，默认为 claude-sonnet-5
    
    Returns:
        创建好的 Agent 实例
    """
    if tools is None:
        tools = await load_search_tools()
    
    agent = create_agent(model, tools)
    return agent


def _extract_text(content) -> str:
    """从消息内容（字符串或内容块列表）中提取文本。
    
    Args:
        content: 消息内容，可以是字符串或内容块列表
    
    Returns:
        提取的纯文本内容
    """
    if isinstance(content, list):
        # Anthropic 风格的内容块：只保留文本块
        return "\n".join(
            block["text"] for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return content if isinstance(content, str) else str(content)


async def research(query: str, model: str = DEFAULT_MODEL) -> str:
    """针对查询运行研究智能体，并返回最终报告文本。
    
    Args:
        query: 研究查询内容
        model: 使用的模型名称
    
    Returns:
        研究报告的文本内容
    """
    tools = await load_search_tools()
    agent = await create_research_agent(tools, model)
    
    result = await agent.ainvoke({"messages": [{"role": "user", "content": query}]})
    return _extract_text(result["messages"][-1].content)


async def _demo(query: str, model: str = DEFAULT_MODEL) -> None:
    """直接运行一个查询，并展示可见的执行进度（工具调用）。
    
    Args:
        query: 研究查询内容
        model: 使用的模型名称
    """
    tools = await load_search_tools()
    agent = await create_research_agent(tools, model)
    
    print(f"Researching: {query}\n", flush=True)
    final_state = None
    
    async for state in agent.astream(
        {"messages": [{"role": "user", "content": query}]},
        stream_mode="values",
    ):
        final_state = state
        message = state["messages"][-1]
        
        # 打印工具调用信息
        for tool_call in getattr(message, "tool_calls", None) or []:
            args = str(tool_call.get("args", ""))[:120]
            print(f"  [tool call] {tool_call.get('name')} {args}", flush=True)
    
    if final_state is None:
        print("No results returned.")
        return
    
    print("\n" + "=" * 60)
    print("Research Report:")
    print("=" * 60)
    print(_extract_text(final_state["messages"][-1].content))


async def main_async():
    """异步主函数，用于测试和研究。"""
    query = "What is LangGraph and how does it work?"
    model = os.environ.get("DEFAULT_MODEL", DEFAULT_MODEL)
    await _demo(query, model)


if __name__ == "__main__":
    # 使用默认示例查询进行演示
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认使用 GBK 编码
    except (AttributeError, ValueError):
        pass
    
    print("=" * 60)
    print("Research Agent Demo")
    print("=" * 60)
    print("Using Tavily MCP for internet search...")
    print()
    
    asyncio.run(main_async())

"""LangGraph API Server 兼容的 Agent 定义。

本模块提供一个与 langgraph dev 兼容的研究智能体。
"""

import os
from typing import Annotated, Literal, Sequence, Any

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool, tool
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

load_dotenv()


# 默认使用的模型
DEFAULT_MODEL = "claude-sonnet-5"


class ResearchState(MessagesState, total=False):
    """研究智能体状态，包含消息历史和剩余步数。"""
    remaining_steps: int = Field(default=10, description="剩余执行步数")


@tool
def web_search(query: str) -> str:
    """Search the web for information.
    
    Args:
        query: The search query
    
    Returns:
        Search results as a string
    """
    import httpx
    
    tavily_api_key = os.environ.get("TAVILY_API_KEY", "")
    
    # 使用 Tavily REST API
    response = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key": tavily_api_key,
            "query": query,
            "max_results": 5
        },
        timeout=30.0
    )
    
    if response.status_code == 200:
        data = response.json()
        results = data.get("results", [])
        return "\n".join([f"- {r['title']}: {r['url']}" for r in results])
    else:
        return f"Search failed: {response.status_code}"


@tool
def web_extract(url: str, query: str = "") -> str:
    """Extract content from a web page.
    
    Args:
        url: The URL to extract content from
        query: Optional query to focus extraction on
    
    Returns:
        Extracted content as a string
    """
    import httpx
    
    tavily_api_key = os.environ.get("TAVILY_API_KEY", "")
    
    response = httpx.post(
        "https://api.tavily.com/extract",
        json={
            "api_key": tavily_api_key,
            "urls": [url],
            "query": query
        },
        timeout=30.0
    )
    
    if response.status_code == 200:
        data = response.json()
        results = data.get("results", [])
        return "\n".join([r.get("raw_content", "") for r in results])
    else:
        return f"Extract failed: {response.status_code}"


def create_graph():
    """创建研究智能体图。"""
    from langchain_anthropic import ChatAnthropic
    
    model_name = os.environ.get("DEFAULT_MODEL", DEFAULT_MODEL)
    model = ChatAnthropic(model=model_name)
    
    # 工具列表
    tools = [web_search, web_extract]
    
    # 创建 react agent，使用 ResearchState
    agent = create_react_agent(model, tools, state_schema=ResearchState)
    return agent


# 导出 agent 供 langgraph dev 使用
agent = create_graph()

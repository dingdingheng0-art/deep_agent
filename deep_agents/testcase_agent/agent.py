"""测试用例生成 Agent — LangGraph 服务端图工厂。

langgraph.json 中注册为 "testcase_agent" 图：
    "testcase_agent": "./deep_agents/testcase_agent/agent.py:make_agent"

架构：主 Agent（编排 + 定稿 + 导出）+ 3 个子代理（需求分析 / 用例设计 / 评审），
文件上传经 DoclingParseMiddleware 解析后进入虚拟文件系统 /uploads/，
导出产物落盘 ./exports/（经 CompositeBackend 的 /exports/ 路由 + 自定义下载路由）。

技能（Skills）：领域方法论以渐进式披露的 SKILL.md 形式存放在
testcase_agent/skills/（磁盘），经 CompositeBackend 的 /skills/ 路由挂载，
主 Agent 与各子代理按需 read_file 加载（见 deepagents SkillsMiddleware）：
    requirement-testpoint-mining  需求测试点挖掘（→ requirement-analyzer）
    test-design-techniques        用例设计方法库（→ testcase-designer）
    testcase-review               用例评审方法论（→ testcase-reviewer）
    testcase-delivery             交付导出规范（→ 主 Agent）

环境变量（见 .env）：
    TESTCASE_MODEL         主/子 Agent 使用的模型（默认 anthropic:claude-sonnet-5-5）
    TESTCASE_WORKSPACE_DIR 工作空间真实磁盘路径（默认 ./workspace）
    DOCLING_BASE_URL       docling-serve 地址（默认 http://43.155.235.44:5001）
    DOCLING_SERVE_API_KEY  可选
    EXPORT_BASE_URL        下载链接前缀（默认 http://127.0.0.1:2024）
"""

import asyncio
import os
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend
from dotenv import load_dotenv
from langchain.agents.middleware import TodoListMiddleware

from deep_agents.testcase_agent.middleware import DoclingParseMiddleware, WorkspaceFilesystemMiddleware
from deep_agents.testcase_agent.prompts import (
    ANALYZER_PROMPT,
    DESIGNER_PROMPT,
    MAIN_PROMPT,
    REVIEWER_PROMPT,
)
from deep_agents.testcase_agent.state import TestcaseAgentState
from deep_agents.testcase_agent.tools import EXPORTS_DIR, export_excel, export_xmind

load_dotenv()

MODEL = os.environ.get("TESTCASE_MODEL", "anthropic:claude-sonnet-5-5")

# 工作空间：真实磁盘目录，子代理写入的文件主 Agent 可见
WORKSPACE_DIR = Path(os.environ.get(
    "TESTCASE_WORKSPACE_DIR",
    str(Path(__file__).resolve().parent.parent / "workspace"),
))

# 技能库磁盘目录（挂载为虚拟文件系统的 /skills/，内含各技能的 SKILL.md）
SKILLS_DIR = Path(__file__).resolve().parent / "skills"
SKILLS_SOURCE = "/skills/"

# 声明式子代理必须显式给出 model 与 tools（不会自动继承主 agent）。
# tools=[]：子代理只用内置文件工具（ls/read_file/write_file/edit_file/glob/grep），
# 导出动作统一由主 agent 执行。
# skills=：为每个子代理挂载专属方法论技能（渐进式披露，按需 read_file 加载）。
SUBAGENTS = [
    {
        "name": "requirement-analyzer",
        "description": "需求分析专家：阅读 /uploads/ 下解析后的需求文档，提取功能点与测试点清单，写入 /analysis/test_points.md",
        "system_prompt": ANALYZER_PROMPT,
        "model": MODEL,
        "tools": [],
        "skills": [SKILLS_SOURCE],
    },
    {
        "name": "testcase-designer",
        "description": "用例设计专家：基于 /analysis/test_points.md 设计完整测试用例（等价类/边界值/场景法等），写入 /testcases/testcases.md",
        "system_prompt": DESIGNER_PROMPT,
        "model": MODEL,
        "tools": [],
        "skills": [SKILLS_SOURCE],
    },
    {
        "name": "testcase-reviewer",
        "description": "用例评审专家：审查 /testcases/testcases.md 的覆盖率、正确性与可执行性，评审意见写入 /review/review.md",
        "system_prompt": REVIEWER_PROMPT,
        "model": MODEL,
        "tools": [],
        "skills": [SKILLS_SOURCE],
    },
]


def _build_agent():
    # 工作空间必须存在
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    backend = CompositeBackend(
        # 默认 FilesystemBackend：子代理与主 Agent 文件共享
        default=FilesystemBackend(root_dir=str(WORKSPACE_DIR)),
        routes={
            # 精确路由优先（/skills/ 不被 / 覆盖）
            SKILLS_SOURCE: FilesystemBackend(root_dir=str(SKILLS_DIR)),
            # /exports/ 路由到真实磁盘，供自定义路由提供 xlsx/xmind/md 下载
            "/exports/": FilesystemBackend(root_dir=str(EXPORTS_DIR)),
        },
    )
    return create_deep_agent(
        model=MODEL,
        tools=[export_excel, export_xmind],
        system_prompt=MAIN_PROMPT,
        # DoclingParseMiddleware 需持有 backend，才能把解析结果写进 agent 读到的同一 VFS
        middleware=[WorkspaceFilesystemMiddleware(WORKSPACE_DIR), DoclingParseMiddleware(backend), TodoListMiddleware()],
        subagents=SUBAGENTS,
        backend=backend,
        skills=[SKILLS_SOURCE],
        state_schema=TestcaseAgentState,
        name="testcase-agent",
    )


async def make_agent():
    """LangGraph 服务端 graph factory（langgraph.json: "./testcase_agent/agent.py:make_agent"）。

    create_deep_agent 内部有同步文件系统调用，在 langgraph dev 的 ASGI
    事件循环里会被 blockbuster 拦截，放到线程里执行（同 research_agent.py）。
    """
    return await asyncio.to_thread(_build_agent)


async def main():
    """本地调试入口：uv run python -m testcase_agent.agent"""
    import base64
    import sys

    agent = _build_agent()

    uploaded_files = []
    if len(sys.argv) > 1:
        path = sys.argv[1]
        with open(path, "rb") as f:
            uploaded_files.append(
                {
                    "filename": os.path.basename(path),
                    "mime_type": "application/octet-stream",
                    "data_base64": base64.b64encode(f.read()).decode(),
                }
            )

    result = await agent.ainvoke(
        {
            "messages": [
                {"role": "user", "content": "请根据我上传的需求文档生成完整的测试用例。"}
            ],
            "uploaded_files": uploaded_files,
        },
        config={"recursion_limit": 200},
    )
    print(result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())

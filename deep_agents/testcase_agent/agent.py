"""测试用例生成 Agent — LangGraph 服务端图工厂。

langgraph.json 中注册为 "testcase_agent" 图：
    "testcase_agent": "./deep_agents/testcase_agent/agent.py:make_agent"

架构：主 Agent（编排 + 定稿 + 导出）+ 3 个子代理（需求分析 / 用例设计 / 评审），
文件上传经 DoclingParseMiddleware 解析后进入虚拟文件系统 /uploads/，
导出产物落盘 ./exports/（经 CompositeBackend 的 /exports/ 路由 + 自定义下载路由）。

    10|技能（Skills）：领域方法论以渐进式披露的 SKILL.md 形式存放在
testcase_agent/skills/（磁盘），经 CompositeBackend 的 /skills/ 路由挂载，
主 Agent 与各子代理按需 read_file 加载（见 deepagents SkillsMiddleware）：
    requirement-testpoint-mining  需求测试点挖掘（→ requirement-analyzer）
    test-design-techniques        用例设计方法库（→ testcase-designer）
    testcase-review               用例评审方法论（→ testcase-reviewer）
    testcase-delivery             交付导出规范（→ 主 Agent）

环境变量（见 .env）：
    TESTCASE_MODEL         主/子 Agent 使用的模型（默认 anthropic:claude-sonnet-5-5）
    20|    TESTCASE_WORKSPACE_DIR 工作空间真实磁盘路径（默认 ./workspace）
    TESTCASE_MAX_TOKENS    DeepSeek 输出上限（默认 8192）
    TESTCASE_THINKING      thinking 开关，默认 disabled
    DOCLING_BASE_URL       docling-serve 地址（默认 http://43.155.235.44:5001）
    DOCLING_SERVE_API_KEY  可选
    EXPORT_BASE_URL        下载链接前缀（默认 http://127.0.0.1:2024）
    30|
    TOKEN 优化相关（新增）：
    MAX_MESSAGES            最大保留消息数（默认 30）
    SUMMARY_THRESHOLD       触发裁剪的消息数阈值（默认 25）
    STRIP_THINKING         是否移除 thinking 块（默认 true）
    MAX_SIGNATURE_CHARS    签名最大字符数（默认 500）
"""

import asyncio
import os
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend
from dotenv import load_dotenv
from langchain.agents.middleware import TodoListMiddleware

from deep_agents.testcase_agent.middleware import (
    DoclingParseMiddleware,
    WriteGuardMiddleware,
    WorkspaceFilesystemMiddleware,
)
from deep_agents.testcase_agent.prompts import (
    ANALYZER_PROMPT,
    DESIGNER_PROMPT,
    MAIN_PROMPT,
    REVIEWER_PROMPT,
)
from deep_agents.testcase_agent.state import TestcaseAgentState
from deep_agents.testcase_agent.token_optimization import (
    IdempotentSubagentMiddleware,
    MessageTrimmingMiddleware,
    ThinkingStripperMiddleware,
)
from deep_agents.testcase_agent.tools import (
    EXPORTS_DIR,
    export_xmind,
    make_append_file,
    make_export_tools,
)
from deepagents.middleware.skills import SkillsMiddleware

load_dotenv()

MODEL_NAME = os.environ.get("TESTCASE_MODEL", "anthropic:claude-sonnet-5-5")


def _build_model():
    """构建主/子代理共用的模型实例，统一配置 max_tokens 和 thinking 参数。

    根据 TESTCASE_MODEL 的前缀选择客户端：
      - deepseek:*   → ChatDeepSeek（调用 DeepSeek API）
      - anthropic:* → ChatAnthropic（Kimi 兼容接口，调用 api.kimi.com）

    同时注入一个自定义 httpx2.AsyncClient，
    70|    解决 Windows + httpx2 环境下长连接 'Bad Record MAC' SSL 错误。
    """
    import certifi
    import ssl

    from httpx2 import AsyncClient, Limits, Timeout

    # 显式使用 certifi CA 证书；设置较短 keepalive_expiry 避免 TLS 会话票据腐坏
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    http_client = AsyncClient(
        timeout=Timeout(120.0),
        limits=Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=30.0),
        trust_env=True,
        verify=ssl_context,
    )

    # 从 "deepseek:deepseek-chat" 或 "anthropic:kimi-k3" 提取 provider 和模型名
    raw = MODEL_NAME
    provider, model = (raw.split(":", 1) + [""])[0], (raw.split(":", 1) + [""])[1]
    if ":" in raw:
        model = raw.split(":", 1)[1]
    else:
        provider, model = "", raw

    max_tokens = int(os.environ.get("TESTCASE_MAX_TOKENS", "8192"))

    if provider == "anthropic":
        # Kimi 的 Claude 兼容接口；使用默认 httpx 客户端（独立连接池，不会复用 DeepSeek 的连接）
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=model,
            max_tokens=max_tokens,
            anthropic_api_url=os.environ.get("ANTHROPIC_BASE_URL"),
        )
    else:
        # DeepSeek（默认）
        from langchain_deepseek import ChatDeepSeek
        thinking_cfg: dict | None = None
        thinking_val = os.environ.get("TESTCASE_THINKING", "disabled")
        if thinking_val.lower() not in ("disabled", "false", "0"):
            thinking_cfg = {"type": "enabled", "budget_tokens": 8192}
        extra: dict = {}
        if thinking_cfg is not None:
            extra["thinking"] = thinking_cfg
        # 支持自定义 base_url（如 8888ok 等第三方代理）
        deepseek_base_url = os.environ.get("DEEPSEEK_BASE_URL")
        if deepseek_base_url:
            extra["base_url"] = deepseek_base_url
        return ChatDeepSeek(
            model=model,
            max_tokens=max_tokens,
            http_async_client=http_client,
            **(extra if extra else {}),
        )


# 工作空间：真实磁盘目录，子代理写入的文件主 Agent 可见
WORKSPACE_DIR = Path(os.environ.get(
    "TESTCASE_WORKSPACE_DIR",
    str(Path(__file__).resolve().parent.parent / "workspace"),
))

# 技能库磁盘目录（挂载为虚拟文件系统的 /skills/，内含各技能的 SKILL.md）
SKILLS_DIR = Path(__file__).resolve().parent / "skills"
SKILLS_SOURCE = "/skills/"


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

    # 构建 append_file 工具（共享给主 agent 和子代理）
    append_file = make_append_file(backend)

    model = _build_model()

    # ============================================================
    # 优化 1: 主 agent 工具——新增脚本化导出，移除 LLM 手工搬运
    # ============================================================
    # 使用工厂函数创建绑定 backend 的导出工具
    export_from_markdown, export_xmind_from_markdown, generate_coverage_report = make_export_tools(backend)

    main_tools = [
        export_from_markdown,      # 脚本化：从 Markdown 直接导出 Excel
        export_xmind_from_markdown,  # 脚本化：从 Markdown 直接导出 XMind
        export_xmind,              # 保留：用于需要指定 tree 参数的场景
        generate_coverage_report,  # 脚本化：生成覆盖矩阵
        append_file,
    ]

    # ============================================================
    # 优化 2: Token 优化中间件
    # ============================================================
    # 消息裁剪：防止历史只增不减
    trimming_middleware = MessageTrimmingMiddleware(
        max_messages=30,
        summary_threshold=25,
    )

    # Thinking 移除：减少输出体积
    thinking_stripper = ThinkingStripperMiddleware(
        strip_thinking=True,
        max_signature_chars=500,
    )

    main_middleware = [
        WorkspaceFilesystemMiddleware(WORKSPACE_DIR),
        DoclingParseMiddleware(backend),
        TodoListMiddleware(),
        WriteGuardMiddleware(),
        # Token 优化中间件
        trimming_middleware,
        thinking_stripper,
    ]

    # ============================================================
    # 优化 3: 子代理幂等检查中间件
    # ============================================================
    idempotent_subagent = IdempotentSubagentMiddleware(
        backend=backend,
        product_paths={
            "requirement-analyzer": "/analysis/test_points.md",
            "testcase-designer": "/testcases/testcases.md",
            "testcase-reviewer": "/review/review.md",
        },
        min_file_size=200,  # 最小有效文件大小
    )

    # 子代理：它们才是写长文档的人，每个都挂 append_file
    # 注意：子代理不继承主 agent 的 SkillsMiddleware（skills=[]），
    # 避免重复加载；技能路径直接嵌入 prompt 中按需 read_file。
    subagents = [
        {
            "name": "requirement-analyzer",
            "description": "需求分析专家：阅读 /uploads/ 下解析后的需求文档，提取功能点与测试点清单，写入 /analysis/test_points.md",
            "system_prompt": ANALYZER_PROMPT,
            "model": model,
            "tools": [append_file],
            "skills": [],  # 不自动加载，通过 prompt 内 read_file 按需加载
        },
        {
            "name": "testcase-designer",
            "description": "用例设计专家：基于 /analysis/test_points.md 设计完整测试用例（等价类/边界值/场景法等），写入 /testcases/testcases.md",
            "system_prompt": DESIGNER_PROMPT,
            "model": model,
            "tools": [append_file],
            "skills": [],  # 同上
        },
        {
            "name": "testcase-reviewer",
            "description": "用例评审专家：审查 /testcases/testcases.md 的覆盖率、正确性与可执行性，评审意见写入 /review/review.md",
            "system_prompt": REVIEWER_PROMPT,
            "model": model,
            "tools": [append_file],
            "skills": [],  # 同上
        },
    ]

    return create_deep_agent(
        model=model,
        tools=main_tools,
        system_prompt=MAIN_PROMPT,
        middleware=main_middleware,
        subagents=subagents,
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

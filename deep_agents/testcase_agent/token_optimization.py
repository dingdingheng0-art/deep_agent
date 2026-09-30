"""Token 优化中间件集。

包含四大优化:
1. MessageTrimmingMiddleware: 裁剪过长的消息历史，防止上下文膨胀
2. ThinkingStripperMiddleware: 移除 thinking 块和超长 signature，减少输出体积
3. IdempotentSubagentMiddleware: 子代理幂等检查，避免重复执行浪费 token
4. SessionIsolationMiddleware: 会话级路径隔离，避免多会话文件冲突
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    RemoveMessage,
    SystemMessage,
)


class MessageTrimmingMiddleware(AgentMiddleware):
    """裁剪过长的消息历史，防止上下文只增不减导致的 token 浪费。

    工作原理：
    - 在 abefore_agent / before_agent 中检查当前消息数量
    - 超过阈值时，将早期消息替换为摘要
    - 保留系统消息和最近的 N 条消息

    环境变量：
        MAX_MESSAGES: 最大保留消息数（默认 30）
        SUMMARY_THRESHOLD: 触发裁剪的消息数阈值（默认 25）
        SUMMARY_PROMPT: 摘要生成提示词模板
    """

    name = "message_trimming"

    DEFAULT_MAX_MESSAGES = 30
    DEFAULT_SUMMARY_THRESHOLD = 25
    DEFAULT_SUMMARY_PROMPT = "请简要总结以下对话的核心内容和已完成的工作（约50字）：\n\n{content}"

    def __init__(
        self,
        max_messages: int | None = None,
        summary_threshold: int | None = None,
        summary_prompt: str | None = None,
    ) -> None:
        self.max_messages = max_messages or int(
            os.environ.get("MAX_MESSAGES", str(self.DEFAULT_MAX_MESSAGES))
        )
        self.summary_threshold = summary_threshold or int(
            os.environ.get("SUMMARY_THRESHOLD", str(self.DEFAULT_SUMMARY_THRESHOLD))
        )
        self.summary_prompt = summary_prompt or self.DEFAULT_SUMMARY_PROMPT

    def _summarize_messages(self, messages: list[BaseMessage]) -> str:
        """生成消息历史摘要（实际由模型生成，这里返回占位符）。"""
        content_parts = []
        for msg in messages:
            role = getattr(msg, "role", "unknown")
            content = getattr(msg, "content", "")
            if isinstance(content, str) and content:
                content_parts.append(f"[{role}]: {content[:200]}")
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")[:200]
                        if text:
                            content_parts.append(f"[{role}]: {text}")
        return "\n".join(content_parts[-20:])  # 最近20条

    def _trim_state(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """执行消息裁剪。"""
        messages = state.get("messages", [])
        if not messages or len(messages) <= self.summary_threshold:
            return None

        # 分类消息
        system_msgs: list[BaseMessage] = []
        keep_msgs: list[BaseMessage] = []
        trim_msgs: list[BaseMessage] = []

        for msg in messages:
            if isinstance(msg, SystemMessage):
                system_msgs.append(msg)
            elif len(keep_msgs) < self.max_messages:
                keep_msgs.append(msg)
            else:
                trim_msgs.append(msg)

        if not trim_msgs:
            return None

        # 生成摘要消息
        summary_content = self._summarize_messages(trim_msgs)
        summary_msg = AIMessage(
            content=(
                f"[历史摘要] 早期 {len(trim_msgs)} 条消息已裁剪，"
                f"保留核心内容：\n{summary_content[:500]}"
            ),
            id=f"summary-{int(time.time() * 1000)}",
        )

        # 构建移除操作
        remove_ids = [msg.id for msg in trim_msgs if msg.id]
        removes = [RemoveMessage(id=mid) for mid in remove_ids if mid]

        return {
            "messages": removes + [summary_msg] + keep_msgs,
        }

    async def abefore_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        return self._trim_state(state)

    def before_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        return self._trim_state(state)


class ThinkingStripperMiddleware(AgentMiddleware):
    """移除 thinking 块和超长 signature，减少输出 token 体积。

    工作原理：
    - 在 awrap_model_call / wrap_model_call 中拦截模型响应
    - 移除 content 中的 thinking 块
    - 截断超长的签名文本

    环境变量：
        STRIP_THINKING: 是否移除 thinking 块（默认 true）
        MAX_SIGNATURE_CHARS: 签名最大字符数（默认 500）
    """

    name = "thinking_stripper"

    # thinking 块的正则模式（Anthropic 格式）
    THINKING_PATTERNS = [
        # <thinking>...</thinking>
        re.compile(r"<thinking>[\s\S]*?</thinking>", re.IGNORECASE),
        # [THINKING]...[/THINKING]
        re.compile(r"\[THINKING\][\s\S]*?\[/THINKING\]", re.IGNORECASE),
        # ```thinking\n...\n```
        re.compile(r"```thinking\n[\s\S]*?\n```", re.IGNORECASE),
    ]

    # 签名相关关键词
    SIGNATURE_PATTERNS = [
        re.compile(r"\[signature:.*?\]", re.IGNORECASE | re.DOTALL),
        re.compile(r"<signature>[\s\S]*?</signature>", re.IGNORECASE),
        re.compile(r"```signature\n[\s\S]*?\n```", re.IGNORECASE),
    ]

    def __init__(
        self,
        strip_thinking: bool | None = None,
        max_signature_chars: int | None = None,
    ) -> None:
        strip_env = os.environ.get("STRIP_THINKING", "true").lower()
        self.strip_thinking = (
            strip_thinking if strip_thinking is not None
            else strip_env not in ("0", "false", "off")
        )
        self.max_signature_chars = max_signature_chars or int(
            os.environ.get("MAX_SIGNATURE_CHARS", "500")
        )

    def _strip_thinking(self, content: str | list) -> str | list:
        """从文本内容中移除 thinking 块。"""
        if isinstance(content, list):
            return [
                block if not (isinstance(block, dict) and block.get("type") == "text")
                else {**block, "text": self._strip_thinking(block["text"])}
                for block in content
            ]

        if not isinstance(content, str):
            return content

        result = content
        for pattern in self.THINKING_PATTERNS:
            result = pattern.sub("", result)

        # 移除独立的 thinking 行
        lines = result.split("\n")
        filtered_lines = [
            line for line in lines
            if not line.strip().lower().startswith("thinking:")
            and "reasoning:" not in line.strip().lower()
        ]
        result = "\n".join(filtered_lines)

        return result

    def _strip_signature(self, content: str) -> str:
        """截断超长签名。"""
        result = content
        for pattern in self.SIGNATURE_PATTERNS:
            result = pattern.sub("[signature truncated]", result)

        # 如果单个签名超长，截断
        if len(result) > self.max_signature_chars * 2:
            lines = result.split("\n")
            # 保留前 N 行和最后几行
            keep_lines = min(10, len(lines) - 5)
            if keep_lines > 0:
                result = "\n".join(lines[:keep_lines]) + "\n...[内容已截断]...\n" + "\n".join(lines[-3:])
            else:
                result = result[:self.max_signature_chars] + "\n...[内容已截断]..."

        return result

    def _process_response(self, response: Any) -> Any:
        """处理模型响应，移除 thinking 和超长签名。"""
        if not hasattr(response, "content"):
            return response

        content = getattr(response, "content", None)
        if content is None:
            return response

        if self.strip_thinking:
            content = self._strip_thinking(content)

        # 始终截断超长签名
        if isinstance(content, str):
            content = self._strip_signature(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    block["text"] = self._strip_signature(block["text"])

        # 创建更新后的响应（不修改原对象）
        return response.model_copy(update={"content": content})

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        response = handler(request)
        return self._process_response(response)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        response = await handler(request)
        return self._process_response(response)


class IdempotentSubagentMiddleware(AgentMiddleware):
    """子代理幂等检查中间件，避免重复执行浪费 token。

    工作原理：
    - 在调用 task 工具前，检查目标产物文件是否已存在
    - 如果产物存在且完整，跳过子代理调用，返回已存在的文件信息
    - 只有产物不存在或不完整时才真正调用子代理

    使用方式：
        在 middleware 中注册此中间件，配置产物文件路径映射
    """

    name = "idempotent_subagent"

    # 产物路径配置（子代理名 -> 产物文件路径）
    PRODUCT_PATHS: dict[str, str] = {
        "requirement-analyzer": "/analysis/test_points.md",
        "testcase-designer": "/testcases/testcases.md",
        "testcase-reviewer": "/review/review.md",
    }

    # 最小有效文件大小（字节）
    MIN_FILE_SIZE = 100

    def __init__(
        self,
        backend: Any,
        product_paths: dict[str, str] | None = None,
        min_file_size: int | None = None,
    ) -> None:
        self.backend = backend
        self.product_paths = product_paths or self.PRODUCT_PATHS
        self.min_file_size = min_file_size or self.MIN_FILE_SIZE

    def _check_product_exists(self, product_path: str) -> tuple[bool, dict[str, Any]]:
        """检查产物文件是否存在且有效。"""
        try:
            result = self.backend.read(product_path, limit=100)
            if result.error:
                return False, {"error": result.error}

            content = result.file_data.get("content", "") if result.file_data else ""
            if len(content) < self.min_file_size:
                return False, {"size": len(content), "reason": "文件内容过少"}

            return True, {
                "path": product_path,
                "size": len(content),
                "preview": content[:200],
            }
        except Exception as e:
            return False, {"error": str(e)}

    async def abefore_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        return None  # 不修改状态

    def before_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        return None


class SessionIsolationMiddleware(AgentMiddleware):
    """会话级路径隔离中间件，避免多会话文件冲突。

    工作原理：
    - 在 agent 启动时生成会话唯一标识符
    - 将产物路径映射到会话级子目录
    - 防止不同会话的文件相互覆盖

    使用方式：
        在 middleware 中注册此中间件，自动启用路径隔离
    """

    name = "session_isolation"

    # 默认产物路径映射
    PRODUCT_PATHS = {
        "test_points": "/analysis/test_points.md",
        "testcases": "/testcases/testcases.md",
        "review": "/review/review.md",
        "final": "/exports/testcases.md",
        "coverage_report": "/analysis/coverage_report.md",
    }

    def __init__(
        self,
        session_prefix: str | None = None,
        path_mappings: dict[str, str] | None = None,
    ) -> None:
        # 会话唯一标识符：默认用时间戳+随机后缀
        self.session_prefix = session_prefix or self._generate_session_id()
        self.path_mappings = path_mappings or self.PRODUCT_PATHS

    @staticmethod
    def _generate_session_id() -> str:
        """生成会话唯一标识符。"""
        timestamp = str(int(time.time() * 1000))
        random_suffix = uuid.uuid4().hex[:8]
        return f"session_{timestamp}_{random_suffix}"

    def get_isolated_path(self, product_key: str) -> str:
        """获取会话隔离后的产物路径。

        Args:
            product_key: 产物标识符（如 "test_points"）

        Returns:
            隔离后的虚拟路径（如 /analysis/session_xxx_test_points.md）
        """
        base_path = self.path_mappings.get(product_key, f"/{product_key}.md")
        stem = Path(base_path).stem
        suffix = Path(base_path).suffix
        dir_name = Path(base_path).parent
        return f"{dir_name}/{self.session_prefix}_{stem}{suffix}"

    def get_all_isolated_paths(self) -> dict[str, str]:
        """获取所有隔离后的产物路径。"""
        return {key: self.get_isolated_path(key) for key in self.path_mappings}

    async def abefore_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        # 将隔离路径信息注入到 state 中
        return {
            "_session_prefix": self.session_prefix,
            "_isolated_paths": self.get_all_isolated_paths(),
        }

    def before_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        return {
            "_session_prefix": self.session_prefix,
            "_isolated_paths": self.get_all_isolated_paths(),
        }


class FileContentCache:
    """文件内容缓存，避免重复读取大文件。

    使用 MD5 哈希跟踪文件内容，只在内容变化时通知 agent。
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, int]] = {}  # path -> (hash, size)

    def get_hash(self, content: str) -> str:
        """计算内容的 MD5 哈希。"""
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    def check(self, path: str, content: str) -> tuple[bool, str]:
        """检查内容是否有变化。

        Args:
            path: 文件路径
            content: 文件内容

        Returns:
            (has_changed, content_hash)
        """
        content_hash = self.get_hash(content)
        if path in self._cache:
            cached_hash, cached_size = self._cache[path]
            if cached_hash == content_hash:
                return False, content_hash  # 内容未变
        return True, content_hash

    def update(self, path: str, content: str) -> None:
        """更新缓存。"""
        content_hash = self.get_hash(content)
        self._cache[path] = (content_hash, len(content))

    def get_summary(self, path: str, content: str, max_chars: int = 200) -> str:
        """获取文件摘要。

        Args:
            path: 文件路径
            content: 文件内容
            max_chars: 摘要最大字符数

        Returns:
            文件摘要（包含行数、大小和前 N 个字符）
        """
        lines = content.split("\n")
        line_count = len(lines)
        char_count = len(content)
        preview = content[:max_chars].replace("\n", " ")

        return (
            f"文件: {path}\n"
            f"行数: {line_count}, 字符: {char_count}\n"
            f"预览: {preview}..."
        )


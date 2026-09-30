"""Docling 文件解析中间件。

用户在前端上传的文件以 base64 形式通过图输入的 ``uploaded_files`` 通道传入。
本中间件在 agent 启动前（before_agent / abefore_agent）将其发送到
docling-serve 解析为 Markdown，写入虚拟文件系统的 ``/uploads/`` 目录，
从而作为上下文供大模型（经 read_file 等文件工具）使用。

环境变量：
    DOCLING_SERVE_URL / DOCLING_BASE_URL
                                 docling-serve 服务地址（默认 http://43.155.235.44:5001）
    DOCLING_SERVE_API_KEY        可选，docling-serve 启用鉴权时的 X-Api-Key
    DOCLING_PICTURE_DESCRIPTION  是否启用图片描述（多模态），默认 true
    DOCLING_VLM_URL              图片描述用的 OpenAI 兼容视觉模型端点
    DOCLING_VLM_API_KEY          视觉模型 API Key
    DOCLING_VLM_MODEL            视觉模型名（默认 qwen3.8-flash）

注意：服务端必须以 DOCLING_SERVE_ENABLE_REMOTE_SERVICES=true 启动，
picture_description_api 才会生效。

输入来源（任一即可）：
    - state.uploaded_files（CLI / 图输入通道，含 data_base64）
    - 最近 human 消息中的 <<<FILE upload="...">>>（前端暂存引用）
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path, PurePath
from typing import Any

import httpx
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from deep_agents.testcase_agent.file_utils import extract_uploaded_files_from_messages

DEFAULT_DOCLING_URL = "http://43.155.235.44:5001"

DEFAULT_VLM_URL = (
    "https://llm-dea9z7nvzanzylcv.cn-beijing.maas.aliyuncs.com"
    "/compatible-mode/v1/chat/completions"
)
DEFAULT_VLM_MODEL = "qwen3.8-flash"
DEFAULT_VLM_PROMPT = (
    "用简洁的中文描述这张图片的内容。"
    "如果是图表，请说明图表类型、坐标轴含义和关键数据趋势；"
    "如果是流程图，请说明各个节点和流转关系；"
    "如果是界面截图，请说明界面包含的主要元素和功能。"
)


class WorkspaceFilesystemMiddleware(AgentMiddleware):
    """在 agent 启动前预先挂载工作空间目录到虚拟文件系统。

    DoclingParseMiddleware 将解析后的 .md 写入 /uploads/，
    子代理生成的文件写入 /analysis/、/testcases/、/review/。
    如果工作空间目录下这些子目录不存在，FilesystemMiddleware 的
    write_file 可能在某些后端实现下失败。这里主动 mkdir，
    同时确保与 create_deep_agent 使用同一个 FilesystemBackend 实例。
    """

    name = "workspace_filesystem"

    def __init__(self, workspace_dir: Path | str) -> None:
        self.workspace_dir = Path(workspace_dir)

    async def abefore_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        # mkdir 是同步 IO，放到线程里避开 langgraph blockbuster 对事件循环的拦截
        await asyncio.to_thread(self.workspace_dir.mkdir, parents=True, exist_ok=True)
        return None

    def before_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        return None


class DoclingParseMiddleware(AgentMiddleware):
    """把 uploaded_files 中的 base64 文件解析为 Markdown 并写入虚拟文件系统。

    解析结果通过注入的 ``backend`` 落盘到 ``/uploads/``（与 agent 的
    read_file/ls 等工具读取的是同一个 backend），而不是返回
    ``state["files"]``：``files`` channel 仅在 backend 为 StateBackend 时才存在，
    本 agent 使用真实磁盘的 CompositeBackend(FilesystemBackend)，返回该字段会被丢弃。
    """

    name = "docling_parse"

    def __init__(
        self,
        backend: Any,
        base_url: str | None = None,
        api_key: str | None = None,
        poll_interval: float = 2.0,
        timeout: float = 600.0,
    ) -> None:
        self.backend = backend
        self.base_url = (
            base_url
            or os.environ.get("DOCLING_SERVE_URL")
            or os.environ.get("DOCLING_BASE_URL")
            or DEFAULT_DOCLING_URL
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("DOCLING_SERVE_API_KEY")
        self.poll_interval = poll_interval
        self.timeout = timeout
        # 图片描述（多模态）配置
        self.picture_description = (
            os.environ.get("DOCLING_PICTURE_DESCRIPTION", "true").lower()
            not in ("0", "false", "off")
        )
        self.vlm_url = os.environ.get("DOCLING_VLM_URL") or DEFAULT_VLM_URL
        self.vlm_api_key = os.environ.get("DOCLING_VLM_API_KEY")
        self.vlm_model = os.environ.get("DOCLING_VLM_MODEL") or DEFAULT_VLM_MODEL

    # ------------------------------------------------------------------ hooks

    @staticmethod
    def _collect_uploaded(state: dict[str, Any]) -> list[dict[str, Any]]:
        """合并图输入通道与消息内 FILE 引用；同名后者覆盖前者。"""
        by_name: dict[str, dict[str, Any]] = {}
        for item in state.get("uploaded_files") or []:
            name = item.get("filename")
            if name:
                by_name[name] = dict(item)
        for item in extract_uploaded_files_from_messages(state.get("messages") or []):
            name = item.get("filename")
            if name:
                by_name[name] = dict(item)
        return list(by_name.values())

    async def abefore_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        # 磁盘读入放到线程，避开 langgraph blockbuster 对 open/getcwd 的拦截
        uploaded = await asyncio.to_thread(self._collect_uploaded, state)
        if not uploaded:
            return None
        ready = [f for f in uploaded if not f.get("_error") and f.get("data_base64")]
        pre_errors = {
            f["filename"]: RuntimeError(
                str(f.get("_error") or f"缺少文件内容: {f.get('filename')}")
            )
            for f in uploaded
            if f.get("_error") or not f.get("data_base64")
        }
        converted: dict[str, str | BaseException] = dict(pre_errors)
        if ready:
            async with httpx.AsyncClient(timeout=120.0) as client:
                gathered = await asyncio.gather(
                    *(
                        self._convert_async(client, f["filename"], f["data_base64"])
                        for f in ready
                    ),
                    return_exceptions=True,
                )
            for f, result in zip(ready, gathered, strict=True):
                converted[f["filename"]] = result
        filenames = [f["filename"] for f in uploaded]
        results = [converted[name] for name in filenames]
        # 写入 backend（与 ls/read_file 走同一个 VFS）；同时清空 uploaded_files 通道
        written_notes = await self._write_to_backend(filenames, results)
        note_text = (
            "📎 用户上传的文件已解析完成：\n"
            + "\n".join(written_notes)
            + "\n请使用 read_file 读取上述 /uploads/ 下的 Markdown 文件作为需求依据。"
        )
        return {
            "uploaded_files": [],
            "messages": [HumanMessage(content=note_text)],
        }

    def before_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        uploaded = self._collect_uploaded(state)
        if not uploaded:
            return None
        with httpx.Client(timeout=120.0) as client:
            results: list[str | BaseException] = []
            for f in uploaded:
                if f.get("_error"):
                    results.append(RuntimeError(str(f["_error"])))
                    continue
                if not f.get("data_base64"):
                    results.append(RuntimeError(f"缺少文件内容: {f.get('filename')}"))
                    continue
                try:
                    results.append(
                        self._convert_sync(client, f["filename"], f["data_base64"])
                    )
                except Exception as e:  # noqa: BLE001 - 单文件失败不阻断其他文件
                    results.append(e)
        written_notes = self._write_to_backend_sync(
            [f["filename"] for f in uploaded], results
        )
        note_text = (
            "📎 用户上传的文件已解析完成：\n"
            + "\n".join(written_notes)
            + "\n请使用 read_file 读取上述 /uploads/ 下的 Markdown 文件作为需求依据。"
        )
        return {
            "uploaded_files": [],
            "messages": [HumanMessage(content=note_text)],
        }

    # ------------------------------------------------------------- docling 调用

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key} if self.api_key else {}

    def _payload(self, filename: str, data_base64: str) -> dict[str, Any]:
        options: dict[str, Any] = {"to_formats": ["md"]}
        if self.picture_description and self.vlm_api_key:
            options["do_picture_description"] = True
            options["picture_description_area_threshold"] = 0.02
            options["picture_description_api"] = {
                "url": self.vlm_url,
                "headers": {"Authorization": f"Bearer {self.vlm_api_key}"},
                "params": {"model": self.vlm_model, "max_completion_tokens": 2000},
                "timeout": 90,
                "concurrency": 2,
                "prompt": DEFAULT_VLM_PROMPT,
            }
        return {
            "sources": [
                {"kind": "file", "base64_string": data_base64, "filename": filename}
            ],
            "options": options,
        }

    async def _convert_async(
        self, client: httpx.AsyncClient, filename: str, data_base64: str
    ) -> str:
        resp = await client.post(
            f"{self.base_url}/v1/convert/source/async",
            json=self._payload(filename, data_base64),
            headers=self._headers,
        )
        resp.raise_for_status()
        task_id = resp.json()["task_id"]

        deadline = time.monotonic() + self.timeout
        while True:
            await asyncio.sleep(self.poll_interval)
            status = (
                await client.get(
                    f"{self.base_url}/v1/status/poll/{task_id}", headers=self._headers
                )
            ).json()
            task_status = status.get("task_status")
            if task_status == "success":
                break
            if task_status == "failure":
                raise RuntimeError(f"docling 任务失败: {status}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"docling 解析超时（{self.timeout:.0f}s），task_id={task_id}")

        result = (
            await client.get(
                f"{self.base_url}/v1/result/{task_id}", headers=self._headers
            )
        ).json()
        return self._extract_markdown(result)

    def _convert_sync(self, client: httpx.Client, filename: str, data_base64: str) -> str:
        resp = client.post(
            f"{self.base_url}/v1/convert/source/async",
            json=self._payload(filename, data_base64),
            headers=self._headers,
        )
        resp.raise_for_status()
        task_id = resp.json()["task_id"]

        deadline = time.monotonic() + self.timeout
        while True:
            time.sleep(self.poll_interval)
            status = client.get(
                f"{self.base_url}/v1/status/poll/{task_id}", headers=self._headers
            ).json()
            task_status = status.get("task_status")
            if task_status == "success":
                break
            if task_status == "failure":
                raise RuntimeError(f"docling 任务失败: {status}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"docling 解析超时（{self.timeout:.0f}s），task_id={task_id}")

        result = client.get(
            f"{self.base_url}/v1/result/{task_id}", headers=self._headers
        ).json()
        return self._extract_markdown(result)

    @staticmethod
    def _extract_markdown(result: dict[str, Any]) -> str:
        md = (result.get("document") or {}).get("md_content")
        if not md:
            raise RuntimeError(
                f"docling 返回内容为空: status={result.get('status')}, "
                f"errors={result.get('errors')}"
            )
        return md

    # ------------------------------------------------------------- 状态更新构造

    @staticmethod
    def _render_note(filename: str, result: str | BaseException) -> tuple[str, str, str]:
        """返回 (virtual_path, content, note_line)。"""
        stem = PurePath(filename).stem or "upload"
        if isinstance(result, BaseException):
            path = f"/uploads/{stem}.error.md"
            content = f"# 文件解析失败\n\n原始文件：{filename}\n\n错误：{result}"
            note = f"❌ `{filename}` 解析失败（{result}），详情见 `{path}`"
        else:
            path = f"/uploads/{stem}.md"
            content = result
            note = f"✅ `{filename}` → `{path}`（{len(result)} 字符）"
        return path, content, note

    async def _write_to_backend(
        self, filenames: list[str], results: list[str | BaseException]
    ) -> list[str]:
        """异步路径：把每个文件直接写入注入的 backend，并返回提示行列表。"""
        notes: list[str] = []
        for filename, result in zip(filenames, results, strict=True):
            path, content, note = self._render_note(filename, result)
            write_result = await self.backend.awrite(path, content)
            if write_result.error:
                note = f"❌ `{filename}` 写入失败：{write_result.error}"
            notes.append(note)
        return notes

    def _write_to_backend_sync(
        self, filenames: list[str], results: list[str | BaseException]
    ) -> list[str]:
        """同步路径：与异步分支逻辑一致，但走同步 write。"""
        notes: list[str] = []
        for filename, result in zip(filenames, results, strict=True):
            path, content, note = self._render_note(filename, result)
            write_result = self.backend.write(path, content)
            if write_result.error:
                note = f"❌ `{filename}` 写入失败：{write_result.error}"
            notes.append(note)
        return notes


# --------------------------------------------------------------------------------
# WriteGuardMiddleware：截断检测 + 工具错误自解释化
# --------------------------------------------------------------------------------


class WriteGuardMiddleware(AgentMiddleware):
    """防护中间件：处理 max_tokens 截断和工具参数错误。

    a) wrap_model_call / awrap_model_call：输出被截断时，向 AI 提示分段写入策略。
    b) wrap_tool_call / awrap_tool_call：把 write_file/edit_file/append_file 的
       参数错误改写为可执行指引。

    必须同时提供 sync/async 实现：langgraph 服务端走 ainvoke/astream，
    缺少 awrap_* 会触发 NotImplementedError。
    """

    name = "write_guard"

    _WRITE_TOOLS = frozenset({"write_file", "edit_file", "append_file"})
    _TRUNCATE_PATTERNS = ("length", "max_tokens")
    _ERROR_PATTERNS = (
        "Field required",
        "validation",
        "not valid JSON",
        "missing required",
    )
    _GUARD_TEXT = (
        "\n\n[系统提示] 上一次输出因达到 max_tokens 被截断，"
        "工具参数可能不完整。\n"
        "不要原样重试：把内容拆成更小的批次，用 append_file 分段写入（每段 ≤2000 字符）。\n"
        "首次写入用 write_file，后续追加用 append_file。"
    )
    _FIX_TEXT = (
        "错误：参数缺失或被截断（上次输出不完整导致）。\n"
        "修正策略：\n"
        "1. 缩短本次写入内容（建议每段 ≤2000 字符）。\n"
        "2. 长文档改用 append_file 分段追加（首次用 write_file 写开头，后续用 append_file 追加）。\n"
        "3. 禁止在同一轮里并行调用多个 append_file 写入同一文件。"
    )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _extract_ai_message(response: Any) -> Any | None:
        """从 ModelResponse / AIMessage / ExtendedModelResponse 取出 AIMessage。"""
        from langchain_core.messages import AIMessage

        if isinstance(response, AIMessage):
            return response
        result = getattr(response, "result", None)
        if isinstance(result, list) and result:
            first = result[0]
            if isinstance(first, AIMessage):
                return first
        model_response = getattr(response, "model_response", None)
        if model_response is not None:
            return WriteGuardMiddleware._extract_ai_message(model_response)
        return None

    def _apply_truncation_guard(self, response: Any) -> Any:
        """若输出因 max_tokens 截断且含工具调用，在 AIMessage content 前部注入提示。"""
        from dataclasses import replace

        from langchain_core.messages import AIMessage

        ai_msg = self._extract_ai_message(response)
        if ai_msg is None:
            return response

        metadata = getattr(ai_msg, "response_metadata", None) or {}
        finish_reason = metadata.get("finish_reason")
        stop_reason = metadata.get("stop_reason")
        is_truncated = (
            finish_reason in self._TRUNCATE_PATTERNS
            or stop_reason in self._TRUNCATE_PATTERNS
        )
        if not is_truncated:
            return response

        tool_calls = getattr(ai_msg, "tool_calls", None) or []
        content = getattr(ai_msg, "content", None)
        has_tool_call = bool(tool_calls)
        if not has_tool_call and isinstance(content, list):
            has_tool_call = any(
                (isinstance(item, dict) and item.get("type") == "tool_call")
                or getattr(item, "type", None) == "tool_call"
                for item in content
            )
        if not has_tool_call:
            return response

        if isinstance(content, list):
            new_content: list[Any] = [{"type": "text", "text": self._GUARD_TEXT}, *content]
        elif isinstance(content, str):
            new_content = self._GUARD_TEXT + content
        else:
            new_content = self._GUARD_TEXT

        patched = ai_msg.model_copy(update={"content": new_content})

        # 保持原返回类型结构
        if isinstance(response, AIMessage):
            return patched
        result = getattr(response, "result", None)
        if isinstance(result, list) and result:
            new_result = [patched, *result[1:]]
            return replace(response, result=new_result)
        model_response = getattr(response, "model_response", None)
        if model_response is not None:
            patched_mr = self._apply_truncation_guard(model_response)
            return replace(response, model_response=patched_mr)
        return response

    def _rewrite_tool_error(self, request: Any, result: Any) -> Any:
        """把 write/edit/append_file 的参数校验错误改写成可执行指引。"""
        from langchain_core.messages import ToolMessage

        tool_call = getattr(request, "tool_call", None) or {}
        if isinstance(tool_call, dict):
            tool_name = tool_call.get("name") or ""
        else:
            tool_name = getattr(tool_call, "name", "") or ""
        if tool_name not in self._WRITE_TOOLS:
            return result
        if not isinstance(result, ToolMessage):
            return result

        content_str = str(result.content or "")
        if not any(pat in content_str for pat in self._ERROR_PATTERNS):
            return result

        return ToolMessage(
            content=self._FIX_TEXT,
            tool_call_id=result.tool_call_id,
            name=result.name or tool_name,
            status=getattr(result, "status", None) or "error",
        )

    # ------------------------------------------------------------------ hooks

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        response = handler(request)
        return self._apply_truncation_guard(response)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        response = await handler(request)
        return self._apply_truncation_guard(response)

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        result = handler(request)
        return self._rewrite_tool_error(request, result)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        result = await handler(request)
        return self._rewrite_tool_error(request, result)

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
    """把 uploaded_files 中的 base64 文件解析为 Markdown 并注入 state.files。"""

    name = "docling_parse"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        poll_interval: float = 2.0,
        timeout: float = 600.0,
    ) -> None:
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
        results = [converted[f["filename"]] for f in uploaded]
        return self._build_update([f["filename"] for f in uploaded], results)

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
        return self._build_update([f["filename"] for f in uploaded], results)

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
    def _build_update(
        filenames: list[str], results: list[str | BaseException]
    ) -> dict[str, Any]:
        files_update: dict[str, Any] = {}
        notes: list[str] = []
        for filename, result in zip(filenames, results, strict=True):
            stem = PurePath(filename).stem or "upload"
            if isinstance(result, BaseException):
                path = f"/uploads/{stem}.error.md"
                files_update[path] = {
                    "content": f"# 文件解析失败\n\n原始文件：{filename}\n\n错误：{result}",
                    "encoding": "utf-8",
                }
                notes.append(f"❌ `{filename}` 解析失败（{result}），详情见 `{path}`")
            else:
                path = f"/uploads/{stem}.md"
                files_update[path] = {"content": result, "encoding": "utf-8"}
                notes.append(f"✅ `{filename}` → `{path}`（{len(result)} 字符）")
        note_text = (
            "📎 用户上传的文件已解析完成：\n"
            + "\n".join(notes)
            + "\n请使用 read_file 读取上述 /uploads/ 下的 Markdown 文件作为需求依据。"
        )
        return {
            "files": files_update,
            "uploaded_files": [],
            "messages": [HumanMessage(content=note_text)],
        }

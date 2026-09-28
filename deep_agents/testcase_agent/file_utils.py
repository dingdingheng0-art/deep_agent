"""上传文件辅助：解析消息里的 FILE 块，并从 upload_staging 加载字节。

前端流程（deep-agents-ui）：
  1. POST /api/upload → 写入 ``<repo>/testcase_agent/upload_staging/<uuid>/<name>``
  2. 聊天消息只带 ``<<<FILE name="..." upload="<uuid>"></FILE>>>``
  3. 本模块在 DoclingParseMiddleware 中把引用还原为 base64，供 docling 解析

兼容旧版：消息内联 base64 的 ``<<<FILE name="...">...</FILE>>>``。
"""

from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path
from typing import Any

MAX_FILE_SIZE_MB = 20
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

# 与 deep-agents-ui/src/app/api/upload/route.ts 的 STAGING_DIR 对齐。
# 在 import 时一次性 resolve，避免请求路径上调用 Path.resolve()/os.getcwd
# （langgraph dev 的 blockbuster 会拦截事件循环里的 getcwd）。
_REPO_ROOT = Path(__file__).resolve().parents[2]
STAGING_DIR = (_REPO_ROOT / "testcase_agent" / "upload_staging").resolve()

# <<<FILE name="x.pdf" upload="uuid"></FILE>>>
# <<<FILE name="x.pdf" mime="application/pdf">base64...</FILE>>>
_FILE_TAG_RE = re.compile(
    r"<<<FILE\s+name=\"([^\"]+)\""
    r"(?:\s+upload=\"([^\"]+)\")?"
    r"(?:\s+mime=\"([^\"]*)\")?"
    r"\s*>(.*?)</FILE>>>",
    re.DOTALL,
)


def _sanitize_filename(name: str) -> str:
    base = name.replace("\\", "/").split("/")[-1]
    return re.sub(r"^[A-Za-z]:", "", base).strip()


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError):
        return False


def load_staged_bytes(upload_id: str, filename: str) -> bytes:
    """从暂存目录读取文件字节；路径非法或超限时抛 ValueError。

    不调用 Path.resolve()/os.getcwd，避免 ASGI 事件循环被 blockbuster 拦截。
    """
    if not _is_uuid(upload_id):
        raise ValueError(f"非法 upload id: {upload_id!r}")
    safe_name = _sanitize_filename(filename)
    if not safe_name or safe_name in (".", "..") or "/" in safe_name or "\\" in safe_name:
        raise ValueError("文件名为空或无效")
    path = STAGING_DIR / upload_id / safe_name
    try:
        path.relative_to(STAGING_DIR)
    except ValueError as exc:
        raise ValueError(f"暂存路径越界: {path}") from exc
    if not path.is_file():
        raise FileNotFoundError(f"暂存文件不存在: {upload_id}/{safe_name}")
    data = path.read_bytes()
    if len(data) == 0:
        raise ValueError(f"暂存文件为空: {safe_name}")
    if len(data) > MAX_FILE_SIZE:
        raise ValueError(f"文件超过 {MAX_FILE_SIZE_MB}MB: {safe_name}")
    return data


def _message_text(message: Any) -> str:
    if isinstance(message, dict):
        content = message.get("content", "")
    else:
        content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif hasattr(block, "text"):
                parts.append(str(block.text))
        return "\n".join(parts)
    return str(content or "")


def _message_type(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("type") or message.get("role") or "")
    return str(getattr(message, "type", "") or getattr(message, "role", "") or "")


def extract_uploaded_files_from_text(text: str) -> list[dict[str, str]]:
    """从文本中解析 FILE 块，返回 UploadedFile 风格的 dict 列表。"""
    results: list[dict[str, str]] = []
    for name, upload_id, mime, body in _FILE_TAG_RE.findall(text):
        filename = _sanitize_filename(name)
        if not filename:
            continue
        mime_type = (mime or "application/octet-stream").strip() or "application/octet-stream"
        try:
            if upload_id:
                data = load_staged_bytes(upload_id, filename)
                data_b64 = base64.b64encode(data).decode("ascii")
            else:
                raw = re.sub(r"\s+", "", body or "")
                if not raw:
                    continue
                # 校验可解码且未超限
                data = base64.b64decode(raw, validate=False)
                if len(data) == 0 or len(data) > MAX_FILE_SIZE:
                    raise ValueError(f"内联 base64 无效或超限: {filename}")
                data_b64 = raw
        except Exception as exc:  # noqa: BLE001 - 单文件失败记入错误通道由中间件写 .error.md
            results.append(
                {
                    "filename": filename,
                    "mime_type": mime_type,
                    "data_base64": "",
                    "_error": str(exc),
                }
            )
            continue
        results.append(
            {
                "filename": filename,
                "mime_type": mime_type,
                "data_base64": data_b64,
            }
        )
    return results


def extract_uploaded_files_from_messages(messages: list[Any] | None) -> list[dict[str, str]]:
    """从最近一条 human 消息提取 FILE 引用（避免重复解析历史轮次）。"""
    if not messages:
        return []
    for message in reversed(messages):
        mtype = _message_type(message).lower()
        if mtype in ("human", "user"):
            return extract_uploaded_files_from_text(_message_text(message))
    return []

"""测试用例导出工具：Excel(xlsx) 与思维导图(xmind)。

导出文件写入项目根目录下的 ``exports/``（与 agent 虚拟文件系统的
``/exports/`` 路由指向同一磁盘目录），由 langgraph 服务的自定义路由
``GET /exports/{filename}`` 提供下载。

环境变量：
    EXPORT_BASE_URL  导出文件下载链接的基础地址（默认 http://127.0.0.1:2024）
"""

from __future__ import annotations

import json
import os
import uuid
import zipfile
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

EXPORTS_DIR = Path(__file__).resolve().parent.parent / "exports"

# 用例字段 -> Excel 列头（顺序即列顺序）
TESTCASE_COLUMNS = [
    ("case_id", "用例编号"),
    ("module", "所属模块"),
    ("title", "用例标题"),
    ("precondition", "前置条件"),
    ("test_data", "测试数据"),
    ("steps", "操作步骤"),
    ("expected_result", "预期结果"),
    ("priority", "优先级"),
    ("case_type", "用例类型"),
    ("design_method", "设计方法"),
]


def _export_base_url() -> str:
    return os.environ.get("EXPORT_BASE_URL", "http://127.0.0.1:2024").rstrip("/")


def _safe_path(filename: str, suffix: str) -> Path:
    """把模型给的文件名收敛为 exports 目录下的安全路径。"""
    name = Path(filename).name or f"export{suffix}"
    if not name.lower().endswith(suffix):
        name += suffix
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return EXPORTS_DIR / name


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(
            f"{i}. {item}" for i, item in enumerate(value, start=1)
        )
    return str(value)


@tool
def export_excel(testcases: list[dict[str, Any]], filename: str = "testcases.xlsx") -> str:
    """把测试用例列表导出为 Excel (.xlsx) 文件。

    Args:
        testcases: 测试用例列表，每个用例是 dict，字段：
            case_id(用例编号), module(所属模块), title(用例标题),
            precondition(前置条件), test_data(测试数据), steps(操作步骤),
            expected_result(预期结果), priority(优先级，如 P0/P1/P2),
            case_type(用例类型，如 功能/边界/异常), design_method(设计方法，如 等价类/边界值/场景法)。
            steps 可以是字符串或字符串列表。
        filename: 输出文件名（.xlsx 结尾，仅文件名，不含路径）。

    Returns:
        str: 生成结果说明与下载链接。
    """
    if not testcases:
        return "导出失败：testcases 为空，请先完成测试用例设计。"

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "测试用例"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="4472C4")
    wrap = Alignment(wrap_text=True, vertical="top")

    for col, (_, header) in enumerate(TESTCASE_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    widths = [14, 14, 28, 24, 20, 48, 48, 8, 10, 12]
    for col, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col)].width = width

    for row, case in enumerate(testcases, start=2):
        for col, (field, _) in enumerate(TESTCASE_COLUMNS, start=1):
            cell = ws.cell(row=row, column=col, value=_stringify(case.get(field)))
            cell.alignment = wrap
    ws.freeze_panes = "A2"

    path = _safe_path(filename, ".xlsx")
    wb.save(path)
    return (
        f"已导出 {len(testcases)} 条测试用例到 {path.name}。\n"
        f"下载链接：{_export_base_url()}/exports/{path.name}"
    )


def _build_topic(node: dict[str, Any]) -> dict[str, Any]:
    topic: dict[str, Any] = {
        "id": uuid.uuid4().hex[:24],
        "class": "topic",
        "title": str(node.get("title", "")),
    }
    children = node.get("children") or []
    if children:
        topic["children"] = {
            "attached": [_build_topic(child) for child in children]
        }
    return topic


@tool
def export_xmind(title: str, tree: list[dict[str, Any]], filename: str = "test_points.xmind") -> str:
    """把测试点层级结构导出为 XMind 思维导图 (.xmind) 文件。

    Args:
        title: 中心主题（导图标题），通常为被测系统/需求名称。
        tree: 子主题树，list[dict]，每个节点结构为
            {"title": "主题文本", "children": [...]}（children 可省略）。
            建议层级：模块 -> 功能点 -> 测试点。
        filename: 输出文件名（.xmind 结尾，仅文件名，不含路径）。

    Returns:
        str: 生成结果说明与下载链接。
    """
    if not tree:
        return "导出失败：tree 为空，请先完成测试点分析。"

    root = {"title": title, "children": tree}
    content = [
        {
            "id": uuid.uuid4().hex[:24],
            "class": "sheet",
            "title": title,
            "rootTopic": _build_topic(root),
            "topicPositioning": "fixed",
        }
    ]
    metadata = {"creator": {"name": "testcase-agent", "version": "1.0.0"}}
    manifest = {"file-entries": {"content.json": {}, "metadata.json": {}}}

    path = _safe_path(filename, ".xmind")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.json", json.dumps(content, ensure_ascii=False))
        zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False))
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))

    return (
        f"已导出思维导图 {path.name}（中心主题：{title}）。\n"
        f"下载链接：{_export_base_url()}/exports/{path.name}"
    )

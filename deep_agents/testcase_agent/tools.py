"""测试用例导出工具：Excel(xlsx) 与思维导图(xmind)。

导出文件写入项目根目录下的 ``exports/``（与 agent 虚拟文件系统的
``/exports/`` 路由指向同一磁盘目录），由 langgraph 服务的自定义路由
``GET /exports/{filename}`` 提供下载。

环境变量：
    EXPORT_BASE_URL  导出文件下载链接的基础地址（默认 http://127.0.0.1:2024）

新增脚本化导出（通过 make_export_tools 工厂函数使用）：
    export_from_markdown: 从 Markdown 文件直接解析并导出，无需 LLM 手工构造 JSON
    export_xmind_from_markdown: 从测试点 Markdown 直接生成思维导图
    generate_coverage_report: 脚本化生成覆盖矩阵报告
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from deep_agents.testcase_agent.markdown_parser import (
    generate_coverage_matrix,
    parse_testcases_from_markdown,
    parse_testpoints_from_markdown,
)

EXPORTS_DIR = Path(__file__).resolve().parent.parent / "exports"

# append_file 并发护栏：同一路径串行化
_append_locks: dict[str, threading.Lock] = {}
_append_locks_mu = threading.Lock()


def _get_append_lock(path: str) -> threading.Lock:
    with _append_locks_mu:
        if path not in _append_locks:
            _append_locks[path] = threading.Lock()
        return _append_locks[path]


def make_append_file(backend):
    """工厂函数：创建 append_file 工具，绑定特定 backend 实例。

    append_file 用于分段追加写入长文档，避免单次 max_tokens 截断。
    路径统一规范化为虚拟路径（/开头），自动处理 Windows 绝对路径的转换。
    """
    import re

    @tool
    def append_file(file_path: str, content: str) -> str:
        """追加写入内容到文件尾部（用于长文档分段写入）。

        **使用场景**：文档内容可能超过单次输出上限（max_tokens），
        应先用 write_file 写首段，再用本工具逐段追加。

        **分段策略**：
        - 每段建议 ≤2000 字符
        - 同一份文档的多段追加必须分多轮调用
        - **禁止在同一轮里并行调用多个 append_file**（并发追加会丢内容）

        **并发护栏**：同一文件路径的追加操作已串行化，
        多轮追加请在上一轮返回后再调用下一轮。

        **路径规范**：必须使用虚拟路径（如 /analysis/test_points.md）。
        Windows 绝对路径（如 C:\\...）会被自动转换为虚拟路径。
        禁止包含 '..' 进行目录遍历。

        Args:
            file_path: 目标文件路径（虚拟路径，禁止包含 .. 相对路径）
            content: 要追加的内容

        Returns:
            str: 写入结果说明
        """
        # 拒绝明显的 .. 逃逸
        if ".." in file_path:
            return f"错误：file_path 禁止包含 '..'（收到：{file_path}）"

        # 统一转为虚拟路径（/analysis/test_points.md 格式）
        # 处理各种异常路径格式：
        #   C:\\analysis\\test_points.md  → /analysis/test_points.md
        #   C:/analysis/test_points.md  → /analysis/test_points.md
        #   /analysis/test_points.md   → /analysis/test_points.md
        #   analysis/test_points.md     → /analysis/test_points.md
        normalized = file_path.replace("\\\\", "/")
        # 去掉 Windows 驱动器前缀
        normalized = re.sub(r"^[a-zA-Z]:", "", normalized)
        # 保证以 / 开头
        if not normalized.startswith("/"):
            normalized = "/" + normalized.lstrip("/")

        # 读已有内容（并发护栏）
        lock = _get_append_lock(normalized)
        with lock:
            try:
                read_result = backend.read(normalized, limit=10**9)
            except Exception:
                read_result = type("ReadResult", (), {"error": "read error"})()

            existing = ""
            if read_result.file_data:
                existing = read_result.file_data.get("content", "") or ""

            new_content = existing + content
            backend.write(normalized, new_content)

        return (
            f"已追加写入 {normalized}（本次追加 {len(content)} 字符，"
            f"累计 {len(new_content)} 字符）"
        )

    return append_file


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


def make_export_tools(backend):
    """工厂函数：创建脚本化导出工具，绑定特定 backend 实例。

    这些工具直接从 Markdown 文件解析并导出，无需 LLM 手工构造 JSON。
    """

    @tool
    def export_from_markdown(
        markdown_path: str,
        output_filename: str = "testcases.xlsx",
    ) -> str:
        """脚本化导出：从 Markdown 文件直接解析并导出 Excel。

        这是 export_excel 的脚本化版本，无需 LLM 手工构造 JSON。
        直接读取 Markdown 文件，解析测试用例，然后导出。

        Args:
            markdown_path: 测试用例 Markdown 文件的虚拟路径（如 /testcases/testcases.md）
            output_filename: 输出文件名（.xlsx 结尾）

        Returns:
            str: 生成结果说明与下载链接。
        """
        # 读取 Markdown 文件
        try:
            result = backend.read(markdown_path, limit=10**9)
            if result.error:
                return f"导出失败：读取文件错误 - {result.error}"
            markdown = result.file_data.get("content", "") if result.file_data else ""
            if not markdown:
                return "导出失败：文件内容为空"
        except Exception as e:
            return f"导出失败：{str(e)}"

        # 解析测试用例
        testcases = parse_testcases_from_markdown(markdown)
        if not testcases:
            return "导出失败：未能从 Markdown 中解析出测试用例"

        # 使用 export_excel 的逻辑导出
        return export_excel.invoke({"testcases": testcases, "filename": output_filename})

    @tool
    def export_xmind_from_markdown(
        markdown_path: str,
        title: str = "测试点",
        output_filename: str = "test_points.xmind",
    ) -> str:
        """脚本化导出：从测试点 Markdown 直接生成 XMind 思维导图。

        这是 export_xmind 的脚本化版本，无需 LLM 手工构造 tree 参数。

        Args:
            markdown_path: 测试点 Markdown 文件的虚拟路径（如 /analysis/test_points.md）
            title: 导图标题（中心主题）
            output_filename: 输出文件名（.xmind 结尾）

        Returns:
            str: 生成结果说明与下载链接。
        """
        # 读取 Markdown 文件
        try:
            result = backend.read(markdown_path, limit=10**9)
            if result.error:
                return f"导出失败：读取文件错误 - {result.error}"
            markdown_content = result.file_data.get("content", "") if result.file_data else ""
            if not markdown_content:
                return "导出失败：文件内容为空"
        except Exception as e:
            return f"导出失败：{str(e)}"

        # 解析测试点层级
        tree = parse_testpoints_from_markdown(markdown_content)
        if not tree:
            return "导出失败：未能从 Markdown 中解析出测试点结构"

        # 使用 export_xmind 的逻辑导出
        return export_xmind.invoke({"title": title, "tree": tree, "filename": output_filename})

    @tool
    def generate_coverage_report(
        testpoints_path: str,
        testcases_path: str,
        output_path: str = "/analysis/coverage_report.md",
    ) -> str:
        """脚本化生成覆盖矩阵报告。

        这是评审阶段覆盖矩阵比对的脚本化替代，避免 LLM 读全文逐条核对。

        Args:
            testpoints_path: 测试点 Markdown 路径（如 /analysis/test_points.md）
            testcases_path: 测试用例 Markdown 路径（如 /testcases/testcases.md）
            output_path: 覆盖报告输出路径

        Returns:
            str: 生成结果说明，包含覆盖率统计和未覆盖测试点列表。
        """
        # 读取两个 Markdown 文件
        try:
            tp_result = backend.read(testpoints_path, limit=10**9)
            tc_result = backend.read(testcases_path, limit=10**9)

            if tp_result.error:
                return f"生成失败：读取测试点文件错误 - {tp_result.error}"
            if tc_result.error:
                return f"生成失败：读取用例文件错误 - {tc_result.error}"

            testpoints_md = tp_result.file_data.get("content", "") if tp_result.file_data else ""
            testcases_md = tc_result.file_data.get("content", "") if tc_result.file_data else ""
        except Exception as e:
            return f"生成失败：{str(e)}"

        # 解析
        testpoints = parse_testpoints_from_markdown(testpoints_md)
        testcases = parse_testcases_from_markdown(testcases_md)

        # 生成覆盖矩阵
        report = generate_coverage_matrix(testpoints, testcases)

        # 生成报告文本
        stats = report["统计"]
        report_lines = [
            "# 测试覆盖率报告\n",
            f"## 统计摘要\n",
            f"- 测试点总数：{stats['测试点总数']}",
            f"- 已覆盖数：{stats['已覆盖数']}",
            f"- 未覆盖数：{stats['未覆盖数']}",
            f"- 覆盖率：{stats['覆盖率']}\n",
            f"## 未覆盖测试点\n",
        ]

        if report["未覆盖测试点"]:
            for tp in report["未覆盖测试点"]:
                report_lines.append(f"- {tp}")
        else:
            report_lines.append("✅ 所有测试点均已覆盖")

        report_content = "\n".join(report_lines)

        # 写入报告文件
        try:
            backend.write(output_path, report_content)
        except Exception as e:
            return f"生成失败：写入报告错误 - {str(e)}"

        return (
            f"覆盖报告已生成：\n"
            f"- 测试点总数：{stats['测试点总数']}\n"
            f"- 覆盖率：{stats['覆盖率']}\n"
            f"- 未覆盖测试点：{stats['未覆盖数']} 个\n"
            f"- 报告文件：{output_path}"
        )

    return export_from_markdown, export_xmind_from_markdown, generate_coverage_report


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

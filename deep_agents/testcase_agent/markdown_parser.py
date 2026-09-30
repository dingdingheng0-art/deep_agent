"""Markdown 解析工具：结构化提取测试用例和测试点。

本模块提供确定性解析能力，避免让 LLM 手工搬运全量数据。
- parse_testcases_from_markdown: 从测试用例 Markdown 提取结构化数据
- parse_testpoints_from_markdown: 从测试点 Markdown 提取层级结构
- generate_coverage_matrix: 生成覆盖矩阵（脚本化，不让 LLM 逐条核对）
"""

from __future__ import annotations

import re
from typing import Any


def parse_testcases_from_markdown(markdown: str) -> list[dict[str, Any]]:
    """从测试用例 Markdown 提取结构化数据。

    支持两种格式：
    1. Markdown 表格格式
    2. YAML 列表格式

    Args:
        markdown: 测试用例 Markdown 内容

    Returns:
        测试用例列表，每项包含 case_id, module, title, precondition,
        test_data, steps, expected_result, priority, case_type, design_method
    """
    testcases: list[dict[str, Any]] = []

    # 格式1: Markdown 表格
    # | 用例编号 | 所属模块 | 用例标题 | 前置条件 | 测试数据 | 操作步骤 | 预期结果 | 优先级 | 用例类型 | 设计方法 |
    table_pattern = re.compile(
        r"\|(.+?)\|(.+?)\|(.+?)\|(.+?)\|(.+?)\|(.+?)\|(.+?)\|(.+?)\|(.+?)\|(.+?)\|",
        re.DOTALL,
    )
    rows = table_pattern.findall(markdown)

    if rows:
        # 第一行通常是表头，跳过
        for row in rows[1:]:
            if len(row) >= 10:
                case_id = row[0].strip()
                # 跳过表头行
                if case_id in ("用例编号", "case_id", "ID"):
                    continue
                testcase = {
                    "case_id": case_id,
                    "module": _clean_text(row[1].strip()),
                    "title": _clean_text(row[2].strip()),
                    "precondition": _clean_text(row[3].strip()),
                    "test_data": _clean_text(row[4].strip()),
                    "steps": _clean_text(row[5].strip()),
                    "expected_result": _clean_text(row[6].strip()),
                    "priority": _clean_text(row[7].strip()),
                    "case_type": _clean_text(row[8].strip()),
                    "design_method": _clean_text(row[9].strip()),
                }
                if testcase["case_id"]:
                    testcases.append(testcase)

    # 格式2: YAML 列表
    # - 用例编号: TC_001
    #   所属模块: 登录
    yaml_pattern = re.compile(
        r"-\s*用例编号[:：]\s*(\S+).*?"
        r"所属模块[:：]\s*([^\n]+).*?"
        r"用例标题[:：]\s*([^\n]+).*?"
        r"(?:前置条件[:：]\s*([^\n]+))?.*?"
        r"(?:测试数据[:：]\s*([^\n]+))?.*?"
        r"操作步骤[:：]\s*([^\n]+).*?"
        r"预期结果[:：]\s*([^\n]+).*?"
        r"(?:优先级[:：]\s*([^\n]+))?.*?"
        r"(?:用例类型[:：]\s*([^\n]+))?.*?"
        r"(?:设计方法[:：]\s*([^\n]+))?",
        re.DOTALL,
    )

    yaml_matches = yaml_pattern.findall(markdown)
    for match in yaml_matches:
        if match[0] and match[0] not in [tc["case_id"] for tc in testcases]:
            testcase = {
                "case_id": match[0].strip(),
                "module": _clean_text(match[1].strip()),
                "title": _clean_text(match[2].strip()),
                "precondition": _clean_text(match[3].strip() if match[3] else ""),
                "test_data": _clean_text(match[4].strip() if match[4] else ""),
                "steps": _clean_text(match[5].strip()),
                "expected_result": _clean_text(match[6].strip()),
                "priority": _clean_text(match[7].strip() if match[7] else "P1"),
                "case_type": _clean_text(match[8].strip() if match[8] else "功能"),
                "design_method": _clean_text(match[9].strip() if match[9] else "场景法"),
            }
            testcases.append(testcase)

    # 去重（表格和 YAML 可能有重复）
    seen = set()
    unique = []
    for tc in testcases:
        if tc["case_id"] not in seen:
            seen.add(tc["case_id"])
            unique.append(tc)

    return unique


def parse_testpoints_from_markdown(markdown: str) -> list[dict[str, Any]]:
    """从测试点 Markdown 提取层级结构（模块 → 功能点 → 测试点）。

    Args:
        markdown: 测试点 Markdown 内容

    Returns:
        树形结构列表，每个节点包含 title, level, children
    """
    result: list[dict[str, Any]] = []
    current_module: dict[str, Any] | None = None
    current_func: dict[str, Any] | None = None

    lines = markdown.split("\n")

    for line in lines:
        stripped = line.strip()

        # 模块标题: ## 模块名 或 ### [模块] 模块名
        module_match = re.match(r"^#{2,3}\s*(?:\[模块\]|模块)[:：]?\s*(.+)", stripped)
        if module_match:
            module_name = module_match.group(1).strip()
            current_module = {
                "title": module_name,
                "level": "module",
                "children": [],
            }
            current_func = None
            result.append(current_module)
            continue

        # 功能点标题: ### 功能点 或 #### [功能点] 功能点
        func_match = re.match(r"^#{3,4}\s*(?:\[功能点\]|功能点)[:：]?\s*(.+)", stripped)
        if func_match and current_module:
            func_name = func_match.group(1).strip()
            current_func = {
                "title": func_name,
                "level": "function",
                "children": [],
            }
            current_module["children"].append(current_func)
            continue

        # 测试点行: - [x] TP_001 测试点描述 或 - TP_001 测试点描述
        tp_match = re.match(r"^[-*]\s*(?:\[.?\]\s*)?(?:TP[_-]?\d+[:：]?\s*)?(.+)", stripped)
        if tp_match and current_func is not None:
            desc = tp_match.group(1).strip()
            # 提取测试点标签 [正常]/[边界]/[异常]
            tags = re.findall(r"\[(正常|边界|异常|性能|安全|接口)\]", desc)
            tp_node = {
                "title": re.sub(r"\[(正常|边界|异常|性能|安全|接口)\]\s*", "", desc).strip(),
                "level": "testpoint",
                "tags": tags if tags else ["功能"],
            }
            current_func["children"].append(tp_node)
        elif tp_match and current_module is not None and current_func is None:
            # 没有功能点的测试点，放到模块下
            desc = tp_match.group(1).strip()
            tp_node = {
                "title": re.sub(r"\[(正常|边界|异常|性能|安全|接口)\]\s*", "", desc).strip(),
                "level": "testpoint",
                "tags": [],
            }
            # 创建一个隐含的"通用功能点"
            if not current_module["children"]:
                current_func = {
                    "title": "通用功能",
                    "level": "function",
                    "children": [],
                }
                current_module["children"].append(current_func)
            current_module["children"][-1]["children"].append(tp_node)

    return result


def generate_coverage_matrix(
    testpoints: list[dict[str, Any]],
    testcases: list[dict[str, Any]],
) -> dict[str, Any]:
    """生成测试点 × 用例覆盖矩阵。

    这是一个确定性脚本，不依赖 LLM 逐条核对。

    Args:
        testpoints: 测试点列表（来自 parse_testpoints_from_markdown）
        testcases: 测试用例列表（来自 parse_testcases_from_markdown）

    Returns:
        包含覆盖统计和详细矩阵的字典
    """
    # 提取所有测试点标题
    all_tp: list[str] = []
    tp_to_module: dict[str, str] = {}

    def extract_tp(nodes: list[dict], module: str = ""):
        for node in nodes:
            if node.get("level") == "testpoint":
                title = node.get("title", "")
                if title:
                    all_tp.append(title)
                    tp_to_module[title] = module
            elif node.get("level") == "function":
                func_name = node.get("title", "")
                if node.get("children"):
                    extract_tp(node["children"], module or func_name)
            elif node.get("level") == "module":
                module_name = node.get("title", "")
                if node.get("children"):
                    extract_tp(node["children"], module_name)

    extract_tp(testpoints)

    # 建立用例到模块的映射
    case_to_module: dict[str, str] = {}
    for tc in testcases:
        module = tc.get("module", "")
        case_id = tc.get("case_id", "")
        if module and case_id:
            if module not in case_to_module:
                case_to_module[module] = []
            case_to_module[module].append(case_id)

    # 构建覆盖矩阵
    matrix: list[dict[str, Any]] = []
    covered_tp = set()

    for i, tp_title in enumerate(all_tp, 1):
        module = tp_to_module.get(tp_title, "未知")
        # 简单匹配：找同模块的用例
        covered_cases = case_to_module.get(module, [])
        covered = len(covered_cases) > 0

        if covered:
            covered_tp.add(tp_title)

        matrix.append({
            "序号": i,
            "测试点": tp_title,
            "所属模块": module,
            "覆盖用例": covered_cases[0] if covered_cases else "",
            "覆盖状态": "✅ 已覆盖" if covered else "❌ 未覆盖",
        })

    total_tp = len(all_tp)
    covered_count = len(covered_tp)
    coverage_rate = (covered_count / total_tp * 100) if total_tp > 0 else 0

    return {
        "统计": {
            "测试点总数": total_tp,
            "已覆盖数": covered_count,
            "未覆盖数": total_tp - covered_count,
            "覆盖率": f"{coverage_rate:.1f}%",
        },
        "矩阵": matrix,
        "未覆盖测试点": [tp for tp in all_tp if tp not in covered_tp],
    }


def _clean_text(text: str) -> str:
    """清理文本中的 Markdown 格式符号。"""
    if not text:
        return ""
    # 移除 <br> 等 HTML 标签（保留换行语义）
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # 移除多余的空白
    text = re.sub(r"\s+", " ", text)
    return text.strip()

# Token 优化实施报告

## 问题诊断

根据对 trace 的分析，token 浪费的核心原因是：

1. **上下文只增不减**：历史全程携带，从 6k 涨到 78k+
2. **大文件重复读取**：需求文档、测试点清单、测试用例被多个子代理各读一遍
3. **超长 thinking/signature**：数千至上万字符的签名被序列化、回传、存储
4. **失败重试无幂等检查**：designer 多次失败重试，每次都重新读文件
5. **并发路径冲突**：多会话共用 `/analysis/test_points.md` 等固定路径
6. **无效工具调用**：grep 等工具反复重试
7. **LLM 手工搬运全量数据**：导出时让模型构造 171 个 dict 的 JSON
8. **逐条人工核对测试点**：reviewer 读全文逐条比对
9. **模型自身 thinking 过长**：reasoning 占输出 token 大头
10. **导出探针实验**：多次尝试判断追加还是覆盖

## 优化实施

### 1. 消息裁剪中间件 (`MessageTrimmingMiddleware`)

**文件**: `token_optimization.py`

```python
# 超过 25 条消息时，将早期消息替换为摘要
# 保留最近 30 条消息 + 系统消息
trimming_middleware = MessageTrimmingMiddleware(
    max_messages=30,
    summary_threshold=25,
)
```

**效果**: 防止历史消息无限累积，将上下文从 70k+ 级别压缩到稳定状态

### 2. Thinking 移除中间件 (`ThinkingStripperMiddleware`)

**文件**: `token_optimization.py`

```python
# 移除 <thinking>...</thinking> 等 thinking 块
# 截断超长签名（默认最大 500 字符）
thinking_stripper = ThinkingStripperMiddleware(
    strip_thinking=True,
    max_signature_chars=500,
)
```

**效果**: 显著减少输出 token 体积，避免无效签名占用上下文

### 3. 子代理幂等检查 (`IdempotentSubagentMiddleware`)

**文件**: `token_optimization.py`

```python
# 子代理执行前检查产物文件是否存在且完整
# 存在则跳过，避免重复执行
idempotent_subagent = IdempotentSubagentMiddleware(
    backend=backend,
    product_paths={
        "requirement-analyzer": "/analysis/test_points.md",
        "testcase-designer": "/testcases/testcases.md",
        "testcase-reviewer": "/review/review.md",
    },
    min_file_size=200,
)
```

**效果**: 避免重试时重新读文件、重新规划，节省子代理调用 token

### 4. 会话级路径隔离 (`SessionIsolationMiddleware`)

**文件**: `token_optimization.py`

```python
# 为每个会话生成唯一标识符，隔离产物路径
# 避免多会话文件冲突
isolation = SessionIsolationMiddleware()
# get_isolated_path("test_points") 
# -> "/analysis/session_xxx_test_points.md"
```

**效果**: 防止并发冲突导致的读错文件、反复检查

### 5. 脚本化 Markdown 解析 (`markdown_parser.py`)

**文件**: `markdown_parser.py`

提供确定性解析：
- `parse_testcases_from_markdown()`: 从 Markdown 提取结构化测试用例
- `parse_testpoints_from_markdown()`: 从 Markdown 提取测试点层级
- `generate_coverage_matrix()`: 生成覆盖矩阵（脚本化，不依赖 LLM）

**效果**: 替代 LLM 逐条核对，覆盖矩阵生成从 50k+ input_tokens 降到几 k

### 6. 脚本化导出工具 (`tools.py`)

**文件**: `tools.py`

新增工厂函数 `make_export_tools(backend)` 创建绑定后端的工具：

- `export_from_markdown()`: 直接从 Markdown 解析并导出 Excel
- `export_xmind_from_markdown()`: 直接从测试点 Markdown 导出 XMind
- `generate_coverage_report()`: 生成覆盖报告

```python
# 使用示例
export_from_markdown, export_xmind_from_markdown, generate_coverage_report = make_export_tools(backend)

# 导出 Excel（无需 LLM 手工构造 JSON）
export_from_markdown.invoke({
    "markdown_path": "/testcases/testcases.md",
    "output_filename": "testcases.xlsx"
})
```

**效果**: 消除 LLM 手工搬运 171 个 dict 的 JSON，减少导出 token 消耗

### 7. 产物外置策略 (prompts.py)

**文件**: `prompts.py`

更新提示词，明确要求：
- 子代理只写文件路径和摘要，不返回全文
- 主代理只读路径和摘要，不把全文带回上下文
- 优先使用脚本化工具

**效果**: 从根本上减少上下文体积

## 环境变量配置

```bash
# Token 优化相关
MAX_MESSAGES=30                    # 最大保留消息数
SUMMARY_THRESHOLD=25               # 触发裁剪的消息数阈值
STRIP_THINKING=true                # 是否移除 thinking 块
MAX_SIGNATURE_CHARS=500            # 签名最大字符数
```

## 预期效果

| 指标 | 优化前 | 优化后 | 节省 |
|------|--------|--------|------|
| 主链路 input_tokens | 78k+ | 10k~20k | ~75% |
| 子代理重复读取 | 多次全文读取 | 一次文件写入 | ~80% |
| 导出 token | 手工构造 JSON | 脚本解析 | ~90% |
| 覆盖矩阵 | LLM 逐条核对 | 脚本生成 | ~85% |
| thinking/signature | 数千字符 | 截断保留 | ~70% |

## 使用方式

```python
# agent.py 中自动启用
from deep_agents.testcase_agent.agent import make_agent

# 中间件已自动注册，无需额外配置
agent = await make_agent()
```

## 注意事项

1. **分段写入仍需**: 长文档仍需用 `write_file` + `append_file` 分段
2. **幂等检查依赖产物路径**: 子代理产物路径必须与 `IdempotentSubagentMiddleware.product_paths` 一致
3. **会话隔离可选**: 如无并发需求，可不启用 `SessionIsolationMiddleware`

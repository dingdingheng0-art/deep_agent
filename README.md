# deep_agent

基于 LangGraph / DeepAgents 的多智能体项目，包含：

| 模块 | 说明 |
| --- | --- |
| `deep_agents/research_agent/` | 联网研究 Agent（Tavily 搜索 + LLM 汇总） |
| `deep_agents/testcase_agent/` | 测试用例生成 Agent：主 Agent 编排 + 3 个子代理（需求分析 / 用例设计 / 用例评审） |
| `deep_agents/testcase_agent/skills/` | 领域方法论技能库（SKILL.md 渐进式披露） |
| `langgraph_agent.py` | `langgraph dev` 注册的 `research` 图 |
| `main.py` | 研究 Agent 本地调试入口 |

## 环境准备

```bash
# 1. 安装依赖（推荐 uv）
uv venv
uv pip install -r requirements.txt

# 2. 配置密钥：复制模板并填入真实值
cp .env.example .env
```

> `.env` 已被 `.gitignore` 排除，**永远不会被提交**。
> 仓库里只保留 `.env.example` 作为配置模板。

## 运行

```bash
uv run langgraph dev              # 启动 LangGraph 服务（langgraph.json 声明了两个图）
uv run python main.py "你的问题"   # 单独跑研究 Agent
```

## Git 与 AI 提交检查

### 版本回退

```bash
git log --oneline --graph        # 查看历史
git show <hash>                  # 查看某次提交
git diff <hash>                  # 对比工作区与某次提交

# 回退方式（按风险从低到高）
git restore <file>                          # 丢弃某文件的改动
git restore --staged <file>                 # 取消暂存
git revert <hash>                           # 追加一个反向提交（推荐，历史不被改写）
git reset --soft <hash>                     # 回到某提交，保留改动在暂存区
git reset --hard <hash>                     # 彻底回退（⚠️ 丢失未提交改动）

# 误提交了密钥：先清历史再用 force 推送
git filter-repo --path .env --invert-paths
```

### 提交检查钩子

钩子存放在 `.githooks/`（已纳入版本库，克隆后不丢失），通过
`core.hooksPath` 启用，新克隆需执行一次：

```bash
git config core.hooksPath .githooks
```

| 钩子 | 作用 |
| --- | --- |
| `pre-commit` | 扫描**新增行**拦截疑似密钥（`sk-`/`ghp_`/`AKIA`/`xox` 等）；阻止 `.env` 入库；拦截 >5MB 大文件 |
| `commit-msg` | 校验提交信息为 `<type>(<scope>): <subject>` 且首行 ≤72 字符；拦截 AI 水印、笼统描述、`WIP` 等无意义信息 |

支持的 `type`：`feat` `fix` `docs` `style` `refactor` `perf` `test` `build` `ci` `chore` `revert`

首次提交示例：

```
feat(testcase): 新增 xmind 导出工具

补充理由：用户在评审阶段需要可视化用例结构。
```

紧急情况可用 `git commit --no-verify` 跳过检查。

### 自测钩子

```bash
bash .githooks/test_hooks.sh        # 11 项：提交信息规范
bash .githooks/test_secret_scan.sh  #  9 项：密钥拦截/放行判定
```

### 说明

- `deep-agents-ui/` 是第三方上游检出（`langchain-ai/deep-agents-ui`），
  含 `node_modules` 约 757MB，已整体 gitignore。本地修改请在该子目录自行管理。
- `.langgraph_api/`、`.pytest_cache/`、`__pycache__/`、`workspace/`、
  `testcase_agent/upload_staging/` 等运行时产物同样已排除。

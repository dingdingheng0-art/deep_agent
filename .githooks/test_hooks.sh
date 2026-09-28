#!/bin/bash
# 钩子自测：验证 pre-commit / commit-msg 的拦截与放行行为是否正确。
# 用法: bash .githooks/test_hooks.sh
cd "$(dirname "$0")/.." || exit 1
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0

check() { # check <描述> <期望: pass|block> <hook> [message]
  local desc="$1" expect="$2" hook="$3" msg="$4"
  local out rc
  if [ "$hook" = "pre-commit" ]; then
    out=$(sh .githooks/pre-commit 2>&1); rc=$?
  else
    printf '%s' "$msg" > "$TMP/msg"
    out=$(sh .githooks/commit-msg "$TMP/msg" 2>&1); rc=$?
  fi
  if { [ "$expect" = "pass" ] && [ $rc -eq 0 ]; } || { [ "$expect" = "block" ] && [ $rc -ne 0 ]; }; then
    echo "  [PASS] $desc"; pass=$((pass+1))
  else
    echo "  [FAIL] $desc  (期望 $expect, 实际 rc=$rc)"; printf '%s\n' "$out" | sed 's/^/      /'; fail=$((fail+1))
  fi
}

echo "== pre-commit =="
check "当前暂存区无密钥 → 放行" pass pre-commit

echo "== commit-msg =="
check "规范信息 → 放行"           pass commit-msg "feat(testcase): 新增 xmind 导出工具"
check "规范+正文 → 放行"          pass commit-msg "fix(docling): 修复大文件解析超时

docling 解析 20MB 文档时连接被重置，增加重试。"
check "缺少 type 前缀 → 拦截"    block commit-msg "更新了配置文件"
check "type 不在白名单 → 拦截"   block commit-msg "wip: 改了点东西"
check "首行超 72 字符 → 拦截"    block commit-msg "feat(testcase): $(printf 'a%.0s' {1..100})"
check "空信息 → 拦截"            block commit-msg ""
check "AI 水印 → 拦截"           block commit-msg "feat: add agent

Generated with Claude Code"
check "笼统描述 → 拦截"          block commit-msg "chore: update code"
check "WIP 占位 → 拦截"          block commit-msg "WIP"
check "仅注释 → 拦截"            block commit-msg "# 注释行"

echo ""
echo "通过 $pass 项，失败 $fail 项"
[ $fail -eq 0 ]

#!/bin/bash
# pre-commit 密钥扫描的正/负样本验证（独立于 git 暂存区）
cd "$(dirname "$0")/.." || exit 1
ok=0; bad=0

expect_block() { # expect_block <描述> <文件内容>
  local desc="$1" content="$2"
  local f; f=$(mktemp)
  printf '%s' "$content" > "$f"
  if git check-ignore -q "$f" 2>/dev/null; then rm -f "$f"; return; fi
  # 直接用 PATTERN 复现 hook 的判定逻辑
  if printf '%s\n' "$content" | grep -E 'sk-[A-Za-z0-9._-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}' >/dev/null; then
    echo "  [OK-block] $desc"; ok=$((ok+1))
  else
    echo "  [MISS]     $desc 未被拦截"; bad=$((bad+1))
  fi
  rm -f "$f"
}

expect_pass() { # expect_pass <描述> <文件内容>
  local desc="$1" content="$2"
  if printf '%s\n' "$content" | grep -E 'sk-[A-Za-z0-9._-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}' >/dev/null; then
    echo "  [FALSE+ ]  $desc 被误拦截"; bad=$((bad+1))
  else
    echo "  [OK-pass]  $desc"; ok=$((ok+1))
  fi
}

echo "== 应拦截 =="
expect_block "阿里云 sk-ws- 密钥"  'DOCLING_SERVE_LLM_API_KEY=sk-ws-H.abc123def456ghi789jkl012mno345'
expect_block "OpenAI sk- 密钥"       'OPENAI_API_KEY=sk-proj-ABCdefGHI1234567890'
expect_block "GitHub ghp_ token"     'TOKEN=ghp_ABCdefGHI1234567890abcdefGHI'
expect_block "AWS AKIA"              'AWS=AKIAIOSFODNN7EXAMPLE'

echo "== 应放行 =="
expect_pass ".env.example 占位符"   'ANTHROPIC_API_KEY='
expect_pass "文档占位符 <your-key>" 'DOCLING_SERVE_LLM_API_KEY=<your-key>'
expect_pass "os.environ 读取"       'api_key = os.environ.get("DOCLING_SERVE_API_KEY")'
expect_pass ".gitignore 规则行"     '.env'
expect_pass "变量名含 SK 但无值"    'os.environ.get("ANTHROPIC_API_KEY", "")'

echo ""
echo "拦截/放行判定正确 $ok 项，异常 $bad 项"
[ $bad -eq 0 ]

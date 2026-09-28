#!/bin/bash
# docling-serve-multimodal-setup.sh
# docling serve 多模态配置脚本
# 使用方法: ssh root@43.155.235.44 后执行此脚本

set -e

echo "=== docling serve 多模态配置 ==="
echo ""

# 1. 查看当前容器
echo "[1/4] 查看当前运行的 docling 容器..."
docker ps | grep docling || echo "未找到 docling 容器"
echo ""

# 2. 确认需要配置的容器名称/ID (请替换为实际值)
CONTAINER_NAME="docling-serve"  # 修改为实际容器名
echo "[2/4] 目标容器: $CONTAINER_NAME"
echo ""

# 3. 配置多模态环境变量
echo "[3/4] 设置多模态模型配置..."
cat << 'EOF'
# 复制以下环境变量到容器或 docker-compose.yml：

DOCLING_SERVE_ENABLE_REMOTE_SERVICES=true

# 多模态模型配置 (千问3.8-flash)
DOCLING_SERVE_LLM_PROVIDER=openai
DOCLING_SERVE_LLM_MODEL_NAME=qwen3.8-flash
DOCLING_SERVE_LLM_API_KEY=<your-key>
DOCLING_SERVE_LLM_BASE_URL=https://llm-dea9z7nvzanzylcv.cn-beijing.maas.aliyuncs.com/compatible-mode/v1

# 如果需要额外的图片处理模型 (可选)
# DOCLING_SERVE_GRAPHICAL_MODEL_PROVIDER=openai
# DOCLING_SERVE_GRAPHICAL_MODEL_NAME=qwen-vl-plus
# DOCLING_SERVE_GRAPHICAL_MODEL_API_KEY=<your-key>
EOF
echo ""

# 4. 重新部署容器
echo "[4/4] 重新部署容器选项..."
cat << 'EOF'
选项 A: 如果使用 docker-compose
--------------------------------
编辑 docker-compose.yml 添加环境变量:
services:
  docling-serve:
    environment:
      - DOCLING_SERVE_ENABLE_REMOTE_SERVICES=true
      - DOCLING_SERVE_LLM_PROVIDER=openai
      - DOCLING_SERVE_LLM_MODEL_NAME=qwen3.8-flash
      - DOCLING_SERVE_LLM_API_KEY=<your-key>
      - DOCLING_SERVE_LLM_BASE_URL=https://llm-dea9z7nvzanzylcv.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
    ports:
      - "5001:5001"

然后执行:
docker-compose down
docker-compose up -d

选项 B: 如果直接使用 docker run
--------------------------------
docker stop docling-serve
docker rm docling-serve
docker run -d \
  --name docling-serve \
  -p 5001:5001 \
  -e DOCLING_SERVE_ENABLE_REMOTE_SERVICES=true \
  -e DOCLING_SERVE_LLM_PROVIDER=openai \
  -e DOCLING_SERVE_LLM_MODEL_NAME=qwen3.8-flash \
  -e DOCLING_SERVE_LLM_API_KEY=<your-key> \
  -e DOCLING_SERVE_LLM_BASE_URL=https://llm-dea9z7nvzanzylcv.cn-beijing.maas.aliyuncs.com/compatible-mode/v1 \
  ghcr.io/docling-project/docling-serve:latest

选项 C: 修改正在运行的容器环境变量 (临时)
-----------------------------------------
# 获取容器ID
CONTAINER_ID=$(docker ps --filter "name=docling" -q)

# 为容器设置环境变量 (重启后失效)
docker exec $CONTAINER_ID env DOCLING_SERVE_ENABLE_REMOTE_SERVICES=true
EOF
echo ""

echo "=== 配置完成 ==="

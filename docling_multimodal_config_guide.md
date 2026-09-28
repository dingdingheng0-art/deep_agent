# docling serve 多模态配置指南

## 目标
配置 docling serve 使用多模态模型（qwen3.8-flash）解析 PDF 中的图片、图表等视觉元素。

## 配置参数

### 环境变量

```bash
# 启用远程服务
DOCLING_SERVE_ENABLE_REMOTE_SERVICES=true

# 多模态模型配置 (千问3.8-flash)
DOCLING_SERVE_LLM_PROVIDER=openai
DOCLING_SERVE_LLM_MODEL_NAME=qwen3.8-flash
DOCLING_SERVE_LLM_API_KEY=<your-key>
DOCLING_SERVE_LLM_BASE_URL=https://llm-dea9z7nvzanzylcv.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
```

## 执行步骤

### 1. SSH 连接到服务器

```bash
ssh root@43.155.235.44
# 密码: 123456
```

### 2. 查看当前容器配置

```bash
# 查看运行的容器
docker ps | grep docling

# 查看容器环境变量
docker exec <container_id> env | grep DOCLING
```

### 3. 修改配置（选项 A: docker-compose）

如果使用 docker-compose 管理：

```bash
# 编辑 docker-compose.yml
vi docker-compose.yml
```

添加/修改环境变量：

```yaml
services:
  docling-serve:
    image: ghcr.io/docling-project/docling-serve:latest
    environment:
      - DOCLING_SERVE_ENABLE_REMOTE_SERVICES=true
      - DOCLING_SERVE_LLM_PROVIDER=openai
      - DOCLING_SERVE_LLM_MODEL_NAME=qwen3.8-flash
      - DOCLING_SERVE_LLM_API_KEY=<your-key>
      - DOCLING_SERVE_LLM_BASE_URL=https://llm-dea9z7nvzanzylcv.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
    ports:
      - "5001:5001"
```

重启容器：

```bash
docker-compose down
docker-compose up -d
```

### 4. 修改配置（选项 B: 直接运行）

如果使用 `docker run` 直接运行：

```bash
# 停止现有容器
docker stop docling-serve
docker rm docling-serve

# 重新启动（带多模态配置）
docker run -d \
  --name docling-serve \
  -p 5001:5001 \
  -e DOCLING_SERVE_ENABLE_REMOTE_SERVICES=true \
  -e DOCLING_SERVE_LLM_PROVIDER=openai \
  -e DOCLING_SERVE_LLM_MODEL_NAME=qwen3.8-flash \
  -e DOCLING_SERVE_LLM_API_KEY=<your-key> \
  -e DOCLING_SERVE_LLM_BASE_URL=https://llm-dea9z7nvzanzylcv.cn-beijing.maas.aliyuncs.com/compatible-mode/v1 \
  ghcr.io/docling-project/docling-serve:latest
```

### 5. 验证配置

```bash
# 检查健康状态
curl http://localhost:5001/health

# 测试多模态解析（上传包含图片的PDF）
curl -X POST http://localhost:5001/v1/convert/file \
  -F "file=@test.pdf" \
  -F "options={\"llm_model\":\"qwen3.8-flash\"}"
```

## 多模态效果

配置完成后，docling serve 将能够：
- ✅ 自动识别 PDF 中的图片、图表、流程图
- ✅ 使用多模态模型（qwen3.8-flash）生成图片描述
- ✅ 将图片内容整合到解析结果中
- ✅ 识别手写文字、印章等元素

## 常见问题

### Q: 容器启动失败
A: 检查 API Key 是否正确，网络是否可达

### Q: 图片解析超时
A: 增加超时配置，或检查多模态服务响应时间

### Q: 多模态模型未生效
A: 确认 `DOCLING_SERVE_ENABLE_REMOTE_SERVICES=true` 已设置

# FFmpeg 视频处理 API

一个基于 FastAPI 的异步视频合成服务。它接收远程 MP4 视频、MP3 音频和 ASS 字幕，使用 FFmpeg 合成视频，并将结果上传到 Amazon S3。

<p align="center">
  <a href="README.md">English</a>
</p>

如果这个项目对你有帮助，欢迎点一个 Star。

## 功能

- 异步提交视频合成任务
- 视频循环播放至音频结束
- 替换原始音频
- 烧录 ASS 字幕，支持中文字体
- 可选混入背景音乐
- 上传结果到 Amazon S3
- 使用 PostgreSQL 保存任务状态
- 支持 Docker 和 Railway 部署

## 工作流程

```text
客户端
  |
  | POST /process
  v
FastAPI
  |-- 下载 MP4 / MP3 / ASS
  |-- FFmpeg：循环视频 + 混合音频 + 烧录字幕
  |-- 上传结果到 S3
  `-- 更新 PostgreSQL 任务状态
```

## 环境要求

- Python 3.12+
- 支持 `libass` 的 FFmpeg
- PostgreSQL
- Amazon S3 存储桶

Docker 用户不需要手动安装 FFmpeg，项目镜像会自动安装 FFmpeg 和 Noto CJK 中文字体。

## 快速开始

### 1. 克隆项目

```bash
git clone <your-repository-url>
cd video
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`：

```env
POSTGRES_URL=postgresql://user:password@host:5432/database

AW_ACCESS_KEY_ID=your-access-key
AW_SECRET_ACCESS_KEY=your-secret-key
AW_REGION=eu-central-1
S3_BUCKET=your-bucket-name

FFMPEG_BIN=/usr/bin/ffmpeg
LOG_LEVEL=INFO
```

`AW_ACCESS_KEY_ID` 和 `AW_SECRET_ACCESS_KEY` 这两个变量名与当前代码保持一致。

### 3. 准备数据库

服务需要以下数据表：

- `ffmpeg_process_main`
- `ffmpeg_process_sub`

其中 `ffmpeg_process_sub.main_id` 引用 `ffmpeg_process_main.id`。请先创建主表，再创建任务表：

```sql
CREATE TABLE IF NOT EXISTS "ffmpeg_process_sub" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
  "main_id" uuid NOT NULL REFERENCES "ffmpeg_process_main"("id") ON DELETE CASCADE,
  "name" varchar NOT NULL,
  "status" varchar(20) NOT NULL,
  "payload" jsonb,
  "error_message" text,
  "createAt" timestamp DEFAULT now() NOT NULL,
  "updateAt" timestamp DEFAULT now() NOT NULL
);
```

### 4. 使用 Python 本地运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

打开 <http://localhost:8000/docs> 查看 Swagger 接口文档。

### 5. 使用 Docker 运行

```bash
docker compose up -d --build
```

API 地址为 <http://localhost:8001>。FFmpeg 已安装在同一个容器中，不需要单独启动 FFmpeg 服务。

查看日志：

```bash
docker compose logs -f video-api
```

## API 使用

### 健康检查

```bash
curl http://localhost:8001/health
```

```json
{"status":"ok"}
```

### 提交合成任务

```bash
curl -X POST http://localhost:8001/process \
  -H "Content-Type: application/json" \
  -d '{
    "main_id": "11111111-1111-1111-1111-111111111111",
    "ass_url": "https://example.com/subtitle.ass",
    "mp3_url": "https://example.com/voice.mp3",
    "mp4_url": "https://example.com/video.mp4",
    "music_url": "https://example.com/music.mp3"
  }'
```

`music_url` 为可选参数。接口会立即返回 `sub_id`：

```json
{
  "sub_id": "a3f2c1d0-0000-0000-0000-000000000000",
  "main_id": "11111111-1111-1111-1111-111111111111",
  "status": "pending"
}
```

### 查询任务状态

```bash
curl http://localhost:8001/status/11111111-1111-1111-1111-111111111111
```

状态包括 `pending`、`processing`、`completed` 和 `failed`。任务完成后，响应中的 `s3_url` 会返回生成的视频地址。

## 部署到 Railway

Railway 会自动识别仓库根目录中的 `Dockerfile`。从 GitHub 仓库创建服务，并在 Railway Variables 中配置：

```env
POSTGRES_URL=your-postgresql-connection-string
AW_ACCESS_KEY_ID=your-access-key
AW_SECRET_ACCESS_KEY=your-secret-key
AW_REGION=eu-central-1
S3_BUCKET=your-bucket-name
FFMPEG_BIN=/usr/bin/ffmpeg
```

将 Railway Healthcheck Path 设置为 `/health`。不要手动固定 `PORT`，Railway 会自动注入端口，Dockerfile 会在运行时使用它。然后在 Railway 网络设置中生成公开域名。

Railway 本地文件系统是临时的。本服务只在处理期间保存临时文件，完成后会上传到 S3，因此长期文件存储应使用 S3。

## 项目结构

```text
.
├── api.py              # FastAPI 服务
├── main.py             # 本地视频合成脚本
├── Dockerfile          # Python + FFmpeg 镜像
├── docker-compose.yml  # 本地 Docker 配置
├── requirements.txt    # Python 依赖
└── .env.example        # 环境变量模板
```

## 安全注意事项

- 不要将 `.env` 或云服务凭据提交到 GitHub。
- 为 S3 使用最小权限 IAM 凭据。
- 对外提供服务前，请校验并限制远程输入 URL。
- 如果输出文件不应公开，请使用私有 S3 或带签名的 URL。

## 许可证

当前项目尚未指定许可证。如果要接受外部贡献或用于商业项目，请先添加许可证。

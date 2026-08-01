# FFmpeg Video Processing API

An asynchronous video processing API built with FastAPI. It downloads a remote MP4 video, MP3 audio, and ASS subtitle file, renders them with FFmpeg, and uploads the result to Amazon S3.

<p align="center">
  <a href="README.zh-CN.md">简体中文</a>
</p>

If this project helps you, please consider giving it a star.

## Features

- Asynchronous video processing
- Loops video to match the audio duration
- Replaces the original audio track
- Burns ASS subtitles with CJK font support
- Optional background music mixing
- Uploads output files to Amazon S3
- PostgreSQL-backed task status tracking
- Docker and Railway ready

## Architecture

```text
Client
  |
  | POST /process
  v
FastAPI
  |-- download MP4 / MP3 / ASS
  |-- FFmpeg: loop video + mix audio + burn subtitles
  |-- upload output to S3
  `-- update status in PostgreSQL
```

## Requirements

- Python 3.12+
- FFmpeg with `libass` support
- PostgreSQL
- Amazon S3 bucket

Docker users do not need to install FFmpeg manually. The included Docker image installs FFmpeg and Noto CJK fonts automatically.

## Quick Start

### 1. Clone the project

```bash
git clone <your-repository-url>
cd video
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```env
POSTGRES_URL=postgresql://user:password@host:5432/database

AW_ACCESS_KEY_ID=your-access-key
AW_SECRET_ACCESS_KEY=your-secret-key
AW_REGION=eu-central-1
S3_BUCKET=your-bucket-name

FFMPEG_BIN=/usr/bin/ffmpeg
LOG_LEVEL=INFO
```

The variable names `AW_ACCESS_KEY_ID` and `AW_SECRET_ACCESS_KEY` are kept for compatibility with the current application code.

### 3. Prepare the database

The service expects these existing tables:

- `ffmpeg_process_main`
- `ffmpeg_process_sub`

`ffmpeg_process_sub.main_id` references `ffmpeg_process_main.id`. Create the parent table first, then create the task table:

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

### 4. Run locally with Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

Open Swagger UI at <http://localhost:8000/docs>.

### 5. Run with Docker

```bash
docker compose up -d --build
```

The API is available at <http://localhost:8001>. FFmpeg runs inside the same container; no separate FFmpeg service is required.

View logs:

```bash
docker compose logs -f video-api
```

## API Usage

### Health check

```bash
curl http://localhost:8001/health
```

```json
{"status":"ok"}
```

### Submit a processing task

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

`music_url` is optional. The response returns a `sub_id` immediately:

```json
{
  "sub_id": "a3f2c1d0-0000-0000-0000-000000000000",
  "main_id": "11111111-1111-1111-1111-111111111111",
  "status": "pending"
}
```

### Query task status

```bash
curl http://localhost:8001/status/11111111-1111-1111-1111-111111111111
```

Possible statuses are `pending`, `processing`, `completed`, and `failed`. When processing is complete, the response contains the S3 URL in `s3_url`.

## Deploy to Railway

Railway automatically detects the `Dockerfile` in the repository root. Create a service from the GitHub repository and configure these variables in the Railway dashboard:

```env
POSTGRES_URL=your-postgresql-connection-string
AW_ACCESS_KEY_ID=your-access-key
AW_SECRET_ACCESS_KEY=your-secret-key
AW_REGION=eu-central-1
S3_BUCKET=your-bucket-name
FFMPEG_BIN=/usr/bin/ffmpeg
```

Set the Railway Healthcheck Path to `/health`. Do not hard-code `PORT`; Railway injects it automatically and the Dockerfile uses it at runtime. Generate a public domain from the Railway service networking settings.

Railway's local filesystem is ephemeral. This service stores temporary media files only during processing and uploads the final result to S3, so long-term output storage should remain in S3.

## Project Structure

```text
.
├── api.py              # FastAPI service
├── main.py             # Standalone local merge script
├── Dockerfile          # Python + FFmpeg image
├── docker-compose.yml  # Local Docker setup
├── requirements.txt    # Python dependencies
└── .env.example        # Environment variable template
```

## Security

- Never commit `.env` or cloud credentials to GitHub.
- Use least-privilege IAM credentials for S3.
- Validate and restrict remote input URLs before exposing this API publicly.
- Use private or signed S3 URLs if output files should not be public.

## License

No license has been specified yet. Add a license before accepting external contributions or using this project commercially.





 - 新增 POST /pictovideo
  - 参数：

    {
      "image_url": "图片链接",
      "mp3_url": "MP3链接"
    }

  - 返回任务 ID：

    {
      "id": "任务UUID",
      "status": "pending"
    }

  - 使用 GET /status/{id} 查询状态
  - 图片生成静态视频，视频时长以 MP3 为准
  - 视频完成后自动上传 S3
  - 返回 s3_url 和 video_url
  - 保留原有 /process、/processV1 和旧状态查询兼容
  - 增加 PostgreSQL 任务表自动初始化
  - 已通过 Python 语法检查和本地 FFmpeg 合成测试

  修改文件：

  - api.py
"""
FastAPI 视频合并服务
- POST /process  接收 ass/mp3/mp4 URL + main_id，异步处理并上传 S3
- GET  /status/{main_id}  查询处理状态及 S3 地址
状态写入 ffmpeg_process_sub 表
"""

import asyncio
import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import asyncpg
import boto3
import ffmpeg
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from typing import Optional

from pydantic import BaseModel

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("video_api")

POSTGRES_URL = os.getenv("POSTGRES_URL")
AWS_ACCESS_KEY_ID = os.getenv("AW_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AW_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AW_REGION", "eu-central-1")
S3_BUCKET = os.getenv("S3_BUCKET")  # 在 .env 中配置桶名

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "/usr/bin/ffmpeg")

# ---------- 数据库连接池 ----------

db_pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    logger.info(
        "startup config: postgres_url_configured=%s aws_key_configured=%s "
        "aws_secret_configured=%s s3_bucket_configured=%s ffmpeg_bin=%s",
        bool(POSTGRES_URL),
        bool(AWS_ACCESS_KEY_ID),
        bool(AWS_SECRET_ACCESS_KEY),
        bool(S3_BUCKET),
        FFMPEG_BIN,
    )
    try:
        db_pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=10)
        logger.info("startup database pool: connected")
    except Exception:
        logger.exception("startup database pool: connection failed")
        raise
    yield
    if db_pool:
        await db_pool.close()
        logger.info("shutdown database pool: closed")


app = FastAPI(title="FFmpeg 视频合并服务", lifespan=lifespan)


@app.get("/health", include_in_schema=False)
async def health_check():
    return {"status": "ok"}


# ---------- 请求模型 ----------

class ProcessRequest(BaseModel):
    main_id: str
    ass_url: str
    mp3_url: str
    mp4_url: str
    music_url: Optional[str] = None


# ---------- 工具函数（同步，在线程池中执行）----------

def _download(url: str, dest: str) -> None:
    urllib.request.urlretrieve(url, dest)


def _get_duration(path: str) -> float:
    probe = ffmpeg.probe(path)
    return float(probe["format"]["duration"])


def _merge(mp4_path: str, mp3_path: str, ass_path: str, output: str, work_dir: str,
           music_path: Optional[str] = None) -> None:
    """循环视频 + 替换音频 + 烧录 ASS 字幕，可选混入背景音乐"""
    duration = _get_duration(mp3_path)
    simple_ass = os.path.join(work_dir, "sub.ass")
    shutil.copy2(ass_path, simple_ass)
    try:
        if music_path:
            cmd = [
                FFMPEG_BIN, "-y",
                "-stream_loop", "-1",
                "-i", mp4_path,
                "-i", mp3_path,
                "-stream_loop", "-1",
                "-i", music_path,
                "-t", str(duration),
                "-filter_complex",
                "[1:a]volume=1.0[voice];[2:a]volume=0.3[music];[voice][music]amix=inputs=2:duration=first[aout]",
                "-map", "0:v:0",
                "-map", "[aout]",
                "-vf", "ass=sub.ass",
                "-vcodec", "libx264",
                "-acodec", "aac",
                output,
            ]
        else:
            cmd = [
                FFMPEG_BIN, "-y",
                "-stream_loop", "-1",
                "-i", mp4_path,
                "-i", mp3_path,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-t", str(duration),
                "-vf", "ass=sub.ass",
                "-vcodec", "libx264",
                "-acodec", "aac",
                output,
            ]
        logger.info("ffmpeg start: %s", shlex.join(cmd))

        process = subprocess.Popen(
            cmd,
            cwd=work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        log_tail: list[str] = []
        if process.stdout:
            for line in process.stdout:
                message = line.rstrip()
                if message:
                    logger.info("ffmpeg: %s", message)
                    log_tail.append(message)
                    log_tail = log_tail[-50:]

        returncode = process.wait()
        logger.info("ffmpeg finished: returncode=%s output=%s", returncode, output)
        if returncode != 0:
            raise RuntimeError(f"ffmpeg 合并失败，returncode={returncode}:\n" + "\n".join(log_tail))

        logger.info("ffmpeg output size: %s bytes", os.path.getsize(output))
    finally:
        if os.path.exists(simple_ass):
            os.remove(simple_ass)


class _S3ProgressLogger:
    def __init__(self, file_path: str, log_every_bytes: int = 5 * 1024 * 1024):
        self.file_path = file_path
        self.total = os.path.getsize(file_path)
        self.transferred = 0
        self.next_log_at = log_every_bytes
        self.log_every_bytes = log_every_bytes

    def __call__(self, bytes_amount: int) -> None:
        self.transferred += bytes_amount
        if self.transferred >= self.next_log_at or self.transferred >= self.total:
            percent = (self.transferred / self.total * 100) if self.total else 100
            logger.info(
                "s3 upload progress: %s/%s bytes %.1f%%",
                min(self.transferred, self.total),
                self.total,
                percent,
            )
            self.next_log_at += self.log_every_bytes


def _upload_s3(file_path: str, s3_key: str) -> str:
    """上传文件到 S3，返回公开访问 URL"""
    file_size = os.path.getsize(file_path)
    logger.info(
        "s3 upload start: file=%s size=%s bucket=%s key=%s",
        file_path,
        file_size,
        S3_BUCKET,
        s3_key,
    )
    s3 = boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        region_name=AWS_REGION,
    )
    s3.upload_file(file_path, S3_BUCKET, s3_key, Callback=_S3ProgressLogger(file_path))
    url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
    logger.info("s3 upload completed: url=%s", url)
    return url


# ---------- 后台任务 ----------

async def _process_task(
    sub_id: str,
    main_id: str,
    ass_url: str,
    mp3_url: str,
    mp4_url: str,
    music_url: Optional[str] = None,
) -> None:
    pool = db_pool
    tmp_dir = tempfile.mkdtemp(prefix=f"ffmpeg_{sub_id}_")
    loop = asyncio.get_event_loop()
    now = lambda: datetime.now(UTC).replace(tzinfo=None)

    try:
        logger.info("task started: sub_id=%s main_id=%s tmp_dir=%s", sub_id, main_id, tmp_dir)
        # 状态 → processing
        await pool.execute(
            'UPDATE "ffmpeg_process_sub" SET status=$1, "updateAt"=$2 WHERE id=$3',
            "processing", now(), uuid.UUID(sub_id),
        )

        mp4_path = os.path.join(tmp_dir, "input.mp4")
        mp3_path = os.path.join(tmp_dir, "input.mp3")
        ass_path = os.path.join(tmp_dir, "input.ass")
        music_path = os.path.join(tmp_dir, "music.mp3") if music_url else None
        output_path = os.path.join(tmp_dir, "output.mp4")

        # 并行下载文件
        logger.info("download start: sub_id=%s music=%s", sub_id, bool(music_url))
        downloads = [
            loop.run_in_executor(None, _download, mp4_url, mp4_path),
            loop.run_in_executor(None, _download, mp3_url, mp3_path),
            loop.run_in_executor(None, _download, ass_url, ass_path),
        ]
        if music_url:
            downloads.append(loop.run_in_executor(None, _download, music_url, music_path))
        await asyncio.gather(*downloads)
        logger.info(
            "download completed: sub_id=%s mp4=%s bytes mp3=%s bytes ass=%s bytes",
            sub_id,
            os.path.getsize(mp4_path),
            os.path.getsize(mp3_path),
            os.path.getsize(ass_path),
        )

        # ffmpeg 合并（CPU 密集，放线程池）
        await loop.run_in_executor(None, _merge, mp4_path, mp3_path, ass_path, output_path, tmp_dir, music_path)

        # 上传 S3
        s3_key = f"ffmpeg-output/{main_id}/{sub_id}.mp4"
        s3_url = await loop.run_in_executor(None, _upload_s3, output_path, s3_key)

        # 状态 → completed，payload 存 s3_url
        await pool.execute(
            'UPDATE "ffmpeg_process_sub" SET status=$1, payload=$2, "updateAt"=$3 WHERE id=$4',
            "completed",
            json.dumps({"s3_url": s3_url}),
            now(),
            uuid.UUID(sub_id),
        )
        logger.info("task completed: sub_id=%s s3_url=%s", sub_id, s3_url)

    except Exception as exc:
        logger.exception("task failed: sub_id=%s main_id=%s", sub_id, main_id)
        await pool.execute(
            'UPDATE "ffmpeg_process_sub" SET status=$1, error_message=$2, "updateAt"=$3 WHERE id=$4',
            "failed", str(exc), now(), uuid.UUID(sub_id),
        )

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------- 接口 ----------

@app.post("/process", summary="提交视频合并任务（异步）")
async def start_process(req: ProcessRequest, background_tasks: BackgroundTasks):
    """
    提交合并任务，立即返回 sub_id 和初始状态 pending。
    后台异步下载 → ffmpeg 合并 → 上传 S3 → 更新状态。
    """
    logger.info(
        "process request: main_id=%r main_id_length=%d ass_url=%s mp3_url=%s "
        "mp4_url=%s music_url=%s",
        req.main_id,
        len(req.main_id),
        bool(req.ass_url),
        bool(req.mp3_url),
        bool(req.mp4_url),
        bool(req.music_url),
    )
    sub_id = str(uuid.uuid4())
    try:
        main_uuid = uuid.UUID(req.main_id)
        logger.info("process validation: main_id_is_valid_uuid=true sub_id=%s", sub_id)

        if db_pool is None:
            raise RuntimeError("database pool is not initialized")

        logger.info("process database insert: start sub_id=%s", sub_id)
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO "ffmpeg_process_sub" (id, main_id, name, status)
                VALUES ($1, $2, $3, $4)
                """,
                uuid.UUID(sub_id),
                main_uuid,
                "video_merge",
                "pending",
            )
        logger.info("process database insert: success sub_id=%s", sub_id)

        background_tasks.add_task(
            _process_task, sub_id, req.main_id, req.ass_url, req.mp3_url, req.mp4_url, req.music_url
        )
        logger.info("process background task: scheduled sub_id=%s", sub_id)
        return {"sub_id": sub_id, "main_id": req.main_id, "status": "pending"}
    except ValueError:
        logger.exception(
            "process validation failed: invalid main_id main_id=%r "
            "main_id_length=%d sub_id=%s",
            req.main_id,
            len(req.main_id),
            sub_id,
        )
        raise HTTPException(
            status_code=422,
            detail="main_id 必须是合法的 UUID",
        )
    except Exception:
        logger.exception("process request failed: sub_id=%s", sub_id)
        raise


@app.get("/status/{main_id}", summary="查询任务状态")
async def get_status(main_id: str):
    """
    按 main_id 查询最新一条任务记录。
    completed 时 payload.s3_url 即为 S3 文件地址。
    """
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, status, payload, error_message, "createAt", "updateAt"
            FROM "ffmpeg_process_sub"
            WHERE main_id = $1
            ORDER BY
                CASE status
                    WHEN 'completed' THEN 0
                    WHEN 'failed'    THEN 1
                    WHEN 'processing' THEN 2
                    ELSE 3
                END,
                "updateAt" DESC
            LIMIT 1
            """,
            uuid.UUID(main_id),
        )

    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到 main_id={main_id} 的任务记录")

    payload = json.loads(row["payload"]) if row["payload"] else None
    s3_url = payload.get("s3_url") if isinstance(payload, dict) else None

    return {
        "sub_id": str(row["id"]),
        "main_id": main_id,
        "status": row["status"],
        "s3_url": s3_url,
        "error_message": row["error_message"],
        "created_at": row["createAt"].isoformat() if row["createAt"] else None,
        "updated_at": row["updateAt"].isoformat() if row["updateAt"] else None,
    }

"""
FastAPI 视频合并服务
- POST /process       接收 ass/mp3/mp4 URL + main_id，异步处理并上传 S3
- POST /processV1     顺序拼接多个 MP4 并混入背景音乐
- POST /pictovideo    接收图片 URL + MP3 URL + 可选 seconds，生成静态图片视频
- GET  /status/{id}   查询任务状态及 S3 地址
"""

import asyncio
import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.request
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import asyncpg
import boto3
from boto3.s3.transfer import TransferConfig
import ffmpeg
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from typing import Optional

from pydantic import BaseModel, Field

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
        await db_pool.execute(
            '''
            CREATE TABLE IF NOT EXISTS "pictovideo_tasks" (
                "id" uuid PRIMARY KEY,
                "image_url" text NOT NULL,
                "mp3_url" text NOT NULL,
                "seconds" integer NOT NULL DEFAULT 60,
                "status" varchar(20) NOT NULL,
                "payload" jsonb,
                "error_message" text,
                "createAt" timestamp DEFAULT now() NOT NULL,
                "updateAt" timestamp DEFAULT now() NOT NULL
            )
            '''
        )
        await db_pool.execute(
            'ALTER TABLE "pictovideo_tasks" ADD COLUMN IF NOT EXISTS "seconds" integer NOT NULL DEFAULT 60'
        )
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


class ProcessV1Request(BaseModel):
    main_id: str
    mp4_urls: list[str]   # 按顺序拼接的 MP4 URL 列表
    mp3_url: str          # 背景音乐


class PictoVideoRequest(BaseModel):
    image_url: str        # 图片地址
    mp3_url: str          # 背景音乐地址
    seconds: int = Field(default=60, gt=0)  # 视频时长（秒）


# ---------- 工具函数（同步，在线程池中执行）----------

def _download(url: str, dest: str, retries: int = 3) -> None:
    """下载文件，自动重试以应对 SSL EOF 等瞬时网络错误"""
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=Retry(
        total=retries,
        backoff_factor=2,
        status_forcelist=[500, 502, 503, 504],
    ))
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    for attempt in range(1, retries + 2):
        try:
            with session.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
            return
        except Exception as e:
            if attempt > retries:
                raise
            wait = 2 ** attempt
            logger.warning("download attempt %d failed (%s), retry in %ds: %s", attempt, e, wait, url)
            time.sleep(wait)


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
    def __init__(self, file_path: str, s3_key: str, log_interval_bytes: int = 2 * 1024 * 1024):
        self.s3_key = s3_key
        self.total = os.path.getsize(file_path)
        self.transferred = 0
        self.next_log_at = log_interval_bytes
        self.log_interval_bytes = log_interval_bytes
        self.start_time = time.monotonic()

    def __call__(self, bytes_amount: int) -> None:
        self.transferred += bytes_amount
        done = self.transferred >= self.total
        if self.transferred >= self.next_log_at or done:
            elapsed = time.monotonic() - self.start_time
            percent = (self.transferred / self.total * 100) if self.total else 100
            speed_mb = (self.transferred / elapsed / 1024 / 1024) if elapsed > 0 else 0
            transferred_mb = min(self.transferred, self.total) / 1024 / 1024
            total_mb = self.total / 1024 / 1024
            eta = ((self.total - self.transferred) / (self.transferred / elapsed)) if (elapsed > 0 and self.transferred < self.total) else 0
            logger.info(
                "s3 upload progress: key=%s  %.1f/%.1f MB  %.1f%%  speed=%.1f MB/s  eta=%.0fs",
                self.s3_key,
                transferred_mb,
                total_mb,
                percent,
                speed_mb,
                eta,
            )
            self.next_log_at += self.log_interval_bytes
            if done:
                elapsed_total = time.monotonic() - self.start_time
                avg_speed = self.total / elapsed_total / 1024 / 1024 if elapsed_total > 0 else 0
                logger.info(
                    "s3 upload done: key=%s  total=%.1f MB  elapsed=%.1fs  avg_speed=%.1f MB/s",
                    self.s3_key,
                    total_mb,
                    elapsed_total,
                    avg_speed,
                )


_S3_TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=8 * 1024 * 1024,   # 文件 > 8MB 启用分片
    multipart_chunksize=8 * 1024 * 1024,   # 每片 8MB
    max_concurrency=10,                     # 最多 10 个并发线程同时上传
    use_threads=True,
)


def _upload_s3(file_path: str, s3_key: str) -> str:
    """上传文件到 S3，返回公开访问 URL（多线程分片并发加速）"""
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
    s3.upload_file(
        file_path, S3_BUCKET, s3_key,
        Config=_S3_TRANSFER_CONFIG,
        Callback=_S3ProgressLogger(file_path, s3_key),
    )
    url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
    logger.info("s3 upload completed: url=%s", url)
    return url


# ---------- 工具函数：V1 ----------

def _concat_mp4s(video_paths: list[str], output: str, work_dir: str) -> None:
    """用 ffmpeg concat demuxer 按顺序拼接多个 MP4（直接 copy，不重编码）"""
    list_file = os.path.join(work_dir, "concat_list.txt")
    with open(list_file, "w") as f:
        for p in video_paths:
            f.write(f"file '{p}'\n")

    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "concat", "-safe", "0",
        "-i", list_file,
        "-c", "copy",
        output,
    ]
    logger.info("ffmpeg concat start: %s", shlex.join(cmd))
    process = subprocess.Popen(
        cmd, cwd=work_dir,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    log_tail: list[str] = []
    if process.stdout:
        for line in process.stdout:
            msg = line.rstrip()
            if msg:
                logger.info("ffmpeg: %s", msg)
                log_tail.append(msg)
                log_tail = log_tail[-50:]
    rc = process.wait()
    logger.info("ffmpeg concat finished: returncode=%s output=%s", rc, output)
    if rc != 0:
        raise RuntimeError(f"ffmpeg concat 失败 returncode={rc}:\n" + "\n".join(log_tail))


def _mix_background_music(video_path: str, music_path: str, output: str, work_dir: str) -> None:
    """将背景音乐循环填充至视频时长，作为唯一音轨写入输出"""
    duration = _get_duration(video_path)
    cmd = [
        FFMPEG_BIN, "-y",
        "-i", video_path,
        "-stream_loop", "-1",
        "-i", music_path,
        "-t", str(duration),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-vcodec", "libx264",
        "-acodec", "aac",
        output,
    ]
    logger.info("ffmpeg mix music start: duration=%.2f %s", duration, shlex.join(cmd))
    process = subprocess.Popen(
        cmd, cwd=work_dir,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    log_tail: list[str] = []
    if process.stdout:
        for line in process.stdout:
            msg = line.rstrip()
            if msg:
                logger.info("ffmpeg: %s", msg)
                log_tail.append(msg)
                log_tail = log_tail[-50:]
    rc = process.wait()
    logger.info("ffmpeg mix music finished: returncode=%s output=%s", rc, output)
    if rc != 0:
        raise RuntimeError(f"ffmpeg 混音失败 returncode={rc}:\n" + "\n".join(log_tail))


def _image_to_video(image_path: str, mp3_path: str, output: str, work_dir: str, seconds: int) -> None:
    """将一张图片延展为指定时长的视频，并循环/截断 MP3 作为音轨。"""
    duration = float(seconds)
    cmd = [
        FFMPEG_BIN, "-y",
        "-loop", "1",
        "-i", image_path,
        "-stream_loop", "-1",
        "-i", mp3_path,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-t", str(duration),
        "-r", "30",
        # yuv420p/libx264 要求宽高均为偶数；奇数边最多裁掉 1 像素。
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        output,
    ]
    logger.info("ffmpeg pictovideo start: duration=%.2f %s", duration, shlex.join(cmd))
    process = subprocess.Popen(
        cmd, cwd=work_dir,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    log_tail: list[str] = []
    if process.stdout:
        for line in process.stdout:
            msg = line.rstrip()
            if msg:
                logger.info("ffmpeg: %s", msg)
                log_tail.append(msg)
                log_tail = log_tail[-50:]
    rc = process.wait()
    logger.info("ffmpeg pictovideo finished: returncode=%s output=%s", rc, output)
    if rc != 0:
        raise RuntimeError(f"ffmpeg 图片转视频失败 returncode={rc}:\n" + "\n".join(log_tail))


async def _process_pictovideo_task(task_id: str, image_url: str, mp3_url: str, seconds: int) -> None:
    pool = db_pool
    tmp_dir = tempfile.mkdtemp(prefix=f"pictovideo_{task_id}_")
    loop = asyncio.get_event_loop()
    now = lambda: datetime.now(UTC).replace(tzinfo=None)

    try:
        await pool.execute(
            'UPDATE "pictovideo_tasks" SET status=$1, "updateAt"=$2 WHERE id=$3',
            "processing", now(), uuid.UUID(task_id),
        )
        image_path = os.path.join(tmp_dir, "input_image")
        mp3_path = os.path.join(tmp_dir, "input.mp3")
        output_path = os.path.join(tmp_dir, "output.mp4")

        await asyncio.gather(
            loop.run_in_executor(None, _download, image_url, image_path),
            loop.run_in_executor(None, _download, mp3_url, mp3_path),
        )
        await loop.run_in_executor(
            None, _image_to_video, image_path, mp3_path, output_path, tmp_dir, seconds
        )

        s3_key = f"pictovideo-output/{task_id}.mp4"
        s3_url = await loop.run_in_executor(None, _upload_s3, output_path, s3_key)
        await pool.execute(
            'UPDATE "pictovideo_tasks" SET status=$1, payload=$2, "updateAt"=$3 WHERE id=$4',
            "completed", json.dumps({"s3_url": s3_url}), now(), uuid.UUID(task_id),
        )
        logger.info("pictovideo task completed: id=%s s3_url=%s", task_id, s3_url)
    except Exception as exc:
        logger.exception("pictovideo task failed: id=%s", task_id)
        await pool.execute(
            'UPDATE "pictovideo_tasks" SET status=$1, error_message=$2, "updateAt"=$3 WHERE id=$4',
            "failed", str(exc), now(), uuid.UUID(task_id),
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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


async def _process_v1_task(
    sub_id: str,
    main_id: str,
    mp4_urls: list[str],
    mp3_url: str,
) -> None:
    pool = db_pool
    tmp_dir = tempfile.mkdtemp(prefix=f"ffmpegv1_{sub_id}_")
    loop = asyncio.get_event_loop()
    now = lambda: datetime.now(UTC).replace(tzinfo=None)

    try:
        logger.info(
            "v1 task started: sub_id=%s main_id=%s mp4_count=%d tmp_dir=%s",
            sub_id, main_id, len(mp4_urls), tmp_dir,
        )
        await pool.execute(
            'UPDATE "ffmpeg_process_sub" SET status=$1, "updateAt"=$2 WHERE id=$3',
            "processing", now(), uuid.UUID(sub_id),
        )

        # 准备文件路径
        mp4_paths = [os.path.join(tmp_dir, f"input_{i:03d}.mp4") for i in range(len(mp4_urls))]
        mp3_path = os.path.join(tmp_dir, "music.mp3")
        concat_path = os.path.join(tmp_dir, "concat.mp4")
        output_path = os.path.join(tmp_dir, "output.mp4")

        # 并行下载所有 MP4 + MP3
        logger.info("v1 download start: sub_id=%s", sub_id)
        downloads = [loop.run_in_executor(None, _download, url, path) for url, path in zip(mp4_urls, mp4_paths)]
        downloads.append(loop.run_in_executor(None, _download, mp3_url, mp3_path))
        await asyncio.gather(*downloads)
        logger.info(
            "v1 download completed: sub_id=%s mp4_sizes=%s mp3_size=%s",
            sub_id,
            [os.path.getsize(p) for p in mp4_paths],
            os.path.getsize(mp3_path),
        )

        # 拼接 MP4
        await loop.run_in_executor(None, _concat_mp4s, mp4_paths, concat_path, tmp_dir)

        # 混入背景音乐
        await loop.run_in_executor(None, _mix_background_music, concat_path, mp3_path, output_path, tmp_dir)

        # 上传 S3
        s3_key = f"ffmpeg-output/{main_id}/{sub_id}.mp4"
        s3_url = await loop.run_in_executor(None, _upload_s3, output_path, s3_key)

        await pool.execute(
            'UPDATE "ffmpeg_process_sub" SET status=$1, payload=$2, "updateAt"=$3 WHERE id=$4',
            "completed",
            json.dumps({"s3_url": s3_url}),
            now(),
            uuid.UUID(sub_id),
        )
        logger.info("v1 task completed: sub_id=%s s3_url=%s", sub_id, s3_url)

    except Exception as exc:
        logger.exception("v1 task failed: sub_id=%s main_id=%s", sub_id, main_id)
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


@app.post("/processV1", summary="多 MP4 顺序拼接 + 背景音乐（异步）")
async def start_process_v1(req: ProcessV1Request, background_tasks: BackgroundTasks):
    """
    提交多视频拼接任务，立即返回 sub_id 和初始状态 pending。
    后台流程：并行下载所有 MP4 → 按顺序拼接 → 混入背景音乐 → 上传 S3 → 更新状态。
    可通过 GET /status/{main_id} 轮询结果。
    """
    if not req.mp4_urls:
        raise HTTPException(status_code=422, detail="mp4_urls 不能为空")

    logger.info(
        "processV1 request: main_id=%r mp4_count=%d mp3_url=%s",
        req.main_id, len(req.mp4_urls), bool(req.mp3_url),
    )
    sub_id = str(uuid.uuid4())
    try:
        main_uuid = uuid.UUID(req.main_id)

        if db_pool is None:
            raise RuntimeError("database pool is not initialized")

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO "ffmpeg_process_sub" (id, main_id, name, status)
                VALUES ($1, $2, $3, $4)
                """,
                uuid.UUID(sub_id),
                main_uuid,
                "video_concat_v1",
                "pending",
            )

        background_tasks.add_task(_process_v1_task, sub_id, req.main_id, req.mp4_urls, req.mp3_url)
        logger.info("processV1 background task scheduled: sub_id=%s", sub_id)
        return {"sub_id": sub_id, "main_id": req.main_id, "status": "pending"}
    except ValueError:
        raise HTTPException(status_code=422, detail="main_id 必须是合法的 UUID")
    except Exception:
        logger.exception("processV1 request failed: sub_id=%s", sub_id)
        raise


@app.post("/pictovideo", summary="图片 + MP3 生成视频（异步）")
async def start_pictovideo(req: PictoVideoRequest, background_tasks: BackgroundTasks):
    """提交图片转视频任务，返回任务 id；视频完成后返回 S3 地址。"""
    if db_pool is None:
        raise HTTPException(status_code=503, detail="database pool is not initialized")

    task_id = str(uuid.uuid4())
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                '''
                INSERT INTO "pictovideo_tasks" (id, image_url, mp3_url, seconds, status)
                VALUES ($1, $2, $3, $4, $5)
                ''',
                uuid.UUID(task_id), req.image_url, req.mp3_url, req.seconds, "pending",
            )
        background_tasks.add_task(
            _process_pictovideo_task, task_id, req.image_url, req.mp3_url, req.seconds
        )
        return {"id": task_id, "status": "pending"}
    except Exception:
        logger.exception("pictovideo request failed: id=%s", task_id)
        raise


@app.get("/status/{id}", summary="查询任务状态")
async def get_status(id: str):
    """
    新接口按 pictovideo 任务 id 查询；旧接口仍可传 main_id 查询。
    completed 时 payload.s3_url 即为 S3 文件地址。
    """
    if db_pool is None:
        raise HTTPException(status_code=503, detail="database pool is not initialized")

    try:
        task_uuid = uuid.UUID(id)
    except ValueError:
        raise HTTPException(status_code=422, detail="id 必须是合法的 UUID")

    # 新的 pictovideo 接口按任务 id 查询；找不到时继续兼容旧的 main_id 查询。
    async with db_pool.acquire() as conn:
        pictovideo_row = await conn.fetchrow(
            '''
            SELECT id, status, payload, error_message, "createAt", "updateAt"
            FROM "pictovideo_tasks"
            WHERE id = $1
            ''',
            task_uuid,
        )
        if pictovideo_row is not None:
            payload = json.loads(pictovideo_row["payload"]) if pictovideo_row["payload"] else None
            s3_url = payload.get("s3_url") if isinstance(payload, dict) else None
            return {
                "id": str(pictovideo_row["id"]),
                "status": pictovideo_row["status"],
                "s3_url": s3_url,
                "video_url": s3_url,
                "error_message": pictovideo_row["error_message"],
                "created_at": pictovideo_row["createAt"].isoformat() if pictovideo_row["createAt"] else None,
                "updated_at": pictovideo_row["updateAt"].isoformat() if pictovideo_row["updateAt"] else None,
            }

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
            task_uuid,
        )

    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到 id={id} 的任务记录")

    payload = json.loads(row["payload"]) if row["payload"] else None
    s3_url = payload.get("s3_url") if isinstance(payload, dict) else None

    return {
        "sub_id": str(row["id"]),
        "main_id": id,
        "status": row["status"],
        "s3_url": s3_url,
        "error_message": row["error_message"],
        "created_at": row["createAt"].isoformat() if row["createAt"] else None,
        "updated_at": row["updateAt"].isoformat() if row["updateAt"] else None,
    }

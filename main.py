"""
合并素材脚本
从 1.txt 读取 MP4/MP3/ASS 的 URL，下载后合并：
- 视频循环播放填满 MP3 时长
- 替换音频为 MP3
- 烧录 ASS 字幕（使用 ffmpeg-full + libass）
- 输出 output.mp4
"""

import os
import re
import shutil
import subprocess
import urllib.request

import ffmpeg

MATERIALS_FILE = os.path.join(os.path.dirname(__file__), "1.txt")
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "output.mp4")
TMP_DIR = os.path.join(os.path.dirname(__file__), "tmp")

# ffmpeg-full 含 libass，支持烧录 ASS 字幕
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "/usr/bin/ffmpeg")


def parse_urls(path: str) -> dict[str, str]:
    """从 txt 文件中按扩展名解析 URL。"""
    urls: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            line = re.sub(r"^\d+\s+", "", line)
            if not line:
                continue
            ext = line.rsplit(".", 1)[-1].lower()
            if ext in ("mp4", "mp3", "ass"):
                urls[ext] = line
    return urls


def download(url: str, dest: str) -> None:
    """下载文件，若已存在则跳过。"""
    if os.path.exists(dest):
        print(f"  已存在，跳过: {dest}")
        return
    print(f"  下载中: {url}")
    urllib.request.urlretrieve(url, dest)
    print(f"  保存到: {dest}")


def get_duration(path: str) -> float:
    """用 ffprobe 获取媒体时长（秒）。"""
    probe = ffmpeg.probe(path)
    return float(probe["format"]["duration"])


def merge(mp4_path: str, mp3_path: str, ass_path: str, output: str) -> None:
    """合并：视频循环填满 MP3 时长 + 替换音频 + 烧录 ASS 字幕。"""
    duration = get_duration(mp3_path)
    print(f"  MP3 时长: {duration:.2f}s，视频将循环填满此长度")

    # ASS 复制到输出目录用简短文件名，规避 ffmpeg filter 路径解析问题
    out_dir = os.path.dirname(os.path.abspath(output))
    simple_ass = os.path.join(out_dir, "sub.ass")
    shutil.copy2(ass_path, simple_ass)

    try:
        cmd = [
            FFMPEG_BIN, "-y",
            "-stream_loop", "-1",  # 视频无限循环
            "-i", mp4_path,
            "-i", mp3_path,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-t", str(duration),   # 以 MP3 时长为准截断
            "-vf", "ass=sub.ass",  # 烧录字幕（相对路径，cwd=out_dir）
            "-vcodec", "libx264",
            "-acodec", "aac",
            output,
        ]
        print("  执行:", " ".join(cmd))
        result = subprocess.run(cmd, cwd=out_dir)
        if result.returncode != 0:
            raise RuntimeError("ffmpeg 合并失败，请查看上方日志")
    finally:
        if os.path.exists(simple_ass):
            os.remove(simple_ass)


def main() -> None:
    os.makedirs(TMP_DIR, exist_ok=True)

    print("解析素材文件...")
    urls = parse_urls(MATERIALS_FILE)
    for ext in ("mp4", "mp3", "ass"):
        if ext not in urls:
            raise ValueError(f"1.txt 中未找到 .{ext} 的 URL")
    print(f"  MP4: {urls['mp4']}")
    print(f"  MP3: {urls['mp3']}")
    print(f"  ASS: {urls['ass']}")

    mp4_path = os.path.join(TMP_DIR, "input.mp4")
    mp3_path = os.path.join(TMP_DIR, "input.mp3")
    ass_path = os.path.join(TMP_DIR, "input.ass")

    print("\n下载素材...")
    download(urls["mp4"], mp4_path)
    download(urls["mp3"], mp3_path)
    download(urls["ass"], ass_path)

    print(f"\n合并中，输出到: {OUTPUT_FILE}")
    merge(mp4_path, mp3_path, ass_path, OUTPUT_FILE)
    print("\n完成！输出文件:", OUTPUT_FILE)


if __name__ == "__main__":
    main()

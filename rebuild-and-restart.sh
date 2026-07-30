#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "错误：未找到 docker 命令。" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "错误：当前 Docker 不支持 docker compose 命令。" >&2
  exit 1
fi

echo "正在构建最新镜像并重新创建容器..."
docker compose up -d --build --force-recreate --remove-orphans

echo
echo "容器状态："
docker compose ps

echo
echo "服务已重建并启动。查看实时日志："
echo "  docker compose logs -f video-api"

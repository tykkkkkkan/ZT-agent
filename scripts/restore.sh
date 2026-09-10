#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
# ZT-agent 数据恢复脚本（与 backup.sh 对应）
# 用法：
#   ./scripts/restore.sh backups/20260910-120000
# ════════════════════════════════════════════════════════════════
set -euo pipefail

SRC="${1:-}"
if [ -z "$SRC" ] || [ ! -d "$SRC" ]; then
  echo "用法: ./scripts/restore.sh <备份目录>" >&2
  exit 1
fi

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

DB_NAME="${DB_NAME:-agent_db}"
DB_USER="${DB_USER:-root}"
DB_PASSWORD="${DB_PASSWORD:-123456}"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-3306}"
CHROMA_DIR="${CHROMA_DIR:-chroma_data}"

if [ -f "$SRC/db.sql" ]; then
  echo "==> 恢复 MySQL 库 '$DB_NAME' ..."
  MYSQL_PWD="$DB_PASSWORD" mysql \
    -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" "$DB_NAME" < "$SRC/db.sql"
  echo "    数据库已恢复"
else
  echo "    [WARN] $SRC/db.sql 不存在，跳过数据库恢复" >&2
fi

if [ -f "$SRC/chroma_data.tar.gz" ]; then
  echo "==> 恢复 Chroma 向量库 ..."
  rm -rf "$CHROMA_DIR"
  tar -xzf "$SRC/chroma_data.tar.gz"
  echo "    向量库已恢复"
else
  echo "    [INFO] $SRC/chroma_data.tar.gz 不存在，跳过向量库恢复"
fi

echo "==> 恢复完成。建议随后执行：python manage.py rebuild_rag"

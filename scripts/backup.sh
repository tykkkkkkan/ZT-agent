#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
# ZT-agent 数据备份脚本
#   - MySQL 业务库（agent_db）逻辑导出
#   - Chroma 向量库持久化目录打包
# 用法：
#   ./scripts/backup.sh                 # 备份到 ./backups/YYYYMMDD-HHMMSS/
#   ./scripts/backup.sh /path/to/out   # 指定输出目录
# ════════════════════════════════════════════════════════════════
set -euo pipefail

# 读取 .env（若存在）
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

OUT_DIR="${1:-backups/$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT_DIR"

echo "==> 备份 MySQL 库 '$DB_NAME' ..."
if command -v mysqldump >/dev/null 2>&1; then
  MYSQL_PWD="$DB_PASSWORD" mysqldump \
    -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" \
    --single-transaction --routines --triggers "$DB_NAME" \
    > "$OUT_DIR/db.sql"
  echo "    已导出 $OUT_DIR/db.sql ($(wc -c < "$OUT_DIR/db.sql") bytes)"
else
  echo "    [WARN] 未找到 mysqldump，跳过数据库备份" >&2
fi

echo "==> 打包 Chroma 向量库 '$CHROMA_DIR' ..."
if [ -d "$CHROMA_DIR" ]; then
  tar -czf "$OUT_DIR/chroma_data.tar.gz" "$CHROMA_DIR"
  echo "    已打包 $OUT_DIR/chroma_data.tar.gz"
else
  echo "    [INFO] $CHROMA_DIR 不存在，跳过向量库打包"
fi

echo "==> 备份完成：$OUT_DIR"

#!/usr/bin/env bash
# =========================================================
# Menerapkan perubahan schema ke database yang SUDAH BERISI DATA.
#
# Kenapa perlu: file di db/init/ hanya dijalankan Postgres saat
# volume database masih kosong. Setelah NMS berjalan, penambahan
# kolom/tabel harus diterapkan manual — itulah tugas script ini.
#
# Semua file migrasi ditulis idempotent, jadi aman dijalankan
# berulang kali.
#
# Pakai:  ./scripts/upgrade-db.sh
# =========================================================
set -euo pipefail

cd "$(dirname "$0")/.."

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[ OK ]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }

if [[ ! -f .env ]]; then
    fail ".env tidak ada. Jalankan dari folder project."
    exit 1
fi
# shellcheck disable=SC1091
set -a; source .env; set +a

DB_USER="${POSTGRES_USER:-nms}"
DB_NAME="${POSTGRES_DB:-nms}"

if ! docker compose ps db --status running -q > /dev/null 2>&1; then
    fail "Container db tidak berjalan. Jalankan: docker compose up -d db"
    exit 1
fi

echo "Backup dulu — batalkan dengan Ctrl+C kalau belum siap."
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="backup-sebelum-upgrade-${STAMP}.sql.gz"
docker compose exec -T db pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$BACKUP"
ok "Backup tersimpan: $BACKUP ($(du -h "$BACKUP" | cut -f1))"

for f in db/init/0*.sql; do
    base=$(basename "$f")
    # 01_schema.sql berisi CREATE TABLE tanpa IF NOT EXISTS —
    # hanya untuk database baru, tidak boleh diulang di sini.
    if [[ "$base" == "01_schema.sql" ]]; then
        continue
    fi
    echo "--- Menerapkan $base ---"
    if docker compose exec -T db psql -v ON_ERROR_STOP=1 -q -U "$DB_USER" -d "$DB_NAME" < "$f"; then
        ok "$base diterapkan"
    else
        fail "$base gagal. Database tidak berubah untuk file ini."
        fail "Pulihkan dengan: gunzip -c $BACKUP | docker compose exec -T db psql -U $DB_USER -d $DB_NAME"
        exit 1
    fi
done

echo ""
ok "Schema terbaru. Bangun ulang service:"
echo "     docker compose up -d --build"

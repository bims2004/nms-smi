#!/usr/bin/env bash
# =========================================================
# Pulihkan database dari backup.
#
# INI MENGHAPUS SEMUA DATA YANG ADA SEKARANG dan menggantinya dengan isi
# backup. Tidak ada pembatalan.
#
# Pakai:  ./scripts/restore.sh backups/nms-20260717-020000.sql.gz
# =========================================================
set -uo pipefail
cd "$(dirname "$0")/.."

G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; N='\033[0m'
ok()  { echo -e "${G}[ OK ]${N} $*"; }
bad() { echo -e "${R}[GAGAL]${N} $*"; }

FILE="${1:-}"
if [[ -z "$FILE" ]]; then
    bad "Sebutkan berkas backup-nya."
    echo "Pakai: ./scripts/restore.sh backups/nms-YYYYMMDD-HHMMSS.sql.gz"
    echo ""
    echo "Yang tersedia:"
    ls -lh backups/nms-*.sql.gz 2>/dev/null | awk '{print "  " $9 "  (" $5 ", " $6 " " $7 ")"}' \
        || echo "  (tidak ada di ./backups)"
    exit 1
fi
[[ -f "$FILE" ]] || { bad "Berkas tidak ada: $FILE"; exit 1; }

if ! gzip -t "$FILE" 2>/dev/null; then
    bad "Berkas rusak — gzip menolaknya. Jangan dipakai."
    exit 1
fi
ok "Berkas backup utuh"

set -a; source .env; set +a
DB_USER="${POSTGRES_USER:-nms}"
DB_NAME="${POSTGRES_DB:-nms}"

if [[ "$(docker compose ps db --format '{{.State}}' 2>/dev/null | head -1)" != "running" ]]; then
    bad "Container db tidak berjalan."
    exit 1
fi

echo ""
echo -e "${Y}Isi database '$DB_NAME' sekarang:${NC}"
docker compose exec -T db psql -U "$DB_USER" -d "$DB_NAME" -t -c "
    SELECT '  ' || count(*) || ' pelanggan' FROM customers;
    SELECT '  ' || count(*) || ' gangguan tercatat' FROM alerts;
    SELECT '  ' || count(*) || ' sampel traffic' FROM traffic_samples;
" 2>/dev/null

echo ""
echo -e "${R}SEMUA DATA DI ATAS AKAN DIHAPUS${N} dan diganti isi $FILE"
echo "Tidak ada pembatalan setelah ini."
read -rp "Ketik 'PULIHKAN' untuk lanjut: " jawab
[[ "$jawab" == "PULIHKAN" ]] || { echo "Dibatalkan."; exit 1; }

# Backup keadaan sekarang dulu — kalau ternyata backup-nya yang salah pilih,
# masih ada jalan pulang.
STAMP=$(date +%Y%m%d-%H%M%S)
JARING="backups/sebelum-restore-${STAMP}.sql.gz"
mkdir -p backups
echo "Mengamankan keadaan sekarang ke $JARING ..."
docker compose exec -T db pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$JARING"
ok "Jaring pengaman: $JARING"

echo "Menghentikan collector & alerter supaya tidak menulis saat restore..."
docker compose stop collector alerter > /dev/null 2>&1

echo "Memulihkan..."
docker compose exec -T db psql -q -U "$DB_USER" -d postgres -c \
    "DROP DATABASE IF EXISTS ${DB_NAME}_lama;" > /dev/null 2>&1
docker compose exec -T db psql -q -U "$DB_USER" -d postgres \
    -c "DROP DATABASE $DB_NAME;" -c "CREATE DATABASE $DB_NAME;" > /dev/null

if gunzip -c "$FILE" | docker compose exec -T db psql -q -v ON_ERROR_STOP=1 \
        -U "$DB_USER" -d "$DB_NAME" > /dev/null; then
    ok "Database dipulihkan dari $FILE"
else
    bad "Restore gagal. Jaring pengaman ada di: $JARING"
    docker compose start collector alerter > /dev/null 2>&1
    exit 1
fi

docker compose start collector alerter > /dev/null 2>&1
ok "collector & alerter dijalankan lagi"

docker compose exec -T db psql -U "$DB_USER" -d "$DB_NAME" -t -c "
    SELECT '  ' || count(*) || ' pelanggan' FROM customers;
    SELECT '  ' || count(*) || ' gangguan tercatat' FROM alerts;
    SELECT '  ' || count(*) || ' sampel traffic' FROM traffic_samples;
" 2>/dev/null

echo ""
ok "Selesai. Periksa dashboard, lalu jalankan ./scripts/diagnose.sh"

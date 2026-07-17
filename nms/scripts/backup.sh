#!/usr/bin/env bash
# =========================================================
# Backup database NMS.
#
# Yang tidak bisa dibuat ulang kalau server ini mati: riwayat gangguan dan
# laporan SLA. Data traffic bisa dikumpulkan lagi mulai besok; riwayat
# gangguan tidak bisa — dan itu yang dikirim ke pelanggan.
#
# Pakai:
#   ./scripts/backup.sh              backup ke ./backups
#   ./scripts/backup.sh /mnt/nas     backup ke folder lain
#
# Otomatis tiap hari jam 2 pagi (jalankan sebagai user pemilik folder):
#   crontab -e
#   0 2 * * * cd /home/bimma/nms-smi && ./scripts/backup.sh >> backup.log 2>&1
# =========================================================
set -uo pipefail
cd "$(dirname "$0")/.."

G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; N='\033[0m'
ok()   { echo -e "${G}[ OK ]${N} $*"; }
bad()  { echo -e "${R}[GAGAL]${N} $*"; }
warn() { echo -e "${Y}[PERHATIAN]${N} $*"; }

TUJUAN="${1:-./backups}"
SIMPAN_HARI="${BACKUP_KEEP_DAYS:-30}"

if [[ ! -f .env ]]; then
    bad ".env tidak ada — jalankan dari folder project."
    exit 1
fi
set -a; source .env; set +a
DB_USER="${POSTGRES_USER:-nms}"
DB_NAME="${POSTGRES_DB:-nms}"

if [[ "$(docker compose ps db --format '{{.State}}' 2>/dev/null | head -1)" != "running" ]]; then
    bad "Container db tidak berjalan — tidak ada yang bisa di-backup."
    exit 1
fi

mkdir -p "$TUJUAN" || { bad "Tidak bisa menulis ke $TUJUAN"; exit 1; }

STAMP=$(date +%Y%m%d-%H%M%S)
FILE="$TUJUAN/nms-${STAMP}.sql.gz"

echo "Backup ke $FILE ..."
if ! docker compose exec -T db pg_dump -U "$DB_USER" "$DB_NAME" \
        | gzip > "$FILE"; then
    bad "pg_dump gagal."
    rm -f "$FILE"
    exit 1
fi

# Backup yang tidak pernah diperiksa bukan backup — cuma perasaan aman.
# Dua pemeriksaan murah yang menangkap kegagalan paling umum:
# file terpotong (disk penuh di tengah jalan) dan dump kosong.
if ! gzip -t "$FILE" 2>/dev/null; then
    bad "Berkas rusak (gzip gagal diuji). Disk penuh?"
    rm -f "$FILE"
    exit 1
fi

BARIS=$(gunzip -c "$FILE" | grep -c "^COPY \|^INSERT INTO " || true)
UKURAN=$(du -h "$FILE" | cut -f1)
if [[ "$BARIS" -lt 1 ]]; then
    bad "Dump tidak berisi satu pun tabel berdata. Jangan dipercaya."
    exit 1
fi

# Tabel yang paling tidak tergantikan harus benar-benar ada di dalamnya
for t in alerts customers devices; do
    if ! gunzip -c "$FILE" | grep -q "COPY public.${t} "; then
        warn "Tabel '$t' tidak ditemukan di dump — periksa manual."
    fi
done

ok "Backup selesai: $FILE ($UKURAN, $BARIS blok data)"

# ---------- buang yang lama ----------
if [[ "$SIMPAN_HARI" -gt 0 ]]; then
    HAPUS=$(find "$TUJUAN" -maxdepth 1 -name "nms-*.sql.gz" \
            -mtime "+${SIMPAN_HARI}" -print -delete 2>/dev/null | wc -l)
    [[ "$HAPUS" -gt 0 ]] && ok "$HAPUS backup lebih dari $SIMPAN_HARI hari dihapus"
fi

JUMLAH=$(find "$TUJUAN" -maxdepth 1 -name "nms-*.sql.gz" | wc -l)
TOTAL=$(du -sh "$TUJUAN" 2>/dev/null | cut -f1)
echo "     $JUMLAH backup tersimpan, total $TOTAL"

# ---------- peringatan yang jujur ----------
case "$TUJUAN" in
    ./backups|backups|/home/*/nms-smi/backups)
        warn "Backup ada di server yang sama dengan databasenya."
        echo "        Kalau disk atau servernya mati, backup ikut hilang —"
        echo "        justru saat paling dibutuhkan. Salin keluar:"
        echo "        ./scripts/backup.sh /mnt/nas   (atau rsync ke mesin lain)"
        ;;
esac

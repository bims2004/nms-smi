#!/usr/bin/env bash
# =========================================================
# Diagnosa sistem yang SEDANG BERJALAN.
#
# Bedanya dengan preflight.sh: preflight dipakai sebelum deploy untuk
# mengecek prasyarat. Ini dipakai setelah deploy, untuk menjawab
# "kenapa status pelanggan saya masih Belum diketahui?"
#
# Pakai:  ./scripts/diagnose.sh
# =========================================================
set -uo pipefail
cd "$(dirname "$0")/.."

G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; B='\033[0;34m'; N='\033[0m'
ok()   { echo -e "  ${G}OK${N}    $*"; }
bad()  { echo -e "  ${R}MASALAH${N} $*"; }
warn() { echo -e "  ${Y}PERHATIAN${N} $*"; }
head() { echo -e "\n${B}== $* ==${N}"; }

head "Berkas & konfigurasi"
if [[ ! -f .env ]]; then
    bad ".env tidak ada"
    echo "        -> cp .env.example .env lalu isi. Ingat: POSTGRES_PASSWORD"
    echo "           harus sama dengan saat volume database pertama dibuat,"
    echo "           kalau tidak Postgres akan menolak login."
    exit 1
fi
ok ".env ada"

for v in POSTGRES_PASSWORD DJANGO_SECRET_KEY; do
    if ! grep -q "^${v}=..*" .env; then
        bad "$v kosong atau tidak diisi di .env"
    fi
done
if ! grep -q "^TELEGRAM_BOT_TOKEN=..*" .env; then
    warn "TELEGRAM_BOT_TOKEN kosong — alert tidak akan terkirim ke Telegram"
fi

head "Container"
if ! docker compose ps > /dev/null 2>&1; then
    bad "docker compose tidak bisa jalan di folder ini"
    exit 1
fi

for svc in db collector alerter web; do
    state=$(docker compose ps "$svc" --format '{{.State}}' 2>/dev/null | head -1)
    case "$state" in
        running)  ok "$svc berjalan" ;;
        restarting)
            bad "$svc restart terus — biasanya crash saat start"
            echo "        -> docker compose logs $svc --tail 30" ;;
        "")
            bad "$svc tidak ada"
            echo "        -> docker compose up -d --build" ;;
        *)
            bad "$svc dalam keadaan: $state"
            echo "        -> docker compose logs $svc --tail 30" ;;
    esac
done

# Port host untuk service web. Formatnya "${WEB_BIND:-0.0.0.0}:8000:8000",
# jadi yang diambil adalah angka sebelum ":8000" penutup.
port=$(grep -oP '[":]\K[0-9]+(?=:8000")' docker-compose.yml 2>/dev/null | tail -1)
port="${port:-8000}"
head "Akses dashboard"
if curl -sf -o /dev/null --max-time 5 "http://localhost:${port}/"; then
    ok "Dashboard menjawab di port ${port}"
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    [[ -n "$ip" ]] && echo "        Buka dari browser: http://${ip}:${port}"
else
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
           "http://localhost:${port}/" 2>/dev/null)
    if [[ "$code" == "302" || "$code" == "200" ]]; then
        ok "Dashboard menjawab (HTTP $code)"
        ip=$(hostname -I 2>/dev/null | awk '{print $1}')
        [[ -n "$ip" ]] && echo "        Buka dari browser: http://${ip}:${port}"
    else
        bad "Dashboard tidak menjawab di port ${port}"
        echo "        -> Ingat URL-nya HARUS pakai port: http://<ip>:${port}"
        echo "        -> docker compose logs web --tail 30"
    fi
fi

if command -v ufw > /dev/null 2>&1 && ufw status 2>/dev/null | grep -q "^Status: active"; then
    if ! ufw status | grep -q "${port}"; then
        warn "ufw aktif tapi port ${port} belum diizinkan"
        echo "        -> sudo ufw allow ${port}/tcp"
    fi
fi

head "Diagnosa mendalam"
if [[ "$(docker compose ps collector --format '{{.State}}' 2>/dev/null | head -1)" != "running" ]]; then
    bad "Container collector tidak berjalan — diagnosa mendalam dilewati"
    exit 1
fi
echo "  (dijalankan dari dalam container collector, memakai jalur jaringan"
echo "   yang sama persis dengan proses polling)"
echo ""
docker compose exec -T collector python -m nms.diagnose
exit $?

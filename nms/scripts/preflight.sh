#!/usr/bin/env bash
# =========================================================
# Preflight check sebelum menjalankan NMS.
# Memverifikasi: Docker, file .env, dan reachability device.
#
# Pakai:
#   ./scripts/preflight.sh                      # cek Docker + .env saja
#   ./scripts/preflight.sh snmp 10.10.10.1 publicRO
#   ./scripts/preflight.sh api  10.10.10.2
# =========================================================
set -uo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[ OK ]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }

FAILED=0

check_docker() {
    echo "--- Docker ---"
    if ! command -v docker >/dev/null 2>&1; then
        fail "docker tidak ditemukan. Install Docker Engine dulu."
        FAILED=1; return
    fi
    ok "docker: $(docker --version)"

    if docker compose version >/dev/null 2>&1; then
        ok "compose: $(docker compose version --short 2>/dev/null || echo v2)"
    else
        fail "'docker compose' (v2) tidak ada. Jangan pakai docker-compose v1 —"
        fail "  compose file ini butuh Compose v2. Install docker-compose-plugin."
        FAILED=1
    fi

    if ! docker info >/dev/null 2>&1; then
        fail "Tidak bisa akses Docker daemon. Jalankan: sudo usermod -aG docker \$USER && newgrp docker"
        FAILED=1
    else
        ok "Docker daemon dapat diakses tanpa sudo"
    fi
}

check_env() {
    echo "--- File .env ---"
    if [[ ! -f .env ]]; then
        fail ".env tidak ada. Jalankan: cp .env.example .env && nano .env"
        FAILED=1; return
    fi
    ok ".env ditemukan"

    # shellcheck disable=SC1091
    set -a; source .env 2>/dev/null; set +a

    if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || "${TELEGRAM_BOT_TOKEN}" == *"isi-token"* ]]; then
        warn "TELEGRAM_BOT_TOKEN belum diisi — alert tidak akan terkirim (hanya masuk log)"
    else
        ok "TELEGRAM_BOT_TOKEN terisi"
    fi

    if [[ -z "${TELEGRAM_CHAT_ID:-}" || "${TELEGRAM_CHAT_ID}" == "-1001234567890" ]]; then
        warn "TELEGRAM_CHAT_ID masih placeholder"
    else
        ok "TELEGRAM_CHAT_ID terisi"
    fi

    if [[ "${POSTGRES_PASSWORD:-}" == "nmspass" ]]; then
        warn "POSTGRES_PASSWORD masih default 'nmspass' — ganti untuk produksi"
    else
        ok "POSTGRES_PASSWORD sudah diganti"
    fi
}

test_telegram() {
    echo "--- Test kirim Telegram ---"
    # shellcheck disable=SC1091
    set -a; source .env 2>/dev/null; set +a
    if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
        fail "Token/chat ID belum diisi di .env"
        return 1
    fi
    resp=$(curl -s -X POST \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        -d "text=✅ Test dari NMS preflight — bot & chat ID sudah benar.")
    if echo "$resp" | grep -q '"ok":true'; then
        ok "Pesan test terkirim. Cek grup NOC."
    else
        fail "Telegram menolak: $resp"
        return 1
    fi
}

test_snmp() {
    local ip="$1" community="$2"
    echo "--- Test SNMP ke $ip ---"
    if ! command -v snmpwalk >/dev/null 2>&1; then
        fail "snmpwalk tidak ada. Jalankan: sudo apt install -y snmp"
        return 1
    fi
    # sysName
    if out=$(snmpget -v2c -c "$community" -t 3 -r 1 "$ip" 1.3.6.1.2.1.1.5.0 2>&1); then
        ok "SNMP jawab: $out"
    else
        fail "SNMP tidak jawab. Cek: community string, ACL SNMP di device, firewall UDP/161"
        return 1
    fi
    # Cek dukungan counter 64-bit (wajib untuk link gigabit)
    if snmpwalk -v2c -c "$community" -t 3 -r 1 "$ip" 1.3.6.1.2.1.31.1.1.1.6 2>/dev/null | head -1 | grep -q Counter64; then
        ok "ifHCInOctets (counter 64-bit) tersedia"
    else
        warn "ifHCInOctets tidak terbaca — device mungkin tidak support 64-bit counter"
    fi
    echo ""
    echo "Daftar ifIndex di $ip (untuk isi kolom if_index):"
    snmpwalk -v2c -c "$community" -t 3 -r 1 "$ip" 1.3.6.1.2.1.31.1.1.1.1 2>/dev/null | head -40
}

test_api() {
    local ip="$1" port="${2:-8728}"
    echo "--- Test Mikrotik API $ip:$port ---"
    if command -v nc >/dev/null 2>&1; then
        if nc -z -w3 "$ip" "$port" 2>/dev/null; then
            ok "Port $port terbuka"
        else
            fail "Port $port tertutup. Di Mikrotik cek:"
            echo "      /ip service print          -> pastikan 'api' enabled"
            echo "      /ip service set api address=<ip-server-nms>/32"
            echo "      /ip firewall filter        -> pastikan tidak diblok"
            return 1
        fi
    else
        warn "nc tidak ada, skip. Install: sudo apt install -y netcat-openbsd"
    fi
}

# ---- main ----
case "${1:-all}" in
    snmp)     test_snmp "${2:?ip wajib}" "${3:-public}" ;;
    api)      test_api  "${2:?ip wajib}" "${3:-8728}" ;;
    telegram) test_telegram ;;
    all|*)
        check_docker
        echo ""
        check_env
        echo ""
        echo "========================================"
        if [[ $FAILED -eq 0 ]]; then
            ok "Preflight dasar lolos. Lanjut test device:"
            echo "     ./scripts/preflight.sh snmp <ip-switch> <community>"
            echo "     ./scripts/preflight.sh api  <ip-mikrotik>"
            echo "     ./scripts/preflight.sh telegram"
        else
            fail "Ada yang perlu diperbaiki dulu (lihat di atas)."
            exit 1
        fi
        ;;
esac

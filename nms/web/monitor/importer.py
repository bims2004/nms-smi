"""Impor pelanggan dari CSV.

Mendaftarkan ratusan pelanggan satu per satu lewat form adalah siksaan.
Tapi impor massal punya bahaya sendiri: satu file yang salah bisa mengotori
inventaris dalam sekejap, dan membersihkannya jauh lebih susah daripada
memasukkannya.

Karena itu:
- Semua baris divalidasi DULU. Kalau ada satu saja yang gagal, TIDAK ADA yang
  masuk. Impor setengah jalan meninggalkan keadaan yang sulit dirapikan —
  orang tidak tahu mana yang sudah masuk dan mana yang belum.
- Ada mode uji-coba yang menunjukkan apa yang akan terjadi tanpa menyentuh
  database.
- Validasinya sama persis dengan form biasa (full_clean), jadi tidak ada
  jalan pintas yang melewati aturan.
"""
import csv
import io

from django.core.exceptions import ValidationError

from .models import Customer, Device

KOLOM = ["nama", "id_layanan", "perangkat", "tipe", "titik", "ambang_bps",
         "arah", "deteksi_degradasi"]

CONTOH = """nama,id_layanan,perangkat,tipe,titik,ambang_bps,arah,deteksi_degradasi
PT Maju Jaya,CUST-0001,SW CAKRA,snmp_if,6,100000,ke_pelanggan,ya
Budi Santoso,CUST-0102,RB5009-BRAS,pppoe,budi@home,50000,,tidak
"""


def _bersih(v):
    return (v or "").strip()


def parse_csv(teks):
    """Kembalikan (daftar_objek_siap, daftar_error).

    Objeknya belum disimpan. Pemanggil yang memutuskan kapan menyimpan.
    """
    objek, errors = [], []
    try:
        reader = csv.DictReader(io.StringIO(teks))
    except csv.Error as e:
        return [], [f"File tidak bisa dibaca: {e}"]

    if not reader.fieldnames:
        return [], ["File kosong atau tidak punya baris judul."]

    kurang = [k for k in ("nama", "perangkat", "tipe", "titik")
              if k not in reader.fieldnames]
    if kurang:
        return [], [f"Kolom wajib tidak ada: {', '.join(kurang)}. "
                    f"Judul yang diperlukan: {', '.join(KOLOM)}"]

    # Cache perangkat: kalau tidak, 500 baris = 500 query
    devices = {d.name.strip().lower(): d for d in Device.objects.all()}
    terdaftar = set(
        Customer.objects.exclude(service_id__isnull=True)
        .exclude(service_id="")
        .values_list("service_id", flat=True)
    )
    dalam_file = set()

    for i, row in enumerate(reader, start=2):   # baris 1 = judul
        nama = _bersih(row.get("nama"))
        if not nama:
            errors.append(f"Baris {i}: kolom nama kosong")
            continue

        dev_nama = _bersih(row.get("perangkat"))
        dev = devices.get(dev_nama.lower())
        if dev is None:
            errors.append(
                f"Baris {i} ({nama}): perangkat '{dev_nama}' tidak terdaftar. "
                f"Yang ada: {', '.join(sorted(d.name for d in devices.values())) or '(belum ada)'}"
            )
            continue

        sid = _bersih(row.get("id_layanan")) or None
        if sid:
            if sid in terdaftar:
                errors.append(f"Baris {i} ({nama}): id_layanan '{sid}' "
                              f"sudah dipakai pelanggan lain")
                continue
            if sid in dalam_file:
                errors.append(f"Baris {i} ({nama}): id_layanan '{sid}' "
                              f"muncul dua kali di file ini")
                continue
            dalam_file.add(sid)

        tipe = _bersih(row.get("tipe")).lower()
        titik = _bersih(row.get("titik"))
        c = Customer(name=nama, service_id=sid, device=dev, monitor_type=tipe)

        if tipe == "snmp_if":
            try:
                c.if_index = int(titik)
            except (TypeError, ValueError):
                errors.append(f"Baris {i} ({nama}): titik untuk snmp_if harus "
                              f"berupa ifIndex angka, dapat '{titik}'")
                continue
        elif tipe == "pppoe":
            c.pppoe_username = titik
        else:
            errors.append(f"Baris {i} ({nama}): tipe harus 'snmp_if' atau "
                          f"'pppoe', dapat '{tipe}'")
            continue

        ambang = _bersih(row.get("ambang_bps"))
        if ambang:
            try:
                c.threshold_bps = int(ambang)
            except ValueError:
                errors.append(f"Baris {i} ({nama}): ambang_bps harus angka, "
                              f"dapat '{ambang}'")
                continue

        arah = _bersih(row.get("arah")).lower()
        if arah:
            if arah not in ("ke_pelanggan", "ke_upstream"):
                errors.append(f"Baris {i} ({nama}): arah harus 'ke_pelanggan' "
                              f"atau 'ke_upstream', dapat '{arah}'")
                continue
            c.if_direction = arah

        deg = _bersih(row.get("deteksi_degradasi")).lower()
        c.baseline_enabled = deg in ("ya", "yes", "true", "1", "y")

        # Validasi yang sama persis dengan form biasa — tidak ada jalan pintas
        try:
            c.full_clean(exclude=["status", "status_changed_at"])
        except ValidationError as e:
            for f, pesan in e.message_dict.items():
                errors.append(f"Baris {i} ({nama}): {f} — {' '.join(pesan)}")
            continue

        objek.append(c)

    if not objek and not errors:
        errors.append("Tidak ada baris data di file (hanya judul?).")
    return objek, errors

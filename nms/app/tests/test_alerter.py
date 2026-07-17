"""Tes alerter — jantung sistem ini.

Ditulis SEBELUM menambahkan korelasi ODP, supaya kalau refactor merusak
perilaku lama, yang memberi tahu adalah tes ini, bukan pelanggan.

Butuh Postgres yang bisa dihubungi lewat env DB_HOST/POSTGRES_*. Tes ini
membuat datanya sendiri dan membersihkannya lagi.

Jalankan:  python tests/test_alerter.py     (dari folder app/)
"""
import os
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone

APP = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

from nms import alerter, config, db  # noqa: E402


def bersihkan(conn):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM alerts")
        cur.execute("DELETE FROM traffic_samples")
        cur.execute("UPDATE customers SET odp_id = NULL")
        cur.execute("DELETE FROM odps")
        cur.execute("DELETE FROM customers")
        cur.execute("DELETE FROM maintenance_windows")
        cur.execute("DELETE FROM devices")
    conn.commit()


def buat_device(conn, nama="SW-UJI", fail_count=0, last_ok=True):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO devices (name, ip, vendor, poll_method, snmp_community,
                                    enabled, fail_count, last_ok_at)
               VALUES (%s, '10.0.0.1', 'huawei', 'snmp', 'public', TRUE, %s, %s)
               RETURNING id""",
            (nama, fail_count, datetime.now(timezone.utc) if last_ok else None),
        )
        return cur.fetchone()[0]


def buat_odp(conn, nama, device_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO odps (name, device_id) VALUES (%s, %s) RETURNING id",
            (nama, device_id),
        )
        return cur.fetchone()[0]


def buat_customer(conn, nama, device_id, if_index, odp_id=None, threshold=1000):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO customers (name, device_id, monitor_type, if_index,
                                      threshold_bps, enabled, odp_id, status)
               VALUES (%s, %s, 'snmp_if', %s, %s, TRUE, %s, 'unknown')
               RETURNING id""",
            (nama, device_id, if_index, threshold, odp_id),
        )
        return cur.fetchone()[0]


def beri_sampel(conn, customer_id, *, up, n=3):
    """Tulis n sampel terakhir. up=False berarti link mati."""
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        for i in range(n):
            cur.execute(
                """INSERT INTO traffic_samples
                   (time, customer_id, in_bps, out_bps, in_octets, out_octets, link_up)
                   VALUES (%s, %s, %s, %s, 0, 0, %s)""",
                (now - timedelta(seconds=30 * i), customer_id,
                 5_000_000 if up else 0, 5_000_000 if up else 0, up),
            )


def alert_terbuka(conn, **kw):
    kolom, nilai = list(kw.items())[0]
    with db.dict_cursor(conn) as cur:
        cur.execute(
            f"SELECT * FROM alerts WHERE {kolom} = %s AND resolved_at IS NULL",
            (nilai,),
        )
        return cur.fetchone()


class AlerterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("NMS_SECRET_KEY", "kunci-uji")
        cls.conn = db.get_conn()
        # Jangan pernah mengirim Telegram dari dalam tes
        cls.conn.autocommit = False
        alerter.notifier.send_telegram = lambda *a, **k: True

    @classmethod
    def tearDownClass(cls):
        bersihkan(cls.conn)
        cls.conn.close()

    def setUp(self):
        # Satu tes yang gagal meninggalkan transaksi rusak; tanpa rollback,
        # semua tes sesudahnya ikut merah dan pesan aslinya tertimbun.
        try:
            self.conn.rollback()
        except Exception:
            pass
        bersihkan(self.conn)
        self.dev = buat_device(self.conn)

    # ---------------- perilaku dasar ----------------

    def test_pelanggan_down_membuat_alert(self):
        c = buat_customer(self.conn, "A", self.dev, 1)
        beri_sampel(self.conn, c, up=False)
        alerter.run_check(self.conn)
        a = alert_terbuka(self.conn, customer_id=c)
        self.assertIsNotNone(a, "3 sampel down berturut-turut harus membuat alert")
        self.assertEqual(a["alert_type"], "link_down")
        self.assertEqual(a["severity"], "major")

    def test_pelanggan_up_tidak_membuat_alert(self):
        c = buat_customer(self.conn, "A", self.dev, 1)
        beri_sampel(self.conn, c, up=True)
        alerter.run_check(self.conn)
        self.assertIsNone(alert_terbuka(self.conn, customer_id=c))

    def test_sampel_kurang_tidak_menghakimi(self):
        """Sampel belum cukup bukan berarti pelanggan mati."""
        c = buat_customer(self.conn, "A", self.dev, 1)
        beri_sampel(self.conn, c, up=False, n=1)
        alerter.run_check(self.conn)
        self.assertIsNone(alert_terbuka(self.conn, customer_id=c),
                          "1 sampel tidak boleh cukup untuk menuduh mati")

    def test_pulih_menutup_alert(self):
        c = buat_customer(self.conn, "A", self.dev, 1)
        beri_sampel(self.conn, c, up=False)
        alerter.run_check(self.conn)
        self.assertIsNotNone(alert_terbuka(self.conn, customer_id=c))

        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM traffic_samples WHERE customer_id = %s", (c,))
        beri_sampel(self.conn, c, up=True)
        alerter.run_check(self.conn)
        self.assertIsNone(alert_terbuka(self.conn, customer_id=c),
                          "sampel normal harus menutup gangguan")

    def test_alert_tidak_dobel(self):
        c = buat_customer(self.conn, "A", self.dev, 1)
        beri_sampel(self.conn, c, up=False)
        alerter.run_check(self.conn)
        alerter.run_check(self.conn)
        alerter.run_check(self.conn)
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM alerts WHERE customer_id = %s "
                "AND resolved_at IS NULL", (c,))
            self.assertEqual(cur.fetchone()[0], 1,
                             "siklus berulang tidak boleh menumpuk alert")

    # ---------------- perangkat mati ----------------

    def test_perangkat_mati_satu_alert_saja(self):
        """Perangkat mati = SATU alert, bukan satu per pelanggan."""
        with self.conn.cursor() as cur:
            cur.execute("UPDATE devices SET fail_count = %s WHERE id = %s",
                        (config.DEVICE_FAIL_THRESHOLD, self.dev))
        cs = [buat_customer(self.conn, f"C{i}", self.dev, i) for i in range(1, 6)]
        for c in cs:
            beri_sampel(self.conn, c, up=False)
        alerter.run_check(self.conn)

        self.assertIsNotNone(alert_terbuka(self.conn, device_id=self.dev))
        for c in cs:
            self.assertIsNone(
                alert_terbuka(self.conn, customer_id=c),
                "pelanggan di bawah perangkat mati tidak boleh dinilai sendiri",
            )

    # ---------------- jendela pemeliharaan ----------------

    def test_pemeliharaan_alert_dicatat_tapi_ditandai(self):
        c = buat_customer(self.conn, "A", self.dev, 1)
        now = datetime.now(timezone.utc)
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO maintenance_windows
                   (name, device_id, starts_at, ends_at, note, created_at)
                   VALUES ('Uji', %s, %s, %s, 'uji', now())""",
                (self.dev, now - timedelta(hours=1), now + timedelta(hours=1)),
            )
        beri_sampel(self.conn, c, up=False)
        alerter.run_check(self.conn)
        a = alert_terbuka(self.conn, customer_id=c)
        self.assertIsNotNone(a, "pemeliharaan tetap MENCATAT gangguan")
        self.assertTrue(a["suppressed"],
                        "gangguan saat pemeliharaan ditandai suppressed")



class OdpKorelasiTest(AlerterTest):
    """ODP tidak bisa di-ping. Pelanggan di bawahnya yang jadi sensornya."""

    # ifIndex harus unik per perangkat (uq_customer_if_point), jadi tiap ODP
    # dalam satu tes memakai rentang sendiri.
    _idx = 100

    def _odp_dengan(self, n_total, n_down, nama="ODP-01"):
        odp = buat_odp(self.conn, nama, self.dev)
        cs = []
        for i in range(n_total):
            OdpKorelasiTest._idx += 1
            c = buat_customer(self.conn, f"{nama}-C{i}", self.dev,
                              OdpKorelasiTest._idx, odp_id=odp)
            beri_sampel(self.conn, c, up=(i >= n_down))
            cs.append(c)
        return odp, cs

    def test_semua_pelanggan_mati_memicu_alert_odp(self):
        odp, cs = self._odp_dengan(8, 8)
        alerter.run_check(self.conn)
        a = alert_terbuka(self.conn, odp_id=odp)
        self.assertIsNotNone(a, "8 dari 8 pelanggan mati = ODP down")
        self.assertEqual(a["alert_type"], "odp_down")

    def test_satu_pelanggan_mati_bukan_salah_odp(self):
        """Satu pelanggan mati = drop cable-nya sendiri, bukan ODP."""
        odp, cs = self._odp_dengan(8, 1)
        alerter.run_check(self.conn)
        self.assertIsNone(alert_terbuka(self.conn, odp_id=odp),
                          "1 dari 8 mati tidak boleh menuduh ODP")
        self.assertIsNotNone(alert_terbuka(self.conn, customer_id=cs[0]),
                             "pelanggannya sendiri tetap dapat alert")

    def test_di_bawah_ambang_rasio_tidak_memicu(self):
        # 2 dari 8 = 25%, jauh di bawah 75%
        odp, cs = self._odp_dengan(8, 2)
        alerter.run_check(self.conn)
        self.assertIsNone(alert_terbuka(self.conn, odp_id=odp))

    def test_tepat_di_ambang_memicu(self):
        # 6 dari 8 = 75% = tepat ambang
        odp, cs = self._odp_dengan(8, 6)
        alerter.run_check(self.conn)
        self.assertIsNotNone(alert_terbuka(self.conn, odp_id=odp),
                             "tepat di ambang harus memicu")

    def test_odp_kecil_butuh_bukti_minimal(self):
        """2 dari 2 mati bisa saja kebetulan — bukti terlalu lemah."""
        odp, cs = self._odp_dengan(2, 2, "ODP-KECIL")
        alerter.run_check(self.conn)
        self.assertIsNone(
            alert_terbuka(self.conn, odp_id=odp),
            f"ODP dengan {config.ODP_MIN_DOWN} pelanggan atau kurang "
            f"tidak boleh dituduh — buktinya lemah",
        )
        for c in cs:
            self.assertIsNotNone(alert_terbuka(self.conn, customer_id=c),
                                 "pelanggannya tetap dapat alert sendiri")

    def test_alert_pelanggan_tetap_dicatat_dan_ditautkan(self):
        """Alert pelanggan TETAP ada — itu yang dihitung SLA."""
        odp, cs = self._odp_dengan(8, 8)
        alerter.run_check(self.conn)
        induk = alert_terbuka(self.conn, odp_id=odp)
        for c in cs:
            a = alert_terbuka(self.conn, customer_id=c)
            self.assertIsNotNone(
                a, "alert pelanggan HARUS tetap dicatat — ODP putus tetap "
                   "downtime yang dihitung melawan SLA")
            self.assertEqual(a["parent_alert_id"], induk["id"],
                             "alert pelanggan menunjuk ke alert ODP")
            self.assertFalse(
                a["suppressed"],
                "JANGAN pakai suppressed — itu untuk pemeliharaan, dan "
                "laporan SLA punya tombol untuk mengecualikannya. ODP putus "
                "bukan pemeliharaan.")

    def test_notifikasi_pelanggan_ditekan_saat_odp_down(self):
        terkirim = []
        alerter.notifier.notify_down = lambda *a, **k: terkirim.append(a) or True
        alerter.notifier.send_telegram = lambda *a, **k: terkirim.append(a) or True
        try:
            odp, cs = self._odp_dengan(8, 8)
            alerter.run_check(self.conn)
            self.assertEqual(
                len(terkirim), 1,
                f"harus 1 pesan (ODP), bukan {len(terkirim)}. Delapan pesan "
                f"untuk satu feeder putus itu banjir, bukan informasi.")
        finally:
            alerter.notifier.notify_down = lambda *a, **k: True
            alerter.notifier.send_telegram = lambda *a, **k: True

    def test_odp_pulih_menutup_alert(self):
        odp, cs = self._odp_dengan(8, 8)
        alerter.run_check(self.conn)
        self.assertIsNotNone(alert_terbuka(self.conn, odp_id=odp))

        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM traffic_samples")
        for c in cs:
            beri_sampel(self.conn, c, up=True)
        alerter.run_check(self.conn)
        self.assertIsNone(alert_terbuka(self.conn, odp_id=odp),
                          "semua pelanggan pulih -> ODP pulih")

    def test_perangkat_mati_tidak_dituduhkan_ke_odp(self):
        """Switch mati bukan ODP putus. Buktinya tidak boleh dipakai."""
        odp, cs = self._odp_dengan(8, 8)
        with self.conn.cursor() as cur:
            cur.execute("UPDATE devices SET fail_count = %s WHERE id = %s",
                        (config.DEVICE_FAIL_THRESHOLD, self.dev))
        alerter.run_check(self.conn)
        self.assertIsNone(
            alert_terbuka(self.conn, odp_id=odp),
            "perangkat mati sudah punya alertnya sendiri — jangan kirim "
            "teknisi ke ODP untuk masalah di switch")
        self.assertIsNotNone(alert_terbuka(self.conn, device_id=self.dev))

    def test_odp_tanpa_pelanggan_diam(self):
        odp = buat_odp(self.conn, "ODP-KOSONG", self.dev)
        alerter.run_check(self.conn)
        self.assertIsNone(alert_terbuka(self.conn, odp_id=odp))

    def test_pelanggan_tanpa_odp_tidak_terpengaruh(self):
        c = buat_customer(self.conn, "Sendirian", self.dev, 1, odp_id=None)
        beri_sampel(self.conn, c, up=False)
        alerter.run_check(self.conn)
        a = alert_terbuka(self.conn, customer_id=c)
        self.assertIsNotNone(a)
        self.assertIsNone(a["parent_alert_id"])

    def test_alert_odp_tidak_dobel(self):
        odp, cs = self._odp_dengan(8, 8)
        for _ in range(3):
            alerter.run_check(self.conn)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM alerts WHERE odp_id = %s "
                        "AND resolved_at IS NULL", (odp,))
            self.assertEqual(cur.fetchone()[0], 1)

    def test_dua_odp_dinilai_terpisah(self):
        odp1, cs1 = self._odp_dengan(8, 8, "ODP-A")
        odp2, cs2 = self._odp_dengan(8, 0, "ODP-B")
        alerter.run_check(self.conn)
        self.assertIsNotNone(alert_terbuka(self.conn, odp_id=odp1))
        self.assertIsNone(alert_terbuka(self.conn, odp_id=odp2),
                          "ODP lain yang sehat tidak boleh ikut dituduh")

if __name__ == "__main__":
    unittest.main(verbosity=2)

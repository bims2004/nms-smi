"""Uji untuk kelas bug yang lolos dari py_compile.

Latar belakang: `python -m py_compile` hanya memeriksa sintaks. Modul yang
memanggil `crypto.dekripsi()` tanpa pernah meng-import `crypto` tetap lolos —
dan baru meledak saat fungsinya benar-benar dijalankan. Di collector, ledakan
itu tertangkap `except Exception` dan tercatat sebagai "Mikrotik API gagal",
yang terbaca seperti masalah jaringan. Butuh berjam-jam untuk sadar.

Jalankan:  python -m pytest tests/ -q     (dari folder app/)
        atau: python tests/test_pollers.py
"""
import ast
import pathlib
import sys
import unittest

APP = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))


class ImportLengkapTest(unittest.TestCase):
    """Setiap modul harus bisa di-import sungguhan, bukan cuma di-parse."""

    def test_semua_modul_bisa_diimport(self):
        import importlib

        gagal = []
        dilewati = []
        for f in sorted((APP / "nms").rglob("*.py")):
            if "__pycache__" in str(f):
                continue
            rel = f.relative_to(APP).with_suffix("")
            mod = ".".join(rel.parts)
            try:
                importlib.import_module(mod)
            except ModuleNotFoundError as e:
                # Dependensi pihak ketiga yang tidak ada di lingkungan ini
                # (mis. pysnmp butuh Python 3.11; sandbox pengembang mungkin
                # 3.12). Itu soal lingkungan, bukan bug kode — container
                # produksi memakai versi yang dipin di requirements.txt.
                if e.name and not e.name.startswith("nms"):
                    dilewati.append(f"{mod} (butuh '{e.name}')")
                    continue
                gagal.append(f"{mod}: {type(e).__name__}: {e}")
            except Exception as e:
                gagal.append(f"{mod}: {type(e).__name__}: {e}")

        if dilewati:
            print("\n  (dilewati, dependensi tak ada di sini: "
                  + ", ".join(dilewati) + ")")
        self.assertEqual(gagal, [], "Modul gagal di-import:\n" + "\n".join(gagal))

    def test_tidak_ada_nama_tak_terdefinisi(self):
        """Tangkap `crypto.x()` tanpa `import crypto` — py_compile melewatkannya."""
        masalah = []
        for f in sorted((APP / "nms").rglob("*.py")):
            if "__pycache__" in str(f):
                continue
            tree = ast.parse(f.read_text())

            tersedia = set(dir(__builtins__)) | {"__name__", "__file__", "self"}
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    tersedia |= {a.asname or a.name.split(".")[0] for a in n.names}
                elif isinstance(n, ast.ImportFrom):
                    tersedia |= {a.asname or a.name for a in n.names}
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.ClassDef)):
                    tersedia.add(n.name)
                elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    tersedia.add(n.id)
                elif isinstance(n, ast.arg):
                    tersedia.add(n.arg)
                elif isinstance(n, ast.alias):
                    tersedia.add(n.asname or n.name.split(".")[0])

            # Modul yang dipakai lewat atribut: crypto.x, config.Y, db.z
            for n in ast.walk(tree):
                if (isinstance(n, ast.Attribute)
                        and isinstance(n.value, ast.Name)
                        and n.value.id not in tersedia):
                    masalah.append(
                        f"{f.relative_to(APP)}:{n.lineno}: "
                        f"'{n.value.id}.{n.attr}' — '{n.value.id}' tidak di-import"
                    )
        self.assertEqual(masalah, [],
                         "Nama dipakai tanpa di-import:\n" + "\n".join(masalah))


class NormUserTest(unittest.TestCase):
    """librouteros mengubah nilai mirip angka jadi int. Ini konsekuensinya."""

    def test_username_angka_tetap_cocok(self):
        from nms.pollers.mikrotik import norm_user

        # (nilai dari API librouteros, nilai di database)
        cocok = [
            (12345, "12345"),                    # username angka murni
            (81234567890, "081234567890"),       # nol di depan hilang di API
            ("budi", "budi"),
            ("budi@smi.net.id", "budi@smi.net.id"),
            (" 12345 ", "12345"),                # spasi liar
        ]
        for api_val, db_val in cocok:
            self.assertEqual(
                norm_user(api_val), norm_user(db_val),
                f"API {api_val!r} seharusnya cocok dengan database {db_val!r}",
            )

        beda = [(12345, "12346"), ("budi", "andi"), ("budi", "budi2")]
        for a, b in beda:
            self.assertNotEqual(norm_user(a), norm_user(b),
                                f"{a!r} tidak boleh cocok dengan {b!r}")

    def test_bug_lama_memang_gagal(self):
        """Bukti bahwa masalahnya nyata, bukan kehati-hatian berlebihan."""
        self.assertNotIn("12345", {12345})
        self.assertNotIn("081234567890", {81234567890})

    def test_librouteros_memang_begitu(self):
        """Kalau librouteros berubah perilaku, penjaga ini boleh dilonggarkan."""
        from librouteros.protocol import parse_word

        self.assertEqual(parse_word("=name=12345"), ("name", 12345))
        self.assertEqual(parse_word("=name=081234567890"),
                         ("name", 81234567890))
        self.assertEqual(parse_word("=name=budi"), ("name", "budi"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

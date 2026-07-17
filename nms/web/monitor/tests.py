"""Penjaga terhadap kesalahan template yang lolos dari mata.

Latar belakang: sintaks komentar Django `{# ... #}` HANYA berlaku untuk satu
baris. Komentar yang membentang dua baris tidak dianggap komentar sama sekali
dan tercetak apa adanya di halaman. Ini pernah lolos ke produksi karena
pengujian saat itu hanya memeriksa isi JSON dan kesamaan SVG — tidak ada yang
melihat halamannya.

Jalankan: python manage.py test monitor
"""
import pathlib
import re

from django.template import Context, Template
from django.test import TestCase

TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent / "templates"


class KomentarTemplateTest(TestCase):
    def test_tidak_ada_komentar_multibaris(self):
        """{# #} multi-baris akan tercetak ke halaman, bukan disembunyikan."""
        pola = re.compile(r"\{#(.*?)#\}", re.S)
        bocor = []
        for f in TEMPLATE_DIR.rglob("*.html"):
            for m in pola.finditer(f.read_text()):
                if "\n" in m.group(1):
                    bocor.append(
                        f"{f.relative_to(TEMPLATE_DIR)}: "
                        f"{m.group(0)[:50].replace(chr(10), ' / ')}..."
                    )
        self.assertEqual(
            bocor, [],
            "Komentar {# #} multi-baris akan tercetak ke halaman. "
            "Pakai {% comment %}...{% endcomment %}. Yang bermasalah:\n"
            + "\n".join(bocor),
        )

    def test_perilaku_django_memang_begitu(self):
        """Bukti bahwa aturan di atas nyata, bukan takhayul."""
        self.assertEqual(Template("{# x #}A").render(Context({})), "A")
        hasil = Template("{# x\ny #}A").render(Context({}))
        self.assertIn("{#", hasil,
                      "Django ternyata sudah mendukung komentar multi-baris; "
                      "penjaga ini boleh dilonggarkan.")


class TemplateBisaDirenderTest(TestCase):
    def test_semua_template_bisa_di_parse(self):
        """Tangkap sintaks rusak tanpa perlu membuka tiap halaman."""
        from django.template.loader import get_template
        from django.template import TemplateSyntaxError

        rusak = []
        for f in TEMPLATE_DIR.rglob("*.html"):
            nama = str(f.relative_to(TEMPLATE_DIR))
            try:
                get_template(nama)
            except TemplateSyntaxError as e:
                rusak.append(f"{nama}: {e}")
        self.assertEqual(rusak, [], "Template dengan sintaks rusak:\n"
                                    + "\n".join(rusak))

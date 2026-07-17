"""Model Django yang di-map ke tabel yang sudah dibuat oleh db/init/01_schema.sql.

Semua model pakai managed = False: schema tetap dimiliki oleh SQL,
Django tidak akan membuat/mengubah tabelnya. Ini menjaga collector dan
alerter tetap jalan tanpa perubahan.
"""
from django.core.exceptions import ValidationError

from . import crypto
from django.db import models

VENDOR_CHOICES = [
    ("mikrotik", "Mikrotik"),
    ("huawei", "Huawei"),
    ("zte", "ZTE"),
    ("generic", "Lainnya"),
]

POLL_METHOD_CHOICES = [
    ("snmp", "SNMP (interface fisik)"),
    ("mikrotik_api", "Mikrotik API (PPPoE)"),
]

MONITOR_TYPE_CHOICES = [
    ("snmp_if", "Interface fisik (dedicated)"),
    ("pppoe", "Sesi PPPoE"),
]

STATUS_CHOICES = [
    ("up", "Up"),
    ("down", "Down"),
    ("unknown", "Belum diketahui"),
]

DIRECTION_CHOICES = [
    ("ke_pelanggan", "Port menghadap pelanggan (umum)"),
    ("ke_upstream", "Port menghadap upstream/uplink"),
]

SEVERITY_CHOICES = [
    ("major", "Major — layanan mati"),
    ("minor", "Minor — layanan menurun"),
]

ALERT_TYPE_LABEL = {
    "odp_down": "ODP down",
    "link_down": "Link down",
    "session_down": "Sesi PPPoE putus",
    "traffic_zero": "Traffic nol",
    "traffic_degraded": "Traffic turun drastis",
    "device_down": "Perangkat tidak merespon",
}


class Device(models.Model):
    name = models.CharField("Nama perangkat", max_length=200)
    ip = models.GenericIPAddressField("IP management")
    vendor = models.CharField(
        "Vendor", max_length=50, choices=VENDOR_CHOICES, default="generic"
    )
    poll_method = models.CharField(
        "Metode polling", max_length=30,
        choices=POLL_METHOD_CHOICES, default="snmp",
    )
    snmp_community = models.CharField(
        "SNMP community", max_length=200, blank=True, null=True, default="public",
        help_text="Diisi kalau metode polling SNMP.",
    )
    snmp_port = models.IntegerField("Port SNMP", blank=True, null=True, default=161)
    api_username = models.CharField(
        "User API", max_length=200, blank=True, null=True,
        help_text="User read-only Mikrotik. Jangan pakai admin.",
    )
    api_password = models.CharField(
        "Password API", max_length=200, blank=True, null=True
    )
    api_port = models.IntegerField("Port API", blank=True, null=True, default=8728)
    enabled = models.BooleanField("Aktif", default=True)
    status = models.CharField("Status", max_length=20, default="unknown",
                              editable=False)
    status_changed_at = models.DateTimeField("Status berubah", blank=True,
                                             null=True, editable=False)
    last_ok_at = models.DateTimeField("Terakhir merespon", blank=True,
                                      null=True, editable=False)
    fail_count = models.IntegerField("Gagal berturut-turut", default=0,
                                     editable=False)

    def save(self, *args, **kwargs):
        # Dienkripsi saat menyimpan, bukan saat menampilkan. Kalau enkripsinya
        # di lapisan tampilan, nilai yang masuk lewat jalur lain (impor,
        # shell, skrip) akan lolos tanpa terenkripsi.
        if self.api_password:
            self.api_password = crypto.enkripsi(self.api_password)
        super().save(*args, **kwargs)

    class Meta:
        managed = False
        db_table = "devices"
        verbose_name = "Perangkat"
        verbose_name_plural = "Perangkat"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.ip})"

    def clean(self):
        errors = {}
        if self.poll_method == "snmp" and not self.snmp_community:
            errors["snmp_community"] = "Wajib diisi untuk polling SNMP."
        if self.poll_method == "mikrotik_api":
            if not self.api_username:
                errors["api_username"] = "Wajib diisi untuk polling Mikrotik API."
            if not self.api_password:
                errors["api_password"] = "Wajib diisi untuk polling Mikrotik API."
        if errors:
            raise ValidationError(errors)


class Odp(models.Model):
    """Box ODP — titik distribusi optik di lapangan.

    Tidak ada perangkat aktif di dalamnya, cuma splitter pasif. Tidak bisa
    di-ping, di-SNMP, atau ditanyai apa pun. Pelanggan di bawahnya yang jadi
    sensornya: kalau mereka mati bersamaan, ODP-nya yang bermasalah.
    """
    name = models.CharField("Nama ODP", max_length=100, unique=True,
                            help_text="Contoh: ODP-CAKRA-03")
    device = models.ForeignKey(
        "Device", on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name="Perangkat induk",
        help_text="OLT / switch yang menyuplai ODP ini.")
    lokasi = models.CharField(
        "Lokasi", max_length=255, blank=True, null=True,
        help_text="Alamat atau patokan. Ini yang dibaca teknisi saat "
                  "berangkat — tulis yang benar-benar menolong di lapangan.")
    latitude = models.DecimalField("Latitude", max_digits=10, decimal_places=7,
                                   null=True, blank=True)
    longitude = models.DecimalField("Longitude", max_digits=10, decimal_places=7,
                                    null=True, blank=True)
    kapasitas = models.IntegerField("Kapasitas port", null=True, blank=True,
                                    help_text="Jumlah port splitter, mis. 8 atau 16.")
    catatan = models.TextField("Catatan", blank=True, null=True)
    enabled = models.BooleanField("Aktif", default=True)
    status = models.CharField(max_length=20, default="unknown", editable=False)
    status_changed_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    class Meta:
        managed = False
        db_table = "odps"
        verbose_name = "ODP"
        verbose_name_plural = "ODP"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def maps_url(self):
        if self.latitude is None or self.longitude is None:
            return None
        return f"https://www.google.com/maps?q={self.latitude},{self.longitude}"


class Customer(models.Model):
    name = models.CharField("Nama pelanggan", max_length=200)
    service_id = models.CharField(
        "ID layanan", max_length=100, blank=True, null=True, unique=True
    )
    device = models.ForeignKey(
        Device, on_delete=models.PROTECT, db_column="device_id",
        verbose_name="Perangkat",
    )
    monitor_type = models.CharField(
        "Tipe monitoring", max_length=30, choices=MONITOR_TYPE_CHOICES
    )
    if_index = models.IntegerField(
        "ifIndex", blank=True, null=True,
        help_text="Untuk interface fisik. Pakai tombol 'Cari interface' "
                  "di halaman perangkat kalau belum tahu.",
    )
    if_name = models.CharField(
        "Nama interface", max_length=200, blank=True, null=True
    )
    pppoe_username = models.CharField(
        "Username PPPoE", max_length=200, blank=True, null=True
    )
    threshold_bps = models.BigIntegerField(
        "Ambang traffic (bps)", default=1000,
        help_text="Traffic in+out di bawah nilai ini dihitung sebagai down.",
    )
    odp = models.ForeignKey(
        Odp, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name="ODP",
        help_text="Diisi supaya NMS bisa mengenali gangguan ODP: kalau "
                  "banyak pelanggan di ODP yang sama mati bersamaan, yang "
                  "dilaporkan satu gangguan ODP, bukan sekian gangguan "
                  "terpisah.")
    if_direction = models.CharField(
        "Arah port", max_length=20, choices=DIRECTION_CHOICES,
        default="ke_pelanggan",
        help_text="Menentukan mana yang disebut download dan mana upload. "
                  "SNMP mencatat dari sudut pandang PERANGKAT: di port yang "
                  "menghadap pelanggan, trafik masuk ke port justru upload "
                  "pelanggan. Tidak berlaku untuk PPPoE.",
    )
    baseline_enabled = models.BooleanField(
        "Deteksi degradasi", default=False,
        help_text="Bandingkan traffic dengan kebiasaan pelanggan ini pada "
                  "hari & jam yang sama. Cocok untuk dedicated; "
                  "kurang cocok untuk pelanggan rumahan yang polanya acak.",
    )
    baseline_drop_pct = models.IntegerField(
        "Ambang penurunan (%)", default=80,
        help_text="Alert kalau traffic turun lebih dari sekian persen "
                  "dibanding kebiasaannya.",
    )
    enabled = models.BooleanField("Aktif", default=True)
    status = models.CharField(
        "Status", max_length=20, choices=STATUS_CHOICES,
        default="unknown", editable=False,
    )
    status_changed_at = models.DateTimeField(
        "Status berubah", blank=True, null=True, editable=False
    )

    class Meta:
        managed = False
        db_table = "customers"
        verbose_name = "Pelanggan"
        verbose_name_plural = "Pelanggan"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def flip_arah(self) -> bool:
        """True kalau in/out mentah perlu ditukar untuk sudut pandang pelanggan.

        Counter SNMP selalu disimpan apa adanya: in_bps = ifInOctets, yaitu
        trafik MASUK KE PORT. Di port yang menghadap pelanggan, itu berarti
        trafik yang datang dari pelanggan — upload-nya, bukan download.

        Database menyimpan counter mentah; penerjemahan hanya terjadi saat
        menampilkan. Kalau in/out ditukar saat menyimpan, data lama tidak bisa
        ditafsirkan lagi begitu setelan ini berubah.

        PPPoE tidak pernah perlu ditukar: poller Mikrotik sudah memetakan
        tx/rx ke sudut pandang pelanggan sejak awal.
        """
        return (self.monitor_type == "snmp_if"
                and self.if_direction == "ke_pelanggan")

    def clean(self):
        """Cocokkan dengan CHECK constraint chk_monitor di database."""
        errors = {}

        # Dua pelanggan di titik monitor yang sama berarti data yang sama
        # dihitung dua kali: alert kembar, dan SLA yang menghitung satu
        # gangguan sebagai dua. Hampir selalu salah ketik, bukan kesengajaan.
        if self.device_id:
            kembar = Customer.objects.filter(device_id=self.device_id)
            if self.pk:
                kembar = kembar.exclude(pk=self.pk)
            if self.monitor_type == "snmp_if" and self.if_index is not None:
                lain = kembar.filter(if_index=self.if_index).first()
                if lain:
                    errors["if_index"] = (
                        f"ifIndex {self.if_index} di perangkat ini sudah "
                        f"dipantau oleh '{lain.name}'. Dua pelanggan di port "
                        f"yang sama akan menghasilkan alert kembar dan angka "
                        f"SLA yang dihitung dua kali."
                    )
            elif self.monitor_type == "pppoe" and self.pppoe_username:
                lain = kembar.filter(pppoe_username=self.pppoe_username).first()
                if lain:
                    errors["pppoe_username"] = (
                        f"Username '{self.pppoe_username}' di perangkat ini "
                        f"sudah dipantau oleh '{lain.name}'."
                    )
        if self.monitor_type == "snmp_if":
            if self.if_index is None:
                errors["if_index"] = "Wajib diisi untuk monitoring interface fisik."
            if self.device_id and self.device.poll_method != "snmp":
                errors["device"] = (
                    "Perangkat ini polling-nya lewat Mikrotik API, "
                    "tidak bisa dipakai untuk interface fisik."
                )
        elif self.monitor_type == "pppoe":
            if not self.pppoe_username:
                errors["pppoe_username"] = "Wajib diisi untuk monitoring PPPoE."
            if self.device_id and self.device.poll_method != "mikrotik_api":
                errors["device"] = (
                    "Monitoring PPPoE butuh perangkat dengan metode Mikrotik API."
                )
        if errors:
            raise ValidationError(errors)


class Alert(models.Model):
    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, db_column="customer_id",
        verbose_name="Pelanggan", blank=True, null=True,
    )
    device = models.ForeignKey(
        Device, on_delete=models.CASCADE, db_column="device_id",
        verbose_name="Perangkat", blank=True, null=True,
    )
    odp = models.ForeignKey(
        Odp, on_delete=models.CASCADE, db_column="odp_id",
        verbose_name="ODP", blank=True, null=True,
    )
    parent_alert = models.ForeignKey(
        "self", on_delete=models.SET_NULL, db_column="parent_alert_id",
        related_name="children", blank=True, null=True,
        verbose_name="Imbas dari",
        help_text="Kalau terisi, gangguan ini akibat gangguan induknya "
                  "(mis. ODP putus). Notifikasinya ditekan supaya Telegram "
                  "tidak dibanjiri, tapi tetap dihitung melawan SLA.",
    )
    alert_type = models.CharField("Jenis", max_length=50)
    severity = models.CharField("Tingkat", max_length=20,
                                choices=SEVERITY_CHOICES, default="major")
    started_at = models.DateTimeField("Mulai")
    resolved_at = models.DateTimeField("Pulih", blank=True, null=True)
    notified = models.BooleanField("Terkirim ke Telegram", default=False)
    suppressed = models.BooleanField("Ditahan (pemeliharaan)", default=False)
    escalated_at = models.DateTimeField("Dieskalasi", blank=True, null=True)
    ack_by = models.CharField("Ditangani oleh", max_length=150, blank=True,
                              null=True)
    ack_at = models.DateTimeField("Ditangani pada", blank=True, null=True)

    class Meta:
        managed = False
        db_table = "alerts"
        verbose_name = "Gangguan"
        verbose_name_plural = "Gangguan"
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.subject} — {self.alert_type}"

    @property
    def subject(self):
        """Nama yang terkena gangguan: pelanggan, ODP, atau perangkat."""
        if self.customer_id:
            return self.customer.name
        if self.odp_id:
            return self.odp.name
        if self.device_id:
            return self.device.name
        return "—"

    @property
    def duration(self):
        if self.resolved_at:
            return self.resolved_at - self.started_at
        return None


class MaintenanceWindow(models.Model):
    """Jadwal pemeliharaan — gangguan tetap dicatat tapi tidak dikirim ke
    Telegram, dan bisa dikecualikan dari laporan SLA."""
    name = models.CharField("Nama pekerjaan", max_length=200)
    starts_at = models.DateTimeField("Mulai")
    ends_at = models.DateTimeField("Selesai")
    device = models.ForeignKey(
        Device, on_delete=models.CASCADE, db_column="device_id",
        verbose_name="Perangkat", blank=True, null=True,
        help_text="Kosongkan kalau tidak dibatasi ke satu perangkat.",
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, db_column="customer_id",
        verbose_name="Pelanggan", blank=True, null=True,
        help_text="Kosongkan kalau tidak dibatasi ke satu pelanggan.",
    )
    note = models.TextField("Catatan", blank=True, null=True)
    created_by = models.CharField("Dibuat oleh", max_length=150, blank=True,
                                  null=True, editable=False)
    # Kolomnya NOT NULL DEFAULT now() di SQL; auto_now_add membuat Django
    # ikut mengisinya, bukan mengirim NULL.
    created_at = models.DateTimeField("Dibuat", auto_now_add=True)

    class Meta:
        managed = False
        db_table = "maintenance_windows"
        verbose_name = "Jadwal pemeliharaan"
        verbose_name_plural = "Jadwal pemeliharaan"
        ordering = ["-starts_at"]

    def __str__(self):
        return self.name

    def clean(self):
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError(
                {"ends_at": "Waktu selesai harus setelah waktu mulai."}
            )

    @property
    def scope(self):
        if self.customer_id:
            return f"Pelanggan: {self.customer.name}"
        if self.device_id:
            return f"Perangkat: {self.device.name}"
        return "Semua pelanggan"

    @property
    def is_active(self):
        from django.utils import timezone as tz
        return self.starts_at <= tz.now() <= self.ends_at

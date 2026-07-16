"""Model Django yang di-map ke tabel yang sudah dibuat oleh db/init/01_schema.sql.

Semua model pakai managed = False: schema tetap dimiliki oleh SQL,
Django tidak akan membuat/mengubah tabelnya. Ini menjaga collector dan
alerter tetap jalan tanpa perubahan.
"""
from django.core.exceptions import ValidationError
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

    def clean(self):
        """Cocokkan dengan CHECK constraint chk_monitor di database."""
        errors = {}
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
        verbose_name="Pelanggan",
    )
    alert_type = models.CharField("Jenis", max_length=50)
    started_at = models.DateTimeField("Mulai")
    resolved_at = models.DateTimeField("Pulih", blank=True, null=True)
    notified = models.BooleanField("Terkirim ke Telegram", default=False)

    class Meta:
        managed = False
        db_table = "alerts"
        verbose_name = "Gangguan"
        verbose_name_plural = "Gangguan"
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.customer_id} — {self.alert_type}"

    @property
    def duration(self):
        if self.resolved_at:
            return self.resolved_at - self.started_at
        return None

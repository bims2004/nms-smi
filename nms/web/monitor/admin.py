"""Admin sebagai form CRUD inventory — inilah pengganti seed SQL manual."""
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import Alert, Customer, Device, MaintenanceWindow

admin.site.site_header = "NMS — Kelola inventory"
admin.site.site_title = "NMS"
admin.site.index_title = "Perangkat & pelanggan"


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("name", "ip", "vendor", "poll_method", "enabled",
                    "status", "jumlah_pelanggan", "aksi")
    list_filter = ("vendor", "poll_method", "enabled", "status")
    readonly_fields = ("status", "status_changed_at", "last_ok_at", "fail_count")
    search_fields = ("name", "ip")
    fieldsets = (
        (None, {"fields": ("name", "ip", "vendor", "poll_method", "enabled")}),
        ("SNMP", {
            "fields": ("snmp_community", "snmp_port"),
            "description": "Diisi kalau metode polling SNMP.",
        }),
        ("Mikrotik API", {
            "fields": ("api_username", "api_password", "api_port"),
            "description": "Diisi kalau metode polling Mikrotik API. "
                           "Buat user read-only dulu: "
                           "<code>/user group add name=nms-ro policy=read,api</code>",
        }),
        ("Kesehatan perangkat", {
            "fields": ("status", "status_changed_at", "last_ok_at", "fail_count"),
            "description": "Diisi otomatis oleh collector.",
        }),
    )

    @admin.display(description="Pelanggan")
    def jumlah_pelanggan(self, obj):
        return obj.customer_set.count()

    @admin.display(description="")
    def aksi(self, obj):
        url = reverse("device_interfaces", args=[obj.pk])
        return format_html('<a class="button" href="{}">Lihat interface</a>', url)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "service_id", "device", "monitor_type",
                    "titik_monitor", "arah", "threshold_bps",
                    "baseline_enabled", "enabled", "status")
    list_filter = ("status", "monitor_type", "enabled", "baseline_enabled",
                   "device")
    search_fields = ("name", "service_id", "pppoe_username", "if_name")
    list_select_related = ("device",)
    readonly_fields = ("status", "status_changed_at")
    fieldsets = (
        (None, {"fields": ("name", "service_id", "enabled")}),
        ("Titik monitoring", {
            "fields": ("device", "monitor_type", "if_index", "if_name",
                       "pppoe_username"),
            "description": "Interface fisik → isi ifIndex. "
                           "PPPoE → isi username. "
                           "Belum tahu ifIndex? Buka Perangkat → Lihat interface.",
        }),
        ("Arah port", {
            "fields": ("if_direction",),
            "description": "Hanya untuk SNMP interface. Salah setel membuat "
                           "download dan upload tertukar di semua grafik dan "
                           "laporan — angkanya tetap terlihat masuk akal, "
                           "cuma terbalik.",
        }),
        ("Ambang alert", {"fields": ("threshold_bps",)}),
        ("Deteksi degradasi", {
            "fields": ("baseline_enabled", "baseline_drop_pct"),
            "description": "Menangkap pelanggan yang masih hidup tapi "
                           "traffic-nya jatuh jauh di bawah kebiasaannya. "
                           "Butuh riwayat minimal beberapa minggu.",
        }),
        ("Status terkini", {"fields": ("status", "status_changed_at")}),
    )

    @admin.display(description="Titik")
    def titik_monitor(self, obj):
        if obj.monitor_type == "snmp_if":
            return obj.if_name or f"ifIndex {obj.if_index}"
        return obj.pppoe_username or "—"


    @admin.display(description="Arah")
    def arah(self, obj):
        if obj.monitor_type != "snmp_if":
            return "—"
        return "ke pelanggan" if obj.if_direction == "ke_pelanggan" else "ke upstream"


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("subject", "alert_type", "severity", "started_at",
                    "resolved_at", "suppressed", "notified", "ack_by")
    list_filter = ("alert_type", "severity", "notified", "suppressed")
    search_fields = ("customer__name", "device__name")
    list_select_related = ("customer", "device")
    date_hierarchy = "started_at"

    @admin.display(description="Terkena")
    def subject(self, obj):
        return obj.subject

    def has_add_permission(self, request):
        return False


@admin.register(MaintenanceWindow)
class MaintenanceWindowAdmin(admin.ModelAdmin):
    list_display = ("name", "scope", "starts_at", "ends_at", "aktif",
                    "created_by")
    list_filter = ("device",)
    search_fields = ("name", "note")
    list_select_related = ("device", "customer")
    fieldsets = (
        (None, {"fields": ("name", "starts_at", "ends_at", "note")}),
        ("Cakupan", {
            "fields": ("device", "customer"),
            "description": "Kosongkan keduanya untuk menahan alert "
                           "SEMUA pelanggan. Isi salah satu untuk membatasi.",
        }),
    )

    @admin.display(description="Aktif", boolean=True)
    def aktif(self, obj):
        return obj.is_active

    @admin.display(description="Cakupan")
    def scope(self, obj):
        return obj.scope

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user.get_username()
        super().save_model(request, obj, form, change)

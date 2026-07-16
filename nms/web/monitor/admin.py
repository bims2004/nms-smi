"""Admin sebagai form CRUD inventory — inilah pengganti seed SQL manual."""
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import Alert, Customer, Device

admin.site.site_header = "NMS — Kelola inventory"
admin.site.site_title = "NMS"
admin.site.index_title = "Perangkat & pelanggan"


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("name", "ip", "vendor", "poll_method", "enabled",
                    "jumlah_pelanggan", "aksi")
    list_filter = ("vendor", "poll_method", "enabled")
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
                    "titik_monitor", "threshold_bps", "enabled", "status")
    list_filter = ("status", "monitor_type", "enabled", "device")
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
        ("Ambang alert", {"fields": ("threshold_bps",)}),
        ("Status terkini", {"fields": ("status", "status_changed_at")}),
    )

    @admin.display(description="Titik")
    def titik_monitor(self, obj):
        if obj.monitor_type == "snmp_if":
            return obj.if_name or f"ifIndex {obj.if_index}"
        return obj.pppoe_username or "—"


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("customer", "alert_type", "started_at", "resolved_at",
                    "notified")
    list_filter = ("alert_type", "notified")
    search_fields = ("customer__name",)
    list_select_related = ("customer",)
    date_hierarchy = "started_at"

    def has_add_permission(self, request):
        return False

from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("api/status/", views.status_json, name="status_json"),
    path("pelanggan/<int:pk>/", views.customer_detail, name="customer_detail"),
    path("pelanggan/<int:pk>/live/", views.customer_live, name="customer_live"),
    path("perangkat/", views.device_list, name="device_list"),
    path("impor/", views.customer_import, name="customer_import"),
    path("perangkat/<int:pk>/interface/", views.device_interfaces,
         name="device_interfaces"),
    path("gangguan/", views.alert_list, name="alert_list"),
    path("gangguan/<int:pk>/tangani/", views.alert_ack, name="alert_ack"),
    path("sla/", views.sla_report, name="sla_report"),
]

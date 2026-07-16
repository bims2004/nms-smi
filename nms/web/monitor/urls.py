from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("api/status/", views.status_json, name="status_json"),
    path("pelanggan/<int:pk>/", views.customer_detail, name="customer_detail"),
    path("perangkat/<int:pk>/interface/", views.device_interfaces,
         name="device_interfaces"),
    path("gangguan/", views.alert_list, name="alert_list"),
]

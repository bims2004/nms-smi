"""Django settings untuk NMS dashboard."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "ganti-secret-key-ini-di-env-untuk-produksi"
)
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"

# Kalau dashboard diakses lewat HTTPS (lihat profil caddy di docker-compose),
# setel HTTPS_ENABLED=1. Sengaja tidak diaktifkan otomatis: cookie-secure di
# server yang masih HTTP membuat login mustahil — browser mengirim cookie-nya,
# lalu Django menolaknya, dan orang terjebak di halaman login tanpa pesan error.
HTTPS_ENABLED = os.environ.get("HTTPS_ENABLED", "0") == "1"

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")
    if h.strip()
]

CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if o.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "monitor",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "nmsweb.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "nmsweb.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "nms"),
        "USER": os.environ.get("POSTGRES_USER", "nms"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "nmspass"),
        "HOST": os.environ.get("DB_HOST", "db"),
        "PORT": os.environ.get("DB_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "id-id"
TIME_ZONE = os.environ.get("TZ_DISPLAY", "Asia/Jakarta")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/"

# Interval polling SNMP collector — dipakai untuk menentukan data basi di UI
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 60))


# =========================================================
# Pengamanan
# =========================================================
# Berlaku selalu, tidak butuh HTTPS:
SECURE_CONTENT_TYPE_NOSNIFF = True      # jangan tebak-tebak tipe berkas
X_FRAME_OPTIONS = "DENY"                # cegah clickjacking lewat iframe
SESSION_COOKIE_HTTPONLY = True          # cookie tak terbaca JavaScript
CSRF_COOKIE_HTTPONLY = False            # dibaca fetch() di dashboard
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# Sesi berakhir setelah 12 jam. Dashboard NOC sering dibiarkan terbuka di
# layar dinding; sesi abadi berarti siapa pun yang lewat bisa memakainya.
SESSION_COOKIE_AGE = 12 * 3600

if HTTPS_ENABLED:
    # Reverse proxy (Caddy/nginx) yang memegang TLS, Django di belakangnya
    # bicara HTTP. Header ini yang memberitahunya bahwa aslinya HTTPS.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # HSTS: setahun, tapi TANPA preload dan TANPA subdomain.
    # Preload itu keputusan yang nyaris tidak bisa dibatalkan dan tidak pantas
    # dipilihkan diam-diam oleh sebuah template.
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    SECURE_HSTS_PRELOAD = False

if not DEBUG and "*" in ALLOWED_HOSTS:
    import warnings
    warnings.warn(
        "DJANGO_ALLOWED_HOSTS='*' di mode produksi. Django akan menerima Host "
        "header apa pun. Isi dengan IP/hostname server yang sebenarnya.",
        RuntimeWarning,
    )


# =========================================================
# Logging
# =========================================================
# Bawaan Django hanya mengirim traceback 500 ke console saat DEBUG=True. Di
# mode produksi, error request tidak tercatat ke mana pun kecuali ADMINS diisi
# untuk email. Akibatnya: halaman balas "Server Error (500)", log container
# bersih, dan tidak ada satu pun petunjuk kenapa.
#
# Config ini mengembalikan traceback ke stdout, yang di Docker berarti
# `docker compose logs web`.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "ringkas": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "ringkas",
        },
    },
    "loggers": {
        # Ini yang penting: traceback 500 muncul di log, bukan lenyap.
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "monitor": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

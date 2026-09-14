"""
Django settings for config project (BTC Wallet backend).
"""

from datetime import timedelta
from pathlib import Path
import os

from django.urls import reverse_lazy
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-change-me-in-.env")

DEBUG = env_bool("DEBUG", True)

ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]


INSTALLED_APPS = [
    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",

    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",

    "rest_framework",
    "rest_framework_simplejwt",
    "django_celery_beat",
    "django_celery_results",

    "accounts",
    "wallet",
    "webui",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Without this, logger.info()/.debug() calls throughout the codebase (wallet's
# monitoring commands especially — worker, blink_ws, scan_bitcoin — are entirely
# silent: Python's logging falls back to a "last resort" handler that only
# prints WARNING and above. LOG_LEVEL lets a deployment quiet this down (e.g.
# WARNING) without editing code.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s", "datefmt": "%Y-%m-%d %H:%M:%S"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "console"},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
}

LOGIN_URL = "webui:login"
LOGIN_REDIRECT_URL = "webui:dashboard"
LOGOUT_REDIRECT_URL = "webui:landing"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
}


# ── Lightning (Blink) / on-chain LND / AmatoPay ─────────────
# Read via python-decouple in wallet/options.py too; also exposed on
# `settings` directly because wallet/lnd_service.py reads it that way.

BLINK_API_KEY = os.getenv("BLINK_API_KEY", "")
BLINK_API_URL = os.getenv("BLINK_API_URL", "https://api.blink.sv/graphql")
BLINK_WS_URL = os.getenv("BLINK_WS_URL", "wss://ws.blink.sv/graphql")
BLINK_WS_USER_AGENT = os.getenv("BLINK_WS_USER_AGENT", "BtcWalletBlinkWS/1.0")

LND_REST_URL = os.getenv("LND_REST_URL", "")
LND_MACAROON = os.getenv("LND_MACAROON", "")
LND_CERT_PATH = os.getenv("LND_CERT_PATH", str(BASE_DIR / "tls.cert"))

AMATOPAY_API_KEY = os.getenv("AMATOPAY_API_KEY", "")
AMATOPAY_BASE_URL = os.getenv("AMATOPAY_BASE_URL", "http://localhost:8000")

# On-chain Bitcoin (btclib). Defaults to testnet4 on purpose, so switching to
# real funds ("mainnet") is a deliberate .env change, not an accident.
# testnet4 (not "testnet" = testnet3, which is largely dead in practice) is
# the modern usable test network — see wallet/esplora_client.py's
# module docstring for why testnet3/testnet4 are NOT interchangeable (same
# address format, completely different chains) and which provider serves
# which. WALLET_ENCRYPTION_KEY encrypts custodied private keys at rest
# (wallet/onchain_keys.py) — generate one with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
BITCOIN_NETWORK = os.getenv("BITCOIN_NETWORK", "testnet4")
WALLET_ENCRYPTION_KEY = os.getenv("WALLET_ENCRYPTION_KEY", "")


# ── Celery (Blink invoice-poll fallback task) ───────────────

CELERY_BROKER_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = "django-db"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_IMPORTS = ("wallet.celery_tasks",)
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_RESULT_EXTENDED = True
CELERY_TASK_TRACK_STARTED = True
CELERY_TIMEZONE = "UTC"
CELERY_ENABLE_UTC = True
CELERY_TASK_DEFAULT_QUEUE = "btc_wallet"
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"


# ── Django admin (Unfold) ───────────────────────────────────

UNFOLD = {
    "SITE_TITLE": "BTC Wallet Admin",
    "SITE_HEADER": "BTC Wallet",
    "SITE_SUBHEADER": "Bitcoin, Lightning & BIF operations",
    "SITE_SYMBOL": "currency_bitcoin",
    "SITE_URL": "/",
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": False,
    "DASHBOARD_CALLBACK": "config.admin_dashboard.dashboard_callback",
    "COLORS": {
        # Bitcoin-orange primary scale (close to Tailwind's "orange", centered on #f7931a).
        "primary": {
            "50": "255 247 237",
            "100": "255 237 213",
            "200": "254 215 170",
            "300": "253 186 116",
            "400": "251 146 60",
            "500": "247 147 26",
            "600": "234 88 12",
            "700": "194 65 12",
            "800": "154 52 18",
            "900": "124 45 18",
            "950": "67 20 7",
        },
    },
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": [
            {
                "title": "Overview",
                "items": [
                    {"title": "Dashboard", "icon": "space_dashboard", "link": reverse_lazy("admin:index")},
                ],
            },
            {
                "title": "Wallets",
                "separator": True,
                "items": [
                    {"title": "Wallets", "icon": "account_balance_wallet", "link": reverse_lazy("admin:wallet_wallet_changelist")},
                    {"title": "Transactions", "icon": "receipt_long", "link": reverse_lazy("admin:wallet_wallettransaction_changelist"), "badge": "config.admin_badges.pending_deposits", "badge_variant": "warning"},
                    {"title": "Withdrawal fee policies", "icon": "percent", "link": reverse_lazy("admin:wallet_withdrawalfeepolicy_changelist")},
                ],
            },
            {
                "title": "Bitcoin",
                "separator": True,
                "items": [
                    {"title": "HD wallet", "icon": "key", "link": reverse_lazy("admin:wallet_bitcoinhdwallet_changelist")},
                    {"title": "Platform addresses", "icon": "qr_code_2", "link": reverse_lazy("admin:wallet_platformbitcoinaddress_changelist")},
                ],
            },
            {
                "title": "BIF",
                "separator": True,
                "items": [
                    {"title": "Exchange rates", "icon": "currency_exchange", "link": reverse_lazy("admin:wallet_exchangerate_changelist")},
                    {"title": "POS charges", "icon": "point_of_sale", "link": reverse_lazy("admin:wallet_poscharge_changelist"), "badge": "config.admin_badges.pending_pos_charges", "badge_variant": "warning"},
                    {"title": "AmatoPay top-ups", "icon": "sync_alt", "link": reverse_lazy("admin:wallet_amatopaycheckoutsession_changelist"), "badge": "config.admin_badges.pending_topups", "badge_variant": "warning"},
                ],
            },
            {
                "title": "Platform & access",
                "separator": True,
                "items": [
                    {"title": "Users", "icon": "person", "link": reverse_lazy("admin:accounts_user_changelist")},
                    {"title": "Groups", "icon": "shield_person", "link": reverse_lazy("admin:auth_group_changelist")},
                    {"title": "Periodic tasks", "icon": "schedule", "link": reverse_lazy("admin:django_celery_beat_periodictask_changelist")},
                ],
            },
        ],
    },
}

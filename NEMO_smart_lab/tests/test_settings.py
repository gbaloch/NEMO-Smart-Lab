"""
Django settings for running NEMO_smart_lab's own test suite (see run_tests.py) - not used at
runtime. Trimmed down from NEMO's own resources/settings.py template (ROOT_URLCONF is the
real NEMO.urls, which touches a fair number of settings even during Django's system checks, so
this needs to be a reasonably complete settings module, not a minimal stub). Needs the real
NEMO package installed (pip install -e ".[NEMO]" or ".[NEMO-CE]") since NEMO_smart_lab depends
on NEMO's models/plugin utilities.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEBUG = True
SECRET_KEY = "test-secret-key"
AUTH_USER_MODEL = "NEMO.User"
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
WSGI_APPLICATION = "NEMO.wsgi.application"
ROOT_URLCONF = "NEMO.urls"
USE_TZ = True
USE_I18N = False
TIME_ZONE = "America/New_York"

ALLOWED_HOSTS = ["testserver", "localhost"]
SERVER_DOMAIN = "https://testserver"
CSRF_TRUSTED_ORIGINS = ["https://testserver"]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "login"

ALLOW_CONDITIONAL_URLS = True
INTERLOCKS_ENABLED = False
CUSTOMIZATIONS_CACHE_SECONDS = 30

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.admin",
    "django.contrib.humanize",
    "NEMO",
    "NEMO_smart_lab",
    "rest_framework",
    "rest_framework.authtoken",
    "django_filters",
    "mptt",
    "auditlog",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.common.BrokenLinkEmailsMiddleware",
    "NEMO.middleware.DeviceDetectionMiddleware",
    "NEMO.middleware.SessionTimeout",
    "NEMO.middleware.ImpersonateMiddleware",
    "NEMO.middleware.NEMOAuditlogMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "NEMO.context_processors.show_logout_button",
                "NEMO.context_processors.base_context",
                "django.contrib.auth.context_processors.auth",
                "django.template.context_processors.debug",
                "django.template.context_processors.media",
                "django.template.context_processors.static",
                "django.template.context_processors.tz",
                "django.template.context_processors.request",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

from rest_framework.settings import DEFAULTS  # noqa: E402

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ("NEMO.permissions.DjangoModelPermissions",),
    "DEFAULT_FILTER_BACKENDS": ("NEMO.rest_filter_backend.NEMOFilterBackend",),
    "DEFAULT_PARSER_CLASSES": DEFAULTS["DEFAULT_PARSER_CLASSES"] + ["NEMO.parsers.CSVParser"],
    "DEFAULT_PAGINATION_CLASS": "NEMO.rest_pagination.NEMOPageNumberPagination",
    "PAGE_SIZE": 1000,
}

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.path.join(BASE_DIR, "test_nemo.db"),
    }
}

STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"
STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "static")
MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(BASE_DIR, "media")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"": {"handlers": ["console"], "level": "WARNING"}},
}

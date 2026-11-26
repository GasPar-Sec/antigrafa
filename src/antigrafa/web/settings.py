"""Django settings, built from the ``[web]`` section (no settings module to edit)."""

from __future__ import annotations

import ipaddress
import secrets
import socket
from pathlib import Path

from ..config import WebConfig
from .files import private_dir, write_private

# "Every interface" addresses: only compared, never bound here
WILDCARDS = {"0.0.0.0", "::", ""}  # nosec B104


def local_names(listen: str = "") -> list[str]:
    """Names and addresses this machine answers to: the certificate and the allowed hosts."""
    names = ["localhost", "127.0.0.1", "::1"]
    if listen not in WILDCARDS:
        names.insert(0, listen)
    try:
        host = socket.gethostname()
        names[:0] = [n for n in (socket.getfqdn(host), host) if n]
        names += socket.gethostbyname_ex(host)[2]
    except OSError:
        pass  # no name resolution: localhost still works
    out = []
    for name in names:
        try:
            name = str(ipaddress.ip_address(name))
        except ValueError:
            name = name.lower()
        if name not in out:
            out.append(name)
    return out


def secret_key(directory: Path) -> str:
    """Signs the sessions: created once, kept in the data directory."""
    path = directory / "secret_key"
    if not path.exists():
        write_private(path, secrets.token_urlsafe(64).encode())
    return path.read_text().strip()


def build(web: WebConfig, config_path: str) -> dict:
    directory = private_dir(web.directory)
    return {
        "DEBUG": False,
        "SECRET_KEY": secret_key(directory),
        "ALLOWED_HOSTS": web.allowed_hosts or local_names(web.listen),
        "INSTALLED_APPS": [
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "django.contrib.sessions",
            "antigrafa.web",
        ],
        "MIDDLEWARE": [
            "django.middleware.security.SecurityMiddleware",
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.middleware.common.CommonMiddleware",
            "django.middleware.csrf.CsrfViewMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.middleware.clickjacking.XFrameOptionsMiddleware",
        ],
        "ROOT_URLCONF": "antigrafa.web.urls",
        "TEMPLATES": [
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "APP_DIRS": True,
                "OPTIONS": {
                    "context_processors": [
                        "django.template.context_processors.request",
                        "django.contrib.auth.context_processors.auth",
                    ]
                },
            }
        ],
        "DATABASES": {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(directory / "web.sqlite3")}},
        "DEFAULT_AUTO_FIELD": "django.db.models.AutoField",
        "AUTH_PASSWORD_VALIDATORS": [
            {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
            {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
            {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
            {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
        ],
        "LOGIN_URL": "login",
        "LOGIN_REDIRECT_URL": "home",
        "LOGOUT_REDIRECT_URL": "login",
        # HTTPS only: cookies never sent in clear, not readable by scripts
        "SESSION_COOKIE_SECURE": True,
        "SESSION_COOKIE_SAMESITE": "Strict",
        "SESSION_COOKIE_AGE": 12 * 3600,
        "CSRF_COOKIE_SECURE": True,
        "CSRF_COOKIE_HTTPONLY": True,
        "CSRF_COOKIE_SAMESITE": "Strict",
        "SECURE_CONTENT_TYPE_NOSNIFF": True,
        "SECURE_REFERRER_POLICY": "same-origin",
        "X_FRAME_OPTIONS": "DENY",
        "USE_TZ": True,
        "LOGGING_CONFIG": None,  # Antigrafa's logging stays as configured
        "ANTIGRAFA_CONFIG": config_path,
    }


def configure(web: WebConfig, config_path: str) -> None:
    """Configure and start Django, once per process."""
    import django
    from django.conf import settings

    if not settings.configured:
        settings.configure(**build(web, config_path))
    django.setup()

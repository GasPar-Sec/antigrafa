"""Starting the web interface over HTTPS (cheroot: pure Python, Linux and Windows)."""

from __future__ import annotations

import logging
import signal
import ssl
import threading

from ..config import Config, ConfigError
from .certs import CertificateError, describe, ensure_certificate
from .settings import WILDCARDS, configure, local_names

log = logging.getLogger(__name__)


def setup(cfg: Config, config_path: str) -> None:
    """Django configured, its database (users, sessions) created or upgraded."""
    from django.core.management import call_command

    configure(cfg.web, config_path)
    call_command("migrate", interactive=False, verbosity=0)


def certificate(cfg: Config) -> None:
    """The configured certificate, or a self-signed one created when there is none."""
    web = cfg.web
    try:
        reason = ensure_certificate(web.certificate, web.private_key, web.allowed_hosts or local_names(web.listen))
        info = describe(web.certificate)
    except CertificateError as exc:
        raise ConfigError(f"[web]: {exc}") from None
    if reason == "missing":
        log.warning("no certificate: self-signed certificate created in %s", web.certificate)
    elif reason == "expiring":
        log.warning("self-signed certificate renewed (it was about to expire): %s", web.certificate)
    log.info("certificate %s, valid until %s", web.certificate, f"{info.not_after:%Y-%m-%d}")
    if info.self_signed:
        log.info("self-signed: the browser warns on the first visit; check that it shows this SHA-256 fingerprint:")
        log.info("  %s", info.fingerprint)
        log.info("  for the names: %s", ", ".join(info.names))


def make_server(cfg: Config, config_path: str):
    """The HTTPS server, ready to start (nothing listens yet)."""
    from cheroot import wsgi
    from cheroot.ssl.builtin import BuiltinSSLAdapter
    from django.contrib.auth import get_user_model
    from django.core.wsgi import get_wsgi_application

    setup(cfg, config_path)
    if not get_user_model().objects.exists():
        raise ConfigError("no user can sign in yet: create one with 'antigrafa web adduser <name>'")
    certificate(cfg)
    web = cfg.web
    server = wsgi.Server((web.listen, web.port), get_wsgi_application(), numthreads=8, server_name="antigrafa")
    adapter = BuiltinSSLAdapter(str(web.certificate), str(web.private_key))
    adapter.context.minimum_version = ssl.TLSVersion.TLSv1_2
    server.ssl_adapter = adapter
    return server


def address(cfg: Config) -> str:
    host = cfg.web.listen
    if host in WILDCARDS:
        names = local_names()
        host = names[0] if names else "localhost"
    return f"https://[{host}]:{cfg.web.port}/" if ":" in host else f"https://{host}:{cfg.web.port}/"


def serve(cfg: Config, config_path: str, server=None) -> int:
    """Run until Ctrl+C (or SIGTERM): the running backup is then cancelled and cleaned up."""
    from .views import get_manager

    server = server if server is not None else make_server(cfg, config_path)
    manager = get_manager()
    previous = None
    if threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGTERM"):
        # systemd stops services with SIGTERM: stop like Ctrl+C, snapshots removed
        previous = signal.signal(signal.SIGTERM, _interrupt)
    log.info("web interface: %s", address(cfg))
    try:
        server.start()
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        server.stop()
        manager.shutdown()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    return 0


def _interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def add_user(cfg: Config, config_path: str, name: str, password: str) -> bool:
    """Create the user, or change its password: True when created."""
    from django.contrib.auth import get_user_model
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    setup(cfg, config_path)
    model = get_user_model()
    user = model.objects.filter(username=name).first()
    created = user is None
    if created:
        user = model(username=name)
    try:
        validate_password(password, user)
    except ValidationError as exc:
        raise ConfigError("password refused: " + " ".join(exc.messages)) from None
    user.set_password(password)
    user.save()
    return created


def remove_user(cfg: Config, config_path: str, name: str) -> bool:
    from django.contrib.auth import get_user_model

    setup(cfg, config_path)
    deleted, _ = get_user_model().objects.filter(username=name).delete()
    return bool(deleted)

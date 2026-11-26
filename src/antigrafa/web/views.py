"""Pages and JSON API of the web interface. Every view requires a logged-in user."""

from __future__ import annotations

import functools
import json
import logging
import threading

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from ..config import ConfigError
from ..platforms.base import AccessDenied, PlatformError
from ..targets import TargetUnavailable
from .manager import BackupManager, Busy

log = logging.getLogger(__name__)

_manager: BackupManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> BackupManager:
    """One manager per process: it owns the running backup."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = BackupManager(settings.ANTIGRAFA_CONFIG)
        return _manager


def set_manager(manager: BackupManager | None) -> None:
    global _manager
    with _manager_lock:
        _manager = manager


def api(view):
    """JSON view: 401 instead of a redirect to the login page; errors as {"error": ...}."""

    @functools.wraps(view)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "not logged in"}, status=401)
        try:
            return view(request, *args, **kwargs)
        except Busy as exc:
            return JsonResponse({"error": str(exc)}, status=409)
        except AccessDenied as exc:
            return JsonResponse({"error": str(exc)}, status=403)
        except ConfigError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except (TargetUnavailable, PlatformError, OSError) as exc:
            return JsonResponse({"error": str(exc)}, status=502)

    return wrapper


RUN_KINDS = ("backup", "restore")


def _page(request: HttpRequest, page: str):
    try:
        choices, error = get_manager().choices(), ""
    except ConfigError as exc:
        choices, error = {"sources": [], "targets": []}, str(exc)
    return render(request, f"antigrafa/{page}.html", {"choices": choices, "config_error": error, "page": page})


@login_required
@require_GET
def home(request: HttpRequest):
    """The home page: the backups running, this machine, its hosts and disks."""
    return render(request, "antigrafa/home.html", {"page": "home"})


@login_required
@require_GET
def backup_page(request: HttpRequest):
    return _page(request, "backup")


@login_required
@require_GET
def restore_page(request: HttpRequest):
    return _page(request, "restore")


@require_GET
@api
def status(request: HttpRequest):
    """The list of runs, and the details of one: ``?run=<id>`` or the most recent one.

    ``?kind=backup|restore`` keeps the runs of that kind only (each page shows its own); "active" is
    the running run, whatever its kind: only one runs at a time.
    """
    manager = get_manager()
    data = manager.status()
    data["active"] = next((r for r in data["runs"] if r["state"] in ("running", "cancelling")), None)
    kind = request.GET.get("kind") or ""
    if kind:
        if kind not in RUN_KINDS:
            raise ConfigError(f"unknown kind of run {kind!r}: {' or '.join(RUN_KINDS)}")
        data["runs"] = [r for r in data["runs"] if r["kind"] == kind]
    run_id = _int(request.GET.get("run"))
    run = manager.run(run_id) if run_id else None
    if run is None and data["runs"]:
        run = manager.run(data["runs"][0]["id"])
    data["run"] = run.details(_int(request.GET.get("logs_after")) or 0) if run else None
    return JsonResponse(data)


@require_GET
@api
def running(request: HttpRequest):
    """The backups running now, each with its overall progress (the home page)."""
    return JsonResponse({"running": get_manager().running()})


@require_GET
@api
def overview(request: HttpRequest):
    """This machine, the recent backups, the hosts of each source and the disks."""
    return JsonResponse(get_manager().overview())


@require_GET
@api
def guests(request: HttpRequest):
    return JsonResponse({"guests": get_manager().guests(request.GET.get("source") or None)})


@require_GET
@api
def restore_locations(request: HttpRequest):
    """The nodes of a source and their storages for a guest kind: ``?source=&kind=vm|ct``."""
    locations = get_manager().restore_locations(request.GET.get("source") or None, request.GET.get("kind") or "")
    return JsonResponse({"locations": locations})


@require_http_methods(["GET", "POST"])
@api
def backups(request: HttpRequest):
    """GET: the backups of a target (``?target=``); POST: start a backup."""
    if request.method == "GET":
        return JsonResponse({"backups": get_manager().backups(request.GET.get("target") or None)})
    return _start_backup(request)


@require_POST
@api
def restore(request: HttpRequest):
    body = _json(request)
    backup = body.get("backup")
    if not isinstance(backup, str) or not backup:
        raise ConfigError("choose the backup to restore")
    options = {}
    for name in ("guest_id", "host", "storage", "mode", "stop"):
        value = body.get(name)
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"{name} must be text")
        options[name] = (value or "").strip()
    if not isinstance(body.get("start", False), bool):
        raise ConfigError("start must be true or false")
    run = get_manager().start_restore(
        target=_text(body.get("target")),
        backup=backup,
        guest_id=options["guest_id"],
        host=options["host"],
        storage=options["storage"],
        mode=options["mode"] or "new",
        stop=options["stop"],
        start=body.get("start", False),
        user=request.user.get_username(),
    )
    return JsonResponse({"run": run.id}, status=202)


def _start_backup(request: HttpRequest):
    body = _json(request)
    run = get_manager().start(
        source=_text(body.get("source")),
        target=_text(body.get("target")),
        guests=_ids(body.get("guests")),
        every=body.get("all") is True,
        exclude=_ids(body.get("exclude")),
        user=request.user.get_username(),
    )
    return JsonResponse({"run": run.id}, status=202)


@require_POST
@api
def cancel(request: HttpRequest, run_id: int):
    if not get_manager().cancel(run_id, request.user.get_username()):
        return JsonResponse({"error": "this run is not running"}, status=409)
    return JsonResponse({"run": run_id, "state": "cancelling"})


@require_GET
@api
def jobs(request: HttpRequest):
    """The [job.<name>] sections (what, when, next and last run) and the scheduler's state."""
    return JsonResponse(get_manager().jobs())


@require_POST
@api
def run_job(request: HttpRequest, name: str):
    """Run a job now, followed on the page like any backup."""
    run = get_manager().start_job(name, user=request.user.get_username())
    return JsonResponse({"run": run.id}, status=202)


def _json(request: HttpRequest) -> dict:
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        raise ConfigError("the request is not valid JSON") from None
    if not isinstance(body, dict):
        raise ConfigError("the request must be a JSON object")
    return body


def _text(value) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError("source and target must be names")
    return value


def _ids(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError("guests and exclude must be lists of guest ids")
    return value


def _int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None

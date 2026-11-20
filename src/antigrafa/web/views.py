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
from django.views.decorators.http import require_GET, require_POST

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


@login_required
@require_GET
def index(request: HttpRequest):
    try:
        choices, error = get_manager().choices(), ""
    except ConfigError as exc:
        choices, error = {"sources": [], "targets": []}, str(exc)
    return render(request, "antigrafa/index.html", {"choices": choices, "config_error": error})


@require_GET
@api
def status(request: HttpRequest):
    """The list of runs, and the details of one: ``?run=<id>`` or the most recent one."""
    manager = get_manager()
    data = manager.status()
    run_id = _int(request.GET.get("run"))
    run = manager.run(run_id) if run_id else None
    if run is None and data["runs"]:
        run = manager.run(data["runs"][0]["id"])
    data["run"] = run.details(_int(request.GET.get("logs_after")) or 0) if run else None
    return JsonResponse(data)


@require_GET
@api
def guests(request: HttpRequest):
    return JsonResponse({"guests": get_manager().guests(request.GET.get("source") or None)})


@require_POST
@api
def start(request: HttpRequest):
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
        return JsonResponse({"error": "this backup is not running"}, status=409)
    return JsonResponse({"run": run_id, "state": "cancelling"})


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

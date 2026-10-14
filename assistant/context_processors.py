"""Контекстные процессоры приложения assistant."""

from __future__ import annotations

import os
from pathlib import Path

from django.conf import settings

# Версия статики пересчитывается по времени изменения файлов и живёт в памяти
# до перезапуска сервера. Нужна, чтобы браузер не держал в кэше устаревшие
# style.css и chat.js: без версии обновлённый JS не подхватывался, и вложения
# переставали доходить до сервера.
_static_version: str | None = None


def _compute_version() -> str:
    newest = 0
    for directory in getattr(settings, "STATICFILES_DIRS", []):
        root = Path(directory)
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                newest = max(newest, int(path.stat().st_mtime))
    return str(newest or 0)


def static_version(request) -> dict:
    global _static_version
    if _static_version is None:
        _static_version = _compute_version()
    return {"STATIC_VERSION": _static_version}

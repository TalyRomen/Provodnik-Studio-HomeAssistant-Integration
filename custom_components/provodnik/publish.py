"""Файлы публикаций: проверка сборки, каталоги версий, безопасное чтение.

Без зависимостей от Home Assistant, чтобы логику можно было проверять отдельно.
Раскладка на диске:

    /config/provodnik/<slug>/<version>/index.html …

Версия пишется во временный каталог и переименовывается целиком, поэтому
оборванная загрузка никогда не видна как готовая версия. Какая версия активна,
хранит реестр (см. publications.py), а не файловая система.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import time
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
VERSION_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
PATH_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$")
ALLOWED_EXT = {
    ".html", ".css", ".js", ".mjs", ".json", ".txt", ".svg", ".png", ".jpg",
    ".jpeg", ".webp", ".gif", ".avif", ".ico", ".woff", ".woff2", ".mp4", ".webm",
}
MAX_FILES = 500
MAX_TOTAL = 12 * 1024 * 1024  # лимит тела запроса HA — 16 МБ, base64 добавляет треть
KEEP_VERSIONS = 3  # полная история — в файле проекта Studio на Mac


class PublishError(ValueError):
    """Сборка отклонена; текст показывается в Studio."""


def check_slug(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise PublishError("Имя публикации: латиница в нижнем регистре, цифры и дефис, до 40 символов")
    return slug


def check_files(files: dict[str, bytes]) -> int:
    """Проверить состав сборки. Возвращает суммарный размер."""
    if not files:
        raise PublishError("Сборка пустая")
    if "index.html" not in files:
        raise PublishError("В сборке нет index.html")
    if len(files) > MAX_FILES:
        raise PublishError(f"Слишком много файлов (больше {MAX_FILES})")
    total = 0
    for path, data in files.items():
        if not isinstance(path, str) or not PATH_RE.match(path) or len(path) > 200:
            raise PublishError(f"Недопустимый путь в сборке: {path!r}")
        if os.path.splitext(path)[1].lower() not in ALLOWED_EXT:
            raise PublishError(f"Недопустимый тип файла: {path}")
        total += len(data)
    if total > MAX_TOTAL:
        raise PublishError(f"Сборка больше {MAX_TOTAL // (1024 * 1024)} МБ")
    return total


def new_version_id(now: float | None = None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now if now is not None else time.time()))
    return f"{stamp}-{secrets.token_hex(2)}"


def write_version(root: Path, slug: str, files: dict[str, bytes], version: str) -> Path:
    """Записать версию целиком; до переименования она не существует."""
    check_slug(slug)
    check_files(files)
    base = root / slug
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / f".upload-{version}"
    final = base / version
    if final.exists():
        raise PublishError("Такая версия уже есть")
    try:
        for path, data in files.items():
            target = tmp / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        tmp.rename(final)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    return final


def remove_version(root: Path, slug: str, version: str) -> None:
    if SLUG_RE.match(slug) and VERSION_RE.match(version):
        shutil.rmtree(root / slug / version, ignore_errors=True)


def remove_publication(root: Path, slug: str) -> None:
    if SLUG_RE.match(slug):
        shutil.rmtree(root / slug, ignore_errors=True)


def resolve(root: Path, slug: str, version: str, path: str) -> Path | None:
    """Путь к файлу активной версии или None. Выход за пределы версии невозможен."""
    if not SLUG_RE.match(slug) or not VERSION_RE.match(version):
        return None
    path = path or "index.html"
    if path.endswith("/"):
        path += "index.html"
    if not PATH_RE.match(path):
        return None
    base = (root / slug / version).resolve()
    target = (base / path).resolve()
    if base not in target.parents or not target.is_file():
        return None
    return target

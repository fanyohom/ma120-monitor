"""Validated, atomic storage for the local MA120 watchlist."""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path


DEFAULT_PATH = Path(__file__).resolve().parent / "watchlist.json"


class WatchlistError(ValueError):
    """The watchlist cannot be read or does not match its schema."""


def normalize_code(value):
    """Use the same six-digit A-share and HKxxxxx forms as the stock monitor."""
    if not isinstance(value, str):
        raise WatchlistError("股票代码必须是字符串")
    code = value.strip().upper()
    if code.endswith(".0"):
        code = code[:-2]
    if code.startswith("HK"):
        digits = code[2:]
        if not digits.isdigit() or not 1 <= len(digits) <= 5:
            raise WatchlistError(f"港股代码无效：{value}")
        return f"HK{digits.zfill(5)}"
    if not re.fullmatch(r"[0-9]{1,6}", code):
        raise WatchlistError(f"股票代码无效：{value}")
    return code.zfill(6)


def _validate_items(items):
    if not isinstance(items, list):
        raise WatchlistError("watchlist.items 必须是数组")
    normalized = []
    seen = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict) or set(item) != {"code", "name"}:
            raise WatchlistError(f"第 {index} 项必须包含 code 和 name")
        code = normalize_code(item["code"])
        name = item["name"]
        if not isinstance(name, str) or not name.strip():
            raise WatchlistError(f"第 {index} 项股票名称不能为空")
        if code in seen:
            raise WatchlistError(f"重复的股票代码：{code}")
        seen.add(code)
        normalized.append({"code": code, "name": name.strip()})
    return normalized


def read_watchlist(path=None):
    path = Path(path) if path is not None else DEFAULT_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WatchlistError(f"读取 {path.name} 失败：{exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"version", "items"}:
        raise WatchlistError("watchlist.json 必须包含 version 和 items")
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise WatchlistError("不支持的 watchlist.json 版本")
    return _validate_items(raw["items"])


def write_watchlist(items, path=None, *, create_only=False):
    path = Path(path) if path is not None else DEFAULT_PATH
    normalized = _validate_items(items)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "items": normalized}, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if create_only:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise WatchlistError(f"{path.name} 已存在，拒绝覆盖") from exc
        else:
            if path.exists():
                read_watchlist(path)
                backup = path.with_name(f"{path.name}.bak")
                backup_fd, backup_temporary = tempfile.mkstemp(
                    prefix=f".{backup.name}.", suffix=".tmp", dir=path.parent
                )
                try:
                    with os.fdopen(backup_fd, "wb") as output, path.open("rb") as source:
                        shutil.copyfileobj(source, output)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(backup_temporary, backup)
                finally:
                    if os.path.exists(backup_temporary):
                        os.unlink(backup_temporary)
            os.replace(temporary, path)
        return normalized
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

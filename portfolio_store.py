"""Validated, atomic storage for the local MA120 portfolio."""

import json
import math
import os
import shutil
import tempfile
from pathlib import Path

from watchlist_store import WatchlistError, normalize_code


DEFAULT_PATH = Path(__file__).resolve().parent / "portfolio.json"


class PortfolioError(ValueError):
    """The portfolio cannot be read or does not match its schema."""


def _validate_items(items):
    if not isinstance(items, list):
        raise PortfolioError("portfolio.items 必须是数组")
    normalized = []
    seen = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict) or set(item) != {"code", "name", "cost", "shares"}:
            raise PortfolioError(f"第 {index} 项必须包含 code、name、cost 和 shares")
        try:
            code = normalize_code(item["code"])
        except WatchlistError as exc:
            raise PortfolioError(str(exc)) from exc
        name = item["name"]
        if not isinstance(name, str) or not name.strip():
            raise PortfolioError(f"第 {index} 项股票名称不能为空")
        if code in seen:
            raise PortfolioError(f"重复的股票代码：{code}")
        values = {}
        for field in ("cost", "shares"):
            value = item[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PortfolioError(f"第 {index} 项 {field} 必须是非负数字")
            try:
                number = float(value)
            except (OverflowError, ValueError) as exc:
                raise PortfolioError(f"第 {index} 项 {field} 必须是有限数字") from exc
            if not math.isfinite(number) or number < 0:
                raise PortfolioError(f"第 {index} 项 {field} 必须是有限的非负数字")
            values[field] = number
        seen.add(code)
        normalized.append({"code": code, "name": name.strip(), **values})
    return normalized


def read_portfolio(path=None):
    path = Path(path) if path is not None else DEFAULT_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PortfolioError(f"未找到 {path.name}；请先迁移或创建持仓 JSON") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PortfolioError(f"读取 {path.name} 失败：{exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"version", "items"}:
        raise PortfolioError("portfolio.json 必须包含 version 和 items")
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise PortfolioError("不支持的 portfolio.json 版本")
    return _validate_items(raw["items"])


def write_portfolio(items, path=None, *, create_only=False):
    path = Path(path) if path is not None else DEFAULT_PATH
    normalized = _validate_items(items)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "items": normalized}, stream, ensure_ascii=False, indent=2,
                      allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if create_only:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise PortfolioError(f"{path.name} 已存在，拒绝覆盖") from exc
        else:
            if path.exists():
                read_portfolio(path)
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

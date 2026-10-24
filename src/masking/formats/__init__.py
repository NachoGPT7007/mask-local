"""格式处理器自动装配。

约定：``formats/`` 下每个模块导出一个 ``HANDLER`` 实例，实现
``FormatHandler`` 协议（extract / apply / audit）。本模块按文件后缀路由。

新增格式（如 .pptx、.csv）只需新增模块并定义 ``HANDLER``，
CLI 与检测器无需改动。
"""

from __future__ import annotations

import importlib
import pkgutil
import pathlib

from ..entity import FormatHandler

_SKIP = {"__init__", "base", "registry"}
_handlers: list[FormatHandler] | None = None


def all_handlers() -> list[FormatHandler]:
    global _handlers
    if _handlers is not None:
        return _handlers
    found: list[FormatHandler] = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_") or info.name in _SKIP:
            continue
        module = importlib.import_module(f".{info.name}", __package__)
        handler = getattr(module, "HANDLER", None)
        if handler is not None:
            found.append(handler)
    _handlers = found
    return found


def handler_for(path: str | pathlib.Path) -> FormatHandler | None:
    suffix = pathlib.Path(path).suffix.lower()
    for handler in all_handlers():
        if suffix in handler.suffixes:
            return handler
    return None


def supported_suffixes() -> list[str]:
    out: list[str] = []
    for handler in all_handlers():
        out.extend(handler.suffixes)
    return sorted(set(out))


__all__ = ["all_handlers", "handler_for", "supported_suffixes"]

"""检测器自动装配。

约定：``detectors/`` 下每个模块提供一个 ``make(settings) -> Detector`` 工厂，
本函数扫描目录自动收集，新增检测器无需改动任何注册表。

跳过清单里的模块不参与自动装配（基础设施，不是检测器）。
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Sequence

from ..config import Settings
from ..entity import Detector

_SKIP = {"__init__", "base", "registry", "validators"}


def build_detectors(settings: Settings) -> list[Detector]:
    out: list[Detector] = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_") or info.name in _SKIP:
            continue
        module = importlib.import_module(f".{info.name}", __package__)
        factory = getattr(module, "make", None)
        if factory is None:
            continue
        det = factory(settings)
        if det is not None:
            out.append(det)
    out.sort(key=lambda d: getattr(d, "order", 50))
    return out


__all__ = ["build_detectors"]

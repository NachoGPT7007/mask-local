"""扫描件 OCR 封装：RapidOCR 单例 + 像素框归一化。

为什么单独成模块
----------------
1. **单例**：RapidOCR 首次加载约 0.9s、单页推理 1~3s。逐个 TextBlock 或逐页重建
   引擎会让一份几十页的扫描卷宗从几十秒涨到几分钟，所以引擎必须在进程内复用。
2. **坐标归一化**：RapidOCR 返回的 ``box`` 是四个点的列表，点的顺序与倾斜角度
   由检测器决定、并不保证轴对齐。这里统一成 ``(x0, y0, x1, y1)`` 的外接矩形，
   同时保留原始四点 ``quad``，供上层按需做倾斜校正。
3. **阅读顺序**：识别结果按视觉行排序，保证 TextBlock 顺序与人工阅读一致，
   审计报告和「需人工复核」清单才读得下去。

坐标约定：``OcrLine.box`` 永远是**图片像素坐标**，不是 PDF 坐标。像素→PDF 的
换算（``72 / dpi``）由 pdf_handler 负责，因为只有它知道渲染分辨率。
"""

from __future__ import annotations

import io
import pathlib
import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OcrLine:
    """一行 OCR 结果。

    text  : 识别文本（已 strip；纯空白行不会出现在结果里）
    box   : 轴对齐外接矩形，图片像素坐标 (x0, y0, x1, y1)
    score : 识别置信度 0~1
    quad  : 检测器原始四点，保留给倾斜页面备用
    """

    text: str
    box: tuple[float, float, float, float]
    score: float = 0.0
    quad: tuple[tuple[float, float], ...] = ()


#: 引擎单例。加载一次的代价是 0.9s，之后每页只付推理成本。
_ENGINE: Any = None
_ENGINE_LOCK = threading.Lock()
#: onnxruntime 的 Session 虽可并发 run，但 RapidOCR 的前后处理用了共享缓冲，
#: 串行化调用最稳妥；本工具本来就是单文件串行处理，没有性能损失。
_RECOGNIZE_LOCK = threading.Lock()


def engine() -> Any:
    """返回进程内唯一的 RapidOCR 引擎（懒加载，线程安全）。"""
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                from rapidocr_onnxruntime import RapidOCR

                _ENGINE = RapidOCR()
    return _ENGINE


def reset_engine() -> None:
    """释放引擎（测试用，正常流程不需要）。"""
    global _ENGINE
    with _ENGINE_LOCK:
        _ENGINE = None


def _to_ndarray(image: Any) -> Any:
    """把 path / bytes / PIL.Image / ndarray 统一成 HxWx3 的 uint8 数组。

    RapidOCR 只接受 ndarray 或文件路径；这里统一转换，让上层不必关心来源。
    """
    import numpy as np

    if isinstance(image, np.ndarray):
        return image

    if isinstance(image, (bytes, bytearray)):
        from PIL import Image

        with Image.open(io.BytesIO(bytes(image))) as im:
            return np.array(im.convert("RGB"))

    if isinstance(image, (str, pathlib.Path)):
        from PIL import Image

        with Image.open(str(image)) as im:
            return np.array(im.convert("RGB"))

    # PIL.Image 及其子类
    if hasattr(image, "convert") and hasattr(image, "width"):
        return np.array(image.convert("RGB"))

    raise TypeError(f"recognize() 不支持的图像类型: {type(image)!r}")


def _sort_reading_order(lines: list[OcrLine]) -> list[OcrLine]:
    """按阅读顺序排序：先按视觉行分组（行高的一半作为容差），行内再按 x。

    不做严格的行聚类：扫描件轻微倾斜时，同一物理行的 y 会漂移，
    用中位行高的 0.6 倍做量化桶足够把同一行的框归到一起。
    """
    if len(lines) <= 1:
        return lines
    heights = sorted(max(1.0, ln.box[3] - ln.box[1]) for ln in lines)
    unit = max(4.0, heights[len(heights) // 2] * 0.6)

    def key(ln: OcrLine) -> tuple[float, float]:
        center_y = (ln.box[1] + ln.box[3]) / 2.0
        return (round(center_y / unit), ln.box[0])

    return sorted(lines, key=key)


def recognize(image: Any, *, min_score: float = 0.30) -> list[OcrLine]:
    """识别一张图片，返回按阅读顺序排列的文本行。

    参数
    ----
    image     : 图片路径 / bytes / PIL.Image / ndarray（PDF 渲染的 PNG 字节可直接传）
    min_score : 低于该置信度的行直接丢弃。默认 0.30 足够宽松——
                脱敏宁可多检测（多擦一块不影响可读性），不可漏检。

    空列表表示「这张图里没有识别到文字」，是有效结果而不是失败。
    """
    array = _to_ndarray(image)
    if array.size == 0:
        return []

    with _RECOGNIZE_LOCK:
        result, _elapse = engine()(array)

    lines: list[OcrLine] = []
    for item in result or []:
        try:
            box, text, score = item[0], item[1], float(item[2])
        except (IndexError, TypeError, ValueError):
            # 引擎偶发返回残缺结构：跳过该行，不影响整页
            continue
        text = (text or "").strip()
        if not text or score < min_score:
            continue
        try:
            points = [(float(p[0]), float(p[1])) for p in box]
        except (IndexError, TypeError, ValueError):
            continue
        if not points:
            continue
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        lines.append(
            OcrLine(
                text=text,
                box=(min(xs), min(ys), max(xs), max(ys)),
                score=score,
                quad=tuple(points),
            )
        )
    return _sort_reading_order(lines)


__all__ = ["OcrLine", "engine", "recognize", "reset_engine"]

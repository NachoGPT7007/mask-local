"""用户自定义词表检测。

难点与取舍
----------
1. **词表缺失不是错误**。第一次使用时 ``config/custom_terms.txt`` 通常不存在，
   此时检测器必须安静地返回空列表，不能抛异常、不能打断整条流水线。
2. **懒加载 + 变更自动重载**。文件在进程启动之后才被用户创建或修改是常态，
   因此在首次 ``detect`` 时才读取，并按 ``mtime`` 判断是否需要重读，
   免得用户改了词表还要重启程序。
3. **长词优先**。用户可能同时写了「张三」和「张三丰」，交替正则按长度倒序，
   保证优先命中更长的那个；否则「张三丰」只会被掩成「【熊猫】丰」。
4. 命中一律 ``EntityType.CUSTOM``、置信度 1.0——用户显式写了这个词，
   就是最高优先级的人工指令。
5. 跳过 ``【】`` 区间，避免把已生成的代号再识别一遍。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from ..config import Settings, config_dir
from ..entity import Detector, Entity, EntityType, MatchContext, TextBlock
from .validators import masked_ranges, within


class CustomTermDetector:
    """自定义词表检测器。``order=50``。"""

    name = "custom"
    order = 50

    def __init__(self, settings: Settings, terms_path: Path | None = None) -> None:
        self.settings = settings
        self._path: Path | None = terms_path
        self._pattern: re.Pattern[str] | None = None
        self._mtime: float | None = None
        self._loaded = False

    # ------------------------------------------------------------------ 词表

    @property
    def path(self) -> Path:
        """词表路径。默认 ``<项目>/config/custom_terms.txt``。"""
        return self._path if self._path is not None else (config_dir() / "custom_terms.txt")

    @staticmethod
    def parse_terms(raw: str) -> list[str]:
        """解析词表文本：一行一个词，``#`` 开头为注释，空行跳过。"""
        terms: list[str] = []
        for line in raw.splitlines():
            word = line.strip()
            if not word or word.startswith("#"):
                continue
            terms.append(word)
        # 去重并保持长度倒序，保证长词优先命中
        return sorted(set(terms), key=len, reverse=True)

    def _ensure_loaded(self) -> None:
        """首次调用或文件变更时重建正则。"""
        path = self.path
        try:
            stat = path.stat()
        except OSError:
            # 文件不存在 / 不可读：视为空词表，属于正常状态
            self._pattern = None
            self._loaded = True
            self._mtime = None
            return
        if self._loaded and self._mtime == stat.st_mtime:
            return
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            self._pattern = None
            self._loaded = True
            self._mtime = None
            return
        terms = self.parse_terms(raw)
        self._pattern = (
            re.compile("|".join(re.escape(t) for t in terms)) if terms else None
        )
        self._mtime = stat.st_mtime
        self._loaded = True

    # ------------------------------------------------------------------ 检测

    def detect(self, block: TextBlock, ctx: MatchContext) -> Sequence[Entity]:
        """返回本块内命中的自定义词，偏移在界内、绝不抛异常。"""
        hits: list[Entity] = []
        try:
            text = block.text
            if not text:
                return hits
            self._ensure_loaded()
            if self._pattern is None:
                return hits
            masked = masked_ranges(text)
            for match in self._pattern.finditer(text):
                start, end = match.span()
                if start == end or within(masked, start, end):
                    continue
                hits.append(
                    Entity(
                        type=EntityType.CUSTOM,
                        text=text[start:end],
                        start=start,
                        end=end,
                        confidence=1.0,
                        detector=self.name,
                    )
                )
        except Exception:
            # 检测器绝不向上抛异常：丢召回比丢一条异常更危险
            pass
        return hits


def make(settings: Settings) -> Detector:
    """检测器工厂，供 ``detectors/__init__.py`` 自动装配。"""
    return CustomTermDetector(settings)


__all__ = ["CustomTermDetector", "make"]

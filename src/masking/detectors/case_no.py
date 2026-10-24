r"""案号检测。

难点与取舍
----------
1. **括号形态极多**。法院文书里见过 ``(2024)``、``（2024）``、``〔2024〕``、
   ``[2024]`` 四种开头，还有 ``（ 2024 ）`` 这种括号内带空格的排版残留。
   全部纳入字符类，用 ``\s*`` 吸收空格。
2. **PDF 会把案号断成两行**。``（2025）浙0192`` + 换行 + ``民初18143号`` 很常见，
   因此年份、院代字、案件类型、序号之间一律允许空白（含换行），能拼回整号。
3. **同一案号必须产生逐字节相同的 text**。原件里同一个案子可能写作
   ``（2025）浙0192民初18143号``，目录页又写作 ``(2025)浙0192民初18143号``；
   若按原文切分，分配器会把同一个案号当成两个实体、发两个代号。
   因此在输出前做**归一化**：全角数字 → 半角，四种括号统一成 ``(`` / ``)``，
   去掉全部空白。归一化只作用于 ``Entity.text``（分配器的映射键与解析输入），
   ``start/end`` 仍是原文偏移，回写、定位不受影响。
4. **必须以「号」收尾**，否则只能说明括号里有个年份，不能说明是案号；
   靠这一条把「（2024）年度报告」这类文字排除掉。
5. 分配器负责把年份、案件类型留下、院代字替换成自编代码；
   检测层不改写原文，只提供归一化后的识别结果。
"""

from __future__ import annotations

import re
from typing import Sequence

from ..config import Settings
from ..entity import Detector, Entity, EntityType, MatchContext, TextBlock
from .validators import masked_ranges, within

#: 案号：
#:   括号（半角/全角/六角/方） + 4 位年份 + 括号 + 院代字 + 案件类型 + 序号 + 号
_CASE_NO_RE = re.compile(
    r"[\(（〔\[［]"
    r"\s*[12０１２][09０９][0-9０-９]{2}\s*"
    r"[\)）〕\]］]"
    r"\s*[\u4e00-\u9fa5]{1,4}\s*[0-9０-９]{0,4}\s*"
    r"[\u4e00-\u9fa5]{1,4}\s*"
    # 序号可能是数字，也可能是原文自己隐去的 X/×/＊（如「民初XX号」）。
    # 只认数字时整段案号不匹配，法院代字（浙0192）会原样留在脱敏件里。
    r"(?:[0-9０-９]{1,7}|[Xx×＊*]{1,7})\s*号"
    r"(?![0-9０-９A-Za-z])"
)

#: 归一化映射：全角数字 → 半角；四种括号统一成半角圆括号。
#: 统一成 ``(`` / ``)`` 还有一个作用：分配器的 CASE_NO_RE 只认圆括号，
#: 若保留 ``〔`` 会解析失败退化成掩码，年份与案件类型就丢了。
_NORMALIZE_TABLE = str.maketrans(
    {
        "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
        "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
        "（": "(", "）": ")",
        "〔": "(", "〕": ")",
        "［": "(", "］": ")",
        "[": "(", "]": ")",
    }
)


def normalize_case_no(raw: str) -> str:
    """把案号归一化成稳定形式，供分配器做映射键与解析输入。

    规则：全角数字 → 半角；四种括号 → ``(`` / ``)``；删除全部空白。
    例：``（2025）浙0192民初18143号`` / ``(2025)浙 0192 民初 18143 号``
    都归一化为 ``(2025)浙0192民初18143号``。
    """
    if not isinstance(raw, str):
        return ""
    return re.sub(r"\s+", "", raw.translate(_NORMALIZE_TABLE))


class CaseNoDetector:
    """案号检测器。``order=20``。"""

    name = "case_no"
    order = 20

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def detect(self, block: TextBlock, ctx: MatchContext) -> Sequence[Entity]:
        """返回本块内的案号实体，偏移在界内、绝不抛异常。

        注意 ``Entity.text`` 是**归一化后**的案号，可能与 ``block.text[start:end]``
        不完全一致（全角括号、断行空白）。这是刻意为之：同一案号在全文各处
        必须拿到同一个代号，而偏移仍指向原文，回写与定位不受影响。
        """
        hits: list[Entity] = []
        try:
            text = block.text
            if not text:
                return hits
            masked = masked_ranges(text)
            for match in _CASE_NO_RE.finditer(text):
                start, end = match.span()
                if within(masked, start, end):
                    continue
                canonical = normalize_case_no(text[start:end])
                if not canonical:
                    continue
                hits.append(
                    Entity(
                        type=EntityType.CASE_NO,
                        text=canonical,
                        start=start,
                        end=end,
                        confidence=0.95,
                        detector=self.name,
                    )
                )
        except Exception:
            # 检测器绝不向上抛异常：丢召回比丢一条异常更危险
            pass
        return hits


def make(settings: Settings) -> Detector:
    """检测器工厂，供 ``detectors/__init__.py`` 自动装配。"""
    return CaseNoDetector(settings)


__all__ = ["CaseNoDetector", "make", "normalize_case_no"]

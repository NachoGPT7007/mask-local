"""规则类实体检测：身份证、手机、固话、银行卡、统一社会信用代码、车牌、邮箱。

难点与取舍
----------
1. **一段数字只能归一类**。18 位纯数字既可能是身份证，也可能是银行卡、
   统一社会信用代码。若每个正则各扫一遍，同一串数字会被输出三次，
   分配器给出三个代号，回写时互相打架。本模块因此不按「正则并发」写，
   而是先把连续数字切成 token，再用一条判定链按「长度 → 校验位 → 前缀特征」
   给出唯一结论。
2. **校验优先，但不因校验失败而沉默**。身份证校验不过 → 0.6；银行卡 Luhn
   不过 → 0.5。低置信度仍然上报，由 engine 的复核清单兜底。
   漏报是泄密，误报只是多一条待复核。
3. **数值 token 要求足够长**。短于 10 位的一律不参与判定，避免把案号里的
   「1234」、年份、金额当证件号。唯一的例外是 15/16-19 位这类「只可能是
   证件」的长度。
4. **邻接字符检查**。数字串紧邻字母、``@``、``.``、``_`` 时视为更长标识符的
   一部分（例如邮箱本地部分、产品编号），不单独上报。
5. 车牌与邮箱独立扫描；最终按「类型优先级 → 长度 → 置信度」做一次内部
   重叠消解，保证同一检测器不输出重叠命中。
"""

from __future__ import annotations

import re
from typing import Sequence

from ..config import Settings
from ..entity import Detector, Entity, EntityType, MatchContext, TextBlock
from .validators import (
    id_card_birth_date,
    is_plausible_phone,
    is_valid_id_card,
    is_valid_luhn,
    is_valid_uscc,
    masked_ranges,
    within,
)

#: 全角数字与全角连字符 → 半角，便于统一校验；实体原文仍用原串
_HALFWIDTH_TABLE = str.maketrans(
    "０１２３４５６７８９－‑—", "0123456789---"
)

#: 连续数字 token：允许中间夹连字符（固话常写成 0571-88888888）
_DIGIT_TOKEN_RE = re.compile(r"[0-9０-９]+(?:[-－‑][0-9０-９]+)*")

#: 紧邻这些字符的数字串视为更长标识符的一部分
_IDENTIFIER_CHARS = frozenset("@._%+-")

#: 车牌：省份简称 + 发牌机关字母（不含 I、O）+ 5 位（普通）或 6 位（新能源）
_PROVINCES = "京津冀晋蒙辽吉黑沪苏浙皖闽赣鲁豫鄂湘粤桂琼渝川贵云藏陕甘青宁新港澳台"
_PLATE_RE = re.compile(
    rf"[{_PROVINCES}][A-HJ-NP-Z](?:[0-9A-HJ-NP-Z]{{6}}|[0-9A-HJ-NP-Z]{{5}})"
    r"(?![0-9A-Za-z])",
    re.IGNORECASE,
)

#: 邮箱：常见本地部分字符 + 域名 + 至少一个点 + 2-24 位字母顶级域
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}"
)

#: 统一社会信用代码：18 位，字符集不含 I O S V Z，且**至少含一个字母**。
#: 纯数字的 18 位交给 ``classify_number`` 判（它还要和身份证、银行卡比优先级），
#: 这里只负责「含字母」这一种——数字 token 正则切不出它，必须单独扫。
_USCC_RE = re.compile(
    r"(?<![0-9A-Za-z])"
    r"(?=[0-9A-HJ-NPQRTUWXY]{0,17}[A-HJ-NPQRTUWXY])"
    r"[0-9A-HJ-NPQRTUWXY]{18}"
    r"(?![0-9A-Za-z])",
    re.IGNORECASE,
)

#: 内部重叠消解优先级（与 engine.TYPE_PRIORITY 同量级，但只管本检测器内部）
_PRIORITY: dict[EntityType, int] = {
    EntityType.ID_CARD: 100,
    EntityType.USCC: 100,
    EntityType.BANK_CARD: 100,
    EntityType.PHONE: 95,
    EntityType.PLATE: 95,
    EntityType.EMAIL: 95,
}


def _to_halfwidth(s: str) -> str:
    """全角数字/连字符转半角。"""
    return s.translate(_HALFWIDTH_TABLE)


def _looks_like_id_shape(clean: str) -> bool:
    """18 位纯数字是否具备身份证的外形：省级前缀 + 真实出生日期。

    这一条只用于「校验位没过时该报什么」，不能单独用来断定是身份证。
    """
    birth = id_card_birth_date(clean)
    return birth is not None and clean[:2] in {
        "11", "12", "13", "14", "15", "21", "22", "23", "31", "32", "33", "34",
        "35", "36", "37", "41", "42", "43", "44", "45", "46", "50", "51", "52",
        "53", "54", "61", "62", "63", "64", "65", "71", "81", "82",
    }


def classify_number(clean: str) -> tuple[EntityType, float] | None:
    """给一条已经去分隔符的数字串定性。

    参数
    ----
    clean: 半角化、去掉连字符后的字符序列（身份证末位可能是 X/x）。

    返回
    ----
    ``(类型, 置信度)``；不属于任何一类时返回 ``None``。

    判定链（顺序即优先级）
    ----------------------
    1. 18 位（末位可为 X）：身份证 → 统一社会信用代码 → 有身份证外形则
       低置信度身份证 → 交给银行卡分支。
    2. 15 位：只可能是旧身份证，要求省级前缀 + 出生日期都合理。
    3. 16-19 位：银行卡。Luhn 通过 1.0；不通过且 62 开头 0.5。
    4. 11 位 ``1[3-9]``：手机号。
    5. 10-12 位且 0 开头：固话。
    """
    n = len(clean)

    # ---- 18 位：身份证 / 统一社会信用代码 / 银行卡 --------------------------
    if n == 18:
        if is_valid_id_card(clean):
            return EntityType.ID_CARD, 1.0
        if is_valid_uscc(clean):
            return EntityType.USCC, 1.0
        if _looks_like_id_shape(clean) and clean[17] in "0123456789Xx":
            return EntityType.ID_CARD, 0.6

    # ---- 15 位：旧身份证 -----------------------------------------------------
    if n == 15 and is_valid_id_card(clean):
        return EntityType.ID_CARD, 1.0

    # ---- 16-19 位：银行卡 ----------------------------------------------------
    if 16 <= n <= 19:
        if is_valid_luhn(clean):
            return EntityType.BANK_CARD, 1.0
        if clean.startswith("62"):
            # 银联卡开头，校验失败也保留低置信度上报
            return EntityType.BANK_CARD, 0.5
        return None

    # ---- 11 位：手机号 -------------------------------------------------------
    if n == 11 and is_plausible_phone(clean):
        return EntityType.PHONE, 0.95

    # ---- 10-12 位且 0 开头：固话（区号 2-3 位 + 号码 7-8 位） ----------------
    if 10 <= n <= 12 and clean.startswith("0"):
        return EntityType.PHONE, 0.7

    return None


class MultiRegexDetector:
    """规则检测器。``order=10``，最先跑：证件号优先级最高，先定下来。"""

    name = "regex"
    order = 10

    def __init__(self, settings: Settings) -> None:
        # 规则暂无需要外部配置的项；保留 settings 以便后续加开关。
        self.settings = settings

    # ------------------------------------------------------------------ 主流程

    def detect(self, block: TextBlock, ctx: MatchContext) -> Sequence[Entity]:
        """扫描一个文本块，返回不重叠的规则类实体。"""
        hits: list[Entity] = []
        try:
            text = block.text
            if not text:
                return hits
            masked = masked_ranges(text)
            # 顺序很关键：数字 token 必须先跑。
            # 身份证末位可以是 X，若先跑 USCC，18 位的「数字+X」会被 USCC 分支
            # 抢先占成 0.6 置信度的信用代码，真正的身份证从此看不到这一段。
            hits.extend(self._scan_numbers(text, masked))
            # USCC 只补「数字 token 切不出来」的那些（含字母的 18 位），
            # 并跳过已被身份证/银行卡认领的区间。
            claimed = masked + [hit.span for hit in hits]
            hits.extend(self._scan_uscc(text, claimed))
            hits.extend(self._scan_plates(text, masked))
            hits.extend(self._scan_emails(text, masked))
        except Exception:
            # 检测器绝不向上抛异常：engine 会吞掉，但吞掉等于丢召回。
            # 出错时至少返回已经收集到的结果。
            pass
        return self._dedupe(hits)

    # ------------------------------------------------------------------ USCC

    def _scan_uscc(self, text: str, masked: list[tuple[int, int]]) -> list[Entity]:
        """含字母的统一社会信用代码。

        校验通过 1.0；字符集与首位登记管理部门代码都像、但校验位不过时
        报 0.6——与身份证同一套「宁可多报」策略。
        """
        out: list[Entity] = []
        for match in _USCC_RE.finditer(text):
            start, end = match.span()
            if within(masked, start, end):
                continue
            raw = match.group(0)
            # 18 位「17 数字 + X」首先可能是身份证，校验通过就不再当信用代码——
            # USCC 的字符集恰好也含 X，两边会撞车。
            if is_valid_id_card(raw):
                continue
            if is_valid_uscc(raw):
                confidence = 1.0
            elif raw[:1].upper() in frozenset("123456789ANY"):
                confidence = 0.6
            else:
                continue
            out.append(
                Entity(
                    type=EntityType.USCC,
                    text=raw,
                    start=start,
                    end=end,
                    confidence=confidence,
                    detector=self.name,
                )
            )
        return out

    # ------------------------------------------------------------------ 数字

    def _scan_numbers(self, text: str, masked: list[tuple[int, int]]) -> list[Entity]:
        """切数字 token 并定性。"""
        out: list[Entity] = []
        for match in _DIGIT_TOKEN_RE.finditer(text):
            start, end = match.span()
            raw = match.group(0)

            # 身份证末位 X 紧跟 17 位数字时，把 X 纳入 token
            if end < len(text) and text[end] in "Xx" and match.group(0).isdigit():
                half_digits = _to_halfwidth(raw)
                if len(half_digits) == 17:
                    end += 1
                    raw = text[start:end]

            if not self._adjacent_ok(text, start, end):
                continue
            if within(masked, start, end):
                continue

            clean = _to_halfwidth(raw).replace("-", "")
            verdict = classify_number(clean)
            if verdict is None:
                continue
            etype, confidence = verdict
            out.append(
                Entity(
                    type=etype,
                    text=text[start:end],
                    start=start,
                    end=end,
                    confidence=confidence,
                    detector=self.name,
                )
            )
        return out

    @staticmethod
    def _adjacent_ok(text: str, start: int, end: int) -> bool:
        """数字 token 两侧不能紧贴**ASCII** 字母/数字或邮箱符号。

        注意必须写成 ``isascii() and isalnum()``：``str.isalnum()`` 对汉字也
        返回 True，只写 ``isalnum()`` 会把「身份证号330102…」这种中文紧邻、
        无标点的写法全部拒掉，实际召回为 0。
        """
        if start > 0:
            prev = text[start - 1]
            if (prev.isascii() and prev.isalnum()) or prev in _IDENTIFIER_CHARS:
                return False
        if end < len(text):
            nxt = text[end]
            # X 是身份证末位，已在 token 扩展阶段处理，这里视为可接受
            if nxt in "Xx":
                return True
            if (nxt.isascii() and nxt.isalnum()) or nxt in _IDENTIFIER_CHARS:
                return False
        return True

    # ------------------------------------------------------------------ 车牌

    def _scan_plates(self, text: str, masked: list[tuple[int, int]]) -> list[Entity]:
        """车牌：省份简称 + 发牌机关字母 + 5 位（普通）/ 6 位（新能源）。"""
        out: list[Entity] = []
        for match in _PLATE_RE.finditer(text):
            start, end = match.span()
            if within(masked, start, end):
                continue
            out.append(
                Entity(
                    type=EntityType.PLATE,
                    text=text[start:end],
                    start=start,
                    end=end,
                    confidence=0.9,
                    detector=self.name,
                )
            )
        return out

    # ------------------------------------------------------------------ 邮箱

    def _scan_emails(self, text: str, masked: list[tuple[int, int]]) -> list[Entity]:
        """邮箱。"""
        out: list[Entity] = []
        for match in _EMAIL_RE.finditer(text):
            start, end = match.span()
            if within(masked, start, end):
                continue
            out.append(
                Entity(
                    type=EntityType.EMAIL,
                    text=text[start:end],
                    start=start,
                    end=end,
                    confidence=0.95,
                    detector=self.name,
                )
            )
        return out

    # ------------------------------------------------------------------ 收尾

    @staticmethod
    def _dedupe(hits: list[Entity]) -> list[Entity]:
        """同一检测器内消解重叠：优先级高者先占位，再按位置输出。"""
        ordered = sorted(
            hits,
            key=lambda e: (-_PRIORITY.get(e.type, 0), -(e.end - e.start), -e.confidence),
        )
        kept: list[Entity] = []
        for cand in ordered:
            if any(cand.start < k.end and cand.end > k.start for k in kept):
                continue
            kept.append(cand)
        kept.sort(key=lambda e: e.start)
        return kept


def make(settings: Settings) -> Detector:
    """检测器工厂，供 ``detectors/__init__.py`` 自动装配。"""
    return MultiRegexDetector(settings)


__all__ = ["MultiRegexDetector", "classify_number", "make"]

"""机构名检测：公司、律所、医院、学校、银行、社会团体等。

难点与取舍
----------
1. **以「后缀定界、向前回溯」为核心**。中文机构名的结构高度规律：字号 +
   行业 + 组织形式后缀（``杭州某某科技有限公司``）。后缀是唯一稳定的锚点，
   所以先找后缀，再向左回溯到标点或连接词为止。
2. **左侧必须清洗**。回溯很容易把「原告张三与」一起吞进来。做法是：
   遇到「与 / 及 / 诉 / 向 / 在 / 年 / 号」等连接、谓语、量词字就停；
   停完再剥掉「原告 / 被告 / 法定代表人 / 甲方」这类角色前缀，
   并要求剩下的字号至少 2 字。
3. **字号太短的宁可不报**。「该医院」「本公司」「总公司」「分公司」这类
   指代性说法字号只有 1 字，不是实体，报出来只会白耗代号词池，一律拒绝。
4. **法院、检察院、政府、人大不归本层**。需求明确：这些主体由 place 层
   拆出地名并保留「人民法院」字样，本模块遇到这些词直接跳过，避免两层
   输出互相打架。
5. **已知局限**：机构名首个字恰好是连接字时会被截断（如「和记黄埔有限公司」
   只剩「记黄埔有限公司」，漏掉一个「和」）；括号内的地名/别名
   （``某某（杭州）科技有限公司``）会从括号处断开，只掩后半段。
"""

from __future__ import annotations

import re
from typing import Sequence

from ..config import Settings
from ..entity import Detector, Entity, EntityType, MatchContext, TextBlock
from .validators import is_cjk, masked_ranges, within

#: 机构后缀。匹配时按长度倒序，保证「有限责任公司」不会被「公司」抢先。
_ORG_SUFFIXES: tuple[str, ...] = (
    "股份有限公司",
    "有限责任公司",
    "律师事务所有限公司",
    "律师事务所",
    "会计师事务所",
    "专利代理事务所",
    "集团有限公司",
    "有限公司",
    "股份公司",
    "分公司",
    "总公司",
    "人民医院",
    "中医院",
    "卫生院",
    "研究院",
    "研究所",
    "设计院",
    "幼儿园",
    "合作社",
    "基金会",
    "公司",
    "医院",
    "银行",
    "支行",
    "分行",
    "大学",
    "学院",
    "中学",
    "小学",
    "学校",
    "工厂",
    "超市",
    "协会",
    "商会",
    "中心",
    "厂",
    "店",
)

#: 向左回溯时遇到这些字就停：连接词、谓语、量词、编号词。
#: 它们不会出现在机构名内部，却能可靠地把「原告张三与」挡在外面。
_ORG_STOP_CHARS: frozenset[str] = frozenset(
    # 「有」不能进停字表：它出现在「有限公司 / 股份有限公司」内部，
    # 一停就会把银行全称截成「限公司杭州分行」。
    "与及和或等系为是的诉向在对因由从自至经按据依称将把让使未无已曾"
    # 「被 / 第」不列进来：它们常是「被告 / 第三人」的首字，
    # 一停就会把机构名切出「告杭州…」「三人杭州…」，改由前缀剥离兜住。
    "均亦而且但则们年月日号条款项元"
)

#: 回溯之后仍可能粘在左侧的角色词，按长度倒序剥离。
_NOISE_PREFIXES: tuple[str, ...] = tuple(
    sorted(
        (
            "委托诉讼代理人", "法定代表人", "主要负责人", "申请执行人",
            "被上诉人", "被申请人", "被执行人", "上诉人", "第三人",
            "申请人", "负责人", "代理人", "辩护人", "原告", "被告",
            "该公司", "本公司", "贵公司", "本单位", "甲方", "乙方", "丙方",
            "以及", "及", "与", "和", "或", "向", "在", "对", "因", "于",
            "由", "等", "的", "系", "为", "是", "诉", "告",
            # 判决主文里机构名左侧高频出现的完整动词 / 连词：
            # 停字表按单字切会把「判决」切成「决」，所以按词剥离
            "判决", "判令", "裁定", "责令", "依照", "依据", "根据", "按照",
            "请求", "要求", "诉请", "主张", "认为", "判", "令",
        ),
        key=len,
        reverse=True,
    )
)

#: 角色词残尾 / 指代首字：丢掉一个字后专名仍 ≥2 才丢。
#: 不含「人」——人保、人民电器、人民银行都是真机构名。
_LEADING_NOISE: frozenset[str] = frozenset("告司单该本原被申上下及和与等")

#: 字号首字是这些时，多数是指代或泛指，不是真实体。
_BAD_STEM_FIRST: frozenset[str] = frozenset("本该各此其全每我你他她它哪这那贵上下")

#: 法院 / 检察院 / 政府 / 人大 体系不归本层处理（由 place 层拆地名并保留字样）
_COURT_GOV_TERMS: tuple[str, ...] = (
    "人民法院",
    "人民检察院",
    "人民政府",
    "人民代表大会",
    "人大常委会",
    "人民政协",
)

#: 泛指机构，不是可识别实体，报出来只会消耗代号词池。
_GENERIC_ORGS: frozenset[str] = frozenset(
    {
        "服务中心", "技术中心", "培训中心", "会议中心", "管理中心", "信息中心",
        "研发中心", "检测中心", "调解中心", "接待中心", "办事中心", "总公司",
        "分公司", "子公司", "母公司", "本公司", "该医院", "人民医院", "中医院",
        "卫生院", "有限公司", "股份公司", "有限责任公司", "股份有限公司",
        "协会", "商会", "基金会", "合作社", "设计院", "研究院", "研究所",
        "大学", "学院", "中学", "小学", "学校", "幼儿园", "工厂", "超市",
        "公司", "医院", "银行", "支行", "分行", "厂", "店", "中心",
    }
)

#: 机构名内部不会出现的标点，回溯到此即止
_STOP_RE = re.compile(r"[，。、；：！？（）【】《》〈〉「」『』\s,.;:!?()\[\]{}<>\"'—－/\\]")


class OrgDetector:
    """机构名检测器。``order=30``。"""

    name = "org"
    order = 30

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._suffixes = tuple(sorted(_ORG_SUFFIXES, key=len, reverse=True))

    # ------------------------------------------------------------------ 主流程

    def detect(self, block: TextBlock, ctx: MatchContext) -> Sequence[Entity]:
        """返回本块内的机构实体，不重叠、偏移在界内、绝不抛异常。"""
        hits: list[Entity] = []
        try:
            text = block.text
            if not text:
                return hits
            masked = masked_ranges(text)
            hits = self._scan(text, masked)
        except Exception:
            # 检测器绝不向上抛异常：丢召回比丢一条异常更危险
            pass
        return self._dedupe(hits)

    # ------------------------------------------------------------------ 扫描

    def _scan(self, text: str, masked: list[tuple[int, int]]) -> list[Entity]:
        out: list[Entity] = []
        claimed_end: set[int] = set()

        for suffix in self._suffixes:
            cursor = 0
            while True:
                idx = text.find(suffix, cursor)
                if idx < 0:
                    break
                cursor = idx + 1
                end = idx + len(suffix)
                # 同一位置只保留最长后缀（有限责任公司 > 有限公司 > 公司）
                if end in claimed_end:
                    continue
                claimed_end.add(end)

                start = self._walk_back(text, idx)
                start = self._strip_noise(text, start, end, len(suffix))
                # 角色词被停字切开后残留的首字（被告 → 告），
                # 丢掉一个字仍够「专名 ≥2」才丢
                while (
                    len(text[start:end]) - len(suffix) > 2
                    and text[start : start + 1] in _LEADING_NOISE
                ):
                    start += 1
                cand = text[start:end]
                if not self._accept(cand, suffix, start, end, masked):
                    continue
                out.append(
                    Entity(
                        type=EntityType.ORG,
                        text=cand,
                        start=start,
                        end=end,
                        confidence=0.9,
                        detector=self.name,
                    )
                )
        return out

    def _walk_back(self, text: str, suffix_start: int) -> int:
        """从后缀起点向左回溯，直到标点、连接词或长度上限。"""
        start = suffix_start
        while start > 0:
            prev = text[start - 1]
            if prev in _ORG_STOP_CHARS:
                break
            if not (is_cjk(prev) or (prev.isascii() and prev.isalnum())):
                break
            if suffix_start - start >= 30:
                break
            start -= 1
        return start

    @staticmethod
    def _strip_noise(text: str, start: int, end: int, suffix_len: int) -> int:
        """剥掉回溯时粘在左侧的角色词 / 连接词。"""
        for _ in range(4):
            chunk = text[start:end]
            for prefix in _NOISE_PREFIXES:
                if chunk.startswith(prefix) and len(chunk) - len(prefix) >= suffix_len + 2:
                    start += len(prefix)
                    break
            else:
                return start
            if start >= end:
                return start
        return start

    @staticmethod
    def _accept(
        cand: str, suffix: str, start: int, end: int, masked: list[tuple[int, int]]
    ) -> bool:
        """候选机构名的最终校验。"""
        if not (2 <= len(cand) <= 30):
            return False
        stem = cand[: len(cand) - len(suffix)]
        # 字号至少 2 字：「该医院」「本公司」「总公司」不是实体
        if len(stem) < 2:
            return False
        if not all(is_cjk(ch) or (ch.isascii() and ch.isalnum()) for ch in stem):
            return False
        if stem[0] in _BAD_STEM_FIRST:
            return False
        if cand in _GENERIC_ORGS:
            return False
        if any(term in cand for term in _COURT_GOV_TERMS):
            return False
        if within(masked, start, end):
            return False
        return True

    # ------------------------------------------------------------------ 收尾

    @staticmethod
    def _dedupe(hits: list[Entity]) -> list[Entity]:
        """内部消解重叠：长实体优先（「浙江某某律师事务所」胜过「某某律师事务所」）。"""
        ordered = sorted(hits, key=lambda e: (-(e.end - e.start), e.start))
        kept: list[Entity] = []
        for cand in ordered:
            if any(cand.start < k.end and cand.end > k.start for k in kept):
                continue
            kept.append(cand)
        kept.sort(key=lambda e: e.start)
        return kept


def make(settings: Settings) -> Detector:
    """检测器工厂，供 ``detectors/__init__.py`` 自动装配。"""
    return OrgDetector(settings)


__all__ = ["OrgDetector", "make"]

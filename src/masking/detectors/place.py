"""地名检测：行政区划、道路，以及「不带后缀的省名 / 市名」。

难点与取舍
----------
1. **必须按后缀切分，不能按固定长度滑窗**。``杭州市西湖区文三路123号``
   难点不在「认出地名」，而在把三个地名**切开**：切错就得到「市西湖」「区文三」。
   本模块的做法是「后缀定界 + 向前回溯」：找到 ``市 / 区 / 路`` 等后缀后，
   向左最多取 2-4 字作为专名，回溯途中一遇到别的行政区划后缀就停——
   这样「区」绝不会跨到「市」前面去。
2. **专名长度下限 2 字**。这一条挡掉了大量误报：``城市 / 都市 / 地区 / 小区 /
   辖区 / 道路 / 铁路 / 马路`` 的专名都只有 1 字，自然被排除。
3. **「XX区」绝大多数不是地名**。真实案例里出现过把「用户反复纠结校区」
   脱成「用户反复【建木】区」、「直接推荐区位更好」脱成「直接【汤谷】区位更好」——
   这类误脱比漏脱更糟：它把正常语义改写成假地名，下游分析会直接读错。
   因此按 ``rules.yaml`` 的 ``place_zone_blocker_before / after`` 做双向约束：
   以「区」结尾时，紧邻「区」的前字命中 before 表、或后字命中 after 表，
   一律不算地名。同一约束也套在「镇 / 乡 / 街道」上（「风情小镇」同理）。
4. **省名市名常常不带后缀**。「云南物理类」「报考云南的哪些学校」
   「杭州互联网法院」里的「云南」「杭州」都不是 ``云南省`` / ``杭州市`` 写法。
   只靠后缀会大面积漏检，因此读取 ``lexicon.yaml`` 的
   ``pools.province / pools.city`` 做词表匹配，不带后缀也识别。
   词表命中后若紧跟层级后缀（省/市/区/县…），让后缀扫描负责，
   避免同一实体既有「云南」又有「云南省」两种切法。
5. **法院 / 检察院不整体输出**。``杭州市西湖区人民法院`` 只切出
   「杭州市」「西湖区」，「人民法院」字样原样保留；``杭州互联网法院``
   由城市词池切出「杭州」，「互联网法院」保留。
   铁路运输、知识产权、金融、海事、军事、林区、农垦等专门法院同理：
   地名交给地名层，「XX法院」的类型后缀不动。
6. 保留后缀（``市 / 区 / 路``），分配器会把它拼回代号后面，例如
   ``杭州市 → 【昆仑】市``，读者仍能看出层级。
"""

from __future__ import annotations

import re
from typing import Sequence

from ..config import Settings
from ..entity import Detector, Entity, EntityType, MatchContext, TextBlock
from .validators import is_cjk, masked_ranges, within

#: 后缀 → 专名最大长度。
#: 「自治区 / 特别行政区」的专名可以长到 4-6 字（新疆维吾尔、内蒙古），
#: 普通「市 / 区」压到 3-4 字，避免把前一个地名吃进来。
_SUFFIX_MAX_STEM: dict[str, int] = {
    "产业园区": 6,
    "特别行政区": 6,
    "开发区": 6,
    "高新区": 6,
    "经开区": 6,
    "工业区": 6,
    "保税区": 6,
    "自贸区": 6,
    "自治区": 5,
    "自治州": 4,
    "自治县": 4,
    "街道": 3,
    "大道": 4,
    "胡同": 3,
    "新区": 4,
    "省": 4,
    "市": 4,
    "区": 3,
    "县": 3,
    "镇": 3,
    "乡": 3,
    "路": 4,
    "街": 3,
    "巷": 3,
}

#: 回溯时视为边界、不得跨越的字符。注意**刻意不含「州」**：#: 广州、杭州、苏州的专名里都有「州」字，把它当边界会让这些城市切不出来。
#: 「地」在表里：法律文书常写「被告住所地杭州市」，不停会粘出「所地杭州」。
_STEM_BOUNDARY: frozenset[str] = frozenset("省市区县镇乡街巷道路号村地")

#: 明显不是地名的切片。真实材料里 place 检测器在超长段落上偶尔会切出
#: 「被诉侵权商品上」「牛奶上」「权商品上」「品实际上」这类片段，
#: 一旦替换就破坏原文语义（比漏检更糟——「商品上」变【堂庭】，
#: 下游读到的句子直接不通）。输出前统一拦掉。
_BOGUS_PLACE_SUBSTR: tuple[str, ...] = (
    "商品", "侵权", "牛奶", "实际上", "公司", "有限", "对方", "被诉",
    "销售", "注册", "包装", "标识", "字样", "商标", "产品", "服务",
)

#: 以这些字结尾的「地名」几乎都是切片碎片
_BOGUS_PLACE_TAIL: frozenset[str] = frozenset("上的了是在和与或及等对为以于们这那他她它")


def _is_bogus_place(text: str) -> bool:
    """地名候选是否明显是切片碎片。"""
    if not text:
        return True
    if any(word in text for word in _BOGUS_PLACE_SUBSTR):
        return True
    return text[-1] in _BOGUS_PLACE_TAIL



#: 回溯时容易误粘进来的动词/介词/住址类首字。
#: 处理方式不是「一票否决」而是「整字丢弃」：``住杭州市`` → ``杭州市``；
#: 但只有在丢掉后专名仍有 ≥2 字时才丢，否则 ``达州市 / 迁安市 / 于都县``
#: 这些「首字恰好是动词义」的真地名会被误伤。
_VERB_PREFIX: frozenset[str] = frozenset(
    "住所地在于至往从向经由到赴迁移送达址居籍贯现原曾系被位"
    # 法言法语里常直接粘在地名前的谓语/连接字：
    # 「上诉于浙江省」「向杭州市」「由杭州市」——不停会把「诉于浙江」当地名。
    # 只丢 3 字以上专名的首字，2 字专名（达州市 / 于都县 / 和县）不受影响。
    "诉请令判决定查明称对与及和或等的将被由从自向在经按据依因至到是为"
    "有无未已曾并且而则共合只仅"
)

#: 专名首字是这些时多半是指代或泛指，不是真实地名。
_BAD_STEM_FIRST: frozenset[str] = frozenset(
    # 只放指代词。「上/下/新/大/小」不能放：上海、新城区、大连、小汤山
    # 都是真地名，放进去会直接打断「上海市」「新城区」的专名。
    "本该各此其全每我你他她它哪这那某"
)

#: 整词泛指，不是可识别实体（否则会白耗地名代号）。
_PLACE_STOP: frozenset[str] = frozenset(
    {
        "地区", "城区", "社区", "小区", "园区", "景区", "辖区", "郊区", "市区",
        "矿区", "林区", "战区", "灾区", "库区", "港区", "片区", "湾区", "厂区",
        "院区", "镇区", "乡区", "县区", "省区", "地区性", "公路", "铁路", "马路",
        "道路", "山路", "水路", "电路", "线路", "出路", "思路", "套路", "环路",
        "高架路", "快速路", "高速公路", "商业街", "步行街", "小吃街",
        "开发区", "高新区", "工业区", "商业区", "住宅区", "行政区", "功能区",
        "示范区", "试验区", "保税区", "经济区", "旅游区", "风景区", "市中心",
        "县城", "城镇", "乡镇", "村庄",
    }
)

#: 适用「区划词块约束」的后缀：这些后缀前后常跟普通语素，需要双向校验。
_ZONE_SUFFIXES: frozenset[str] = frozenset({"区", "镇", "乡", "街道"})

#: 「区」词块约束的例外：这些是真实行政区划，虽然「区」前面是「城」，
#: 但 blocker 里的「城」本意是挡「老城区 / 主城区」这类泛指。
_ZONE_EXEMPT: frozenset[str] = frozenset({"东城区", "西城区", "新城区"})

#: 后缀右邻禁用字：后缀其实是普通词的一部分时，不能算地名。
#: 「投入市场」「同行业市场」曾切出「投入市」「行业市」；
#: 「务实路径」曾切出「务实路」。
_SUFFIX_NEXT_BLOCK: dict[str, frozenset[str]] = {
    "市": frozenset("场"),
    "路": frozenset("径"),
}

#: 专名末字是这些时，说明专名其实是动词短语（「需参加全」省）。
_BAD_STEM_LAST: frozenset[str] = frozenset("全各本该多条")

#: 层级后缀字符：省名/市名词表命中后若紧跟这些字，交给后缀扫描
#: （保留后缀，避免同一实体出现「云南」「云南省」两种切法）。
_HIERARCHY_CHARS: frozenset[str] = frozenset("省市区县镇乡州盟旗")


#: 这些后缀要求专名里含已知省名（「X省」几乎只有省级区划一种写法）
_PROVINCE_REQUIRED_SUFFIXES: frozenset[str] = frozenset({"省"})


class PlaceDetector:
    """地名检测器。``order=60``。"""

    name = "place"
    order = 60

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # 长后缀优先：自治区 > 区，特别行政区 > 区
        self._suffixes = tuple(sorted(_SUFFIX_MAX_STEM, key=len, reverse=True))

        detect = getattr(settings.rules, "detect", {}) or {}
        self._zone_before: frozenset[str] = frozenset(
            detect.get("place_zone_blocker_before") or ""
        )
        self._zone_after: frozenset[str] = frozenset(
            detect.get("place_zone_blocker_after") or ""
        )

        # 省市词池：不带后缀也要识别。长词优先（呼和浩特 > 呼和）。
        pools = getattr(settings.lexicon, "pools", {}) or {}
        self._province: tuple[str, ...] = tuple(
            sorted({w for w in (pools.get("province") or []) if w}, key=len, reverse=True)
        )
        self._city: tuple[str, ...] = tuple(
            sorted({w for w in (pools.get("city") or []) if w}, key=len, reverse=True)
        )
        # 区/镇级补充池：全国区县约 2900 个、乡镇 4 万个，无法穷举。
        # 这里只收低误伤风险的词；具体案件的地名由用户往 lexicon 或
        # config/custom_terms.txt 里补。
        self._district: tuple[str, ...] = tuple(
            sorted({w for w in (pools.get("district") or []) if w}, key=len, reverse=True)
        )
        words = sorted({*self._province, *self._city, *self._district}, key=len, reverse=True)
        self._pool_ordered: tuple[str, ...] = tuple(words)
        self._pool_re: re.Pattern[str] | None = (
            re.compile("|".join(re.escape(w) for w in words)) if words else None
        )
        self._pool_words: frozenset[str] = frozenset(words)

    # ------------------------------------------------------------------ 主流程

    def detect(self, block: TextBlock, ctx: MatchContext) -> Sequence[Entity]:
        """返回本块内的地名实体，不重叠、偏移在界内、绝不抛异常。"""
        hits: list[Entity] = []
        try:
            text = block.text
            if not text:
                return hits
            masked = masked_ranges(text)
            hits.extend(self._scan_suffixes(text, masked))
            hits.extend(self._scan_pools(text, masked))
        except Exception:
            # 检测器绝不向上抛异常：丢召回比丢一条异常更危险
            pass
        return [h for h in self._dedupe(hits) if not _is_bogus_place(h.text)]

    # ------------------------------------------------------------------ 后缀扫描

    def _scan_suffixes(self, text: str, masked: list[tuple[int, int]]) -> list[Entity]:
        """「XX市 / XX区 / XX路」：后缀定界 + 向前回溯。"""
        out: list[Entity] = []
        claimed_end: dict[int, int] = {}

        for suffix in self._suffixes:
            cursor = 0
            while True:
                idx = text.find(suffix, cursor)
                if idx < 0:
                    break
                cursor = idx + 1
                end = idx + len(suffix)
                prev_len = claimed_end.get(end, 0)
                if prev_len >= len(suffix):
                    continue
                claimed_end[end] = len(suffix)

                max_stem = _SUFFIX_MAX_STEM[suffix]
                start = self._walk_back(text, idx, max_stem)
                # 词池锚定：专名末尾若是「云南 / 昆明 / 杭州」这类已知地名，
                # 直接裁到该词起点。「放在云南省」「下在云南省」里的动词
                # 不必逐个枚举，靠锚定一次性解决。
                anchored = self._pool_anchor(text[start:idx])
                if anchored is not None:
                    start = idx - len(anchored)
                # 剥掉误粘的动词/介词首字（住杭州市 → 杭州市）
                while idx - start >= 3 and text[start : start + 1] in _VERB_PREFIX:
                    start += 1
                cand = text[start:end]
                stem = cand[: len(cand) - len(suffix)]
                if not self._accept(cand, stem, suffix, start, end, text, masked):
                    continue
                out.append(
                    Entity(
                        type=EntityType.PLACE,
                        text=cand,
                        start=start,
                        end=end,
                        confidence=0.8,
                        detector=self.name,
                    )
                )
        return out

    @staticmethod
    def _walk_back(text: str, suffix_start: int, max_stem: int) -> int:
        """从后缀起点向左取专名，遇到别的行政区划后缀或标点即停。"""
        start = suffix_start
        while start > 0 and suffix_start - start < max_stem:
            prev = text[start - 1]
            if not is_cjk(prev):
                break
            if prev in _STEM_BOUNDARY:
                break
            start -= 1
        return start

    def _accept(
        self,
        cand: str,
        stem: str,
        suffix: str,
        start: int,
        end: int,
        text: str,
        masked: list[tuple[int, int]],
    ) -> bool:
        """候选地名是否成立。"""
        if len(stem) < 2:
            return False
        if not all(is_cjk(ch) for ch in stem):
            return False
        if stem[0] in _BAD_STEM_FIRST:
            return False
        if cand in _PLACE_STOP:
            return False
        if stem[-1] in _BAD_STEM_LAST:
            return False
        blocked = _SUFFIX_NEXT_BLOCK.get(suffix)
        if blocked and end < len(text) and text[end] in blocked:
            return False
        # 「X省」的专名必须是已知省名：所有省级区划都在词池里，
        # 命中不了就说明「省」只是普通词的一部分（全省 / 各省 / 本省）。
        if suffix in _PROVINCE_REQUIRED_SUFFIXES and self._province_anchor(stem) is None:
            return False
        if suffix in _ZONE_SUFFIXES and cand not in _ZONE_EXEMPT:
            # 「区」类词块双向约束：纠结校区 / 推荐区位 / 风情小镇 / 商城街道
            before_index = len(cand) - len(suffix) - 1
            if before_index >= 0 and cand[before_index] in self._zone_before:
                return False
            if end < len(text) and text[end] in self._zone_after:
                return False
        if within(masked, start, end):
            return False
        return True

    def _pool_anchor(self, stem: str) -> str | None:
        """专名末尾若是已知省市名，返回该地名（长词优先）。"""
        for word in self._pool_ordered:
            if len(word) < len(stem) and stem.endswith(word):
                return word
        return None

    def _province_anchor(self, stem: str) -> str | None:
        """专名里是否含已知省名（开头或结尾，长词优先）。"""
        for word in self._province:
            if stem.startswith(word) or stem.endswith(word):
                return word
        return None

    # ------------------------------------------------------------------ 省市词池

    def _scan_pools(self, text: str, masked: list[tuple[int, int]]) -> list[Entity]:
        """不带后缀的省名 / 市名（云南、昆明、杭州……）。

        词表命中后若紧跟层级后缀，交给后缀扫描：这样「云南省」只产出一次，
        且保留「省」字，不会与「云南」形成两种切法。
        """
        out: list[Entity] = []
        if self._pool_re is None:
            return out
        for match in self._pool_re.finditer(text):
            start, end = match.span()
            if within(masked, start, end):
                continue
            if end < len(text) and text[end] in _HIERARCHY_CHARS:
                continue
            out.append(
                Entity(
                    type=EntityType.PLACE,
                    text=match.group(0),
                    start=start,
                    end=end,
                    confidence=0.85,
                    detector=self.name,
                )
            )
        return out

    # ------------------------------------------------------------------ 收尾

    @staticmethod
    def _dedupe(hits: list[Entity]) -> list[Entity]:
        """内部消解重叠：长实体优先（「内蒙古自治区」胜过「古自治区」这类杂片）。"""
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
    return PlaceDetector(settings)


__all__ = ["PlaceDetector", "make"]

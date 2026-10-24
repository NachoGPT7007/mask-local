"""代号分配器：把命中的原文映射成带框代号。

设计要点
--------
1. 两阶段。先 ``prepare(entities)`` 统计全文出现的不同原值，再 ``placeholder_for``
   取代号。这样掩码类（证件号等）才能决定到底用 ``XXX`` 还是 ``XXX1/XXX2``：
   一个案件里只有一个手机号就用 ``XXX``，有多个才编号，不啰嗦也不混淆。
2. 一致性。映射键是 ``(类型, 原文)``，同一人/同一公司全篇同一代号。
3. 不复用。词池按顺序取，用尽抛 ``PoolExhausted``，绝不回头循环。
4. 后缀保留。地名保留「市/区/路」，机构保留「公司/律师事务所」，
   法律文书里的层级与主体性质在脱敏后仍然可读。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .config import Lexicon, Rules
from .entity import Entity, EntityType


class PoolExhausted(RuntimeError):
    """词池用尽。宁可报错，也不复用代号。"""


#: 实体类型 → 对应的词池名。续跑恢复时要按类型把已用词登记回正确的池子。
_POOL_BY_TYPE: dict[str, str] = {
    EntityType.PERSON.value: "person",
    EntityType.PLACE.value: "place",
    EntityType.ADDRESS.value: "place",
    EntityType.ORG.value: "org",
}

#: 从「【熊猫】」「【白桦】公司」「【昆仑】市」里取出池词
_PLACEHOLDER_WORD_RE = re.compile(r"【([^】]*)】")


def _placeholder_word(placeholder: str) -> str | None:
    """代号里【】包着的那部分就是词池词。掩码 XXX1 返回 None。"""
    m = _PLACEHOLDER_WORD_RE.search(placeholder or "")
    return m.group(1) if m else None


#: 案号：(2024)甲法民初1234号 / （2024）浙01民终5号 / 〔2024〕京0105执123号 / [2024]沪01民初1号
#: 括号形态必须与 detectors/case_no.py 保持一致，否则检测到了却解析不出来
CASE_NO_RE = re.compile(
    r"[\(（\[〔]\s*(?P<year>\d{4})\s*[\)）\]〕]\s*"
    r"(?P<court>[\u4e00-\u9fa5]{1,3}\d{0,4})\s*"
    r"(?P<kind>[\u4e00-\u9fa5]{1,3})\s*"
    # 序号也接受 X/×/＊：原文可能自己把序号隐去了（「民初XX号」）
    r"(?P<serial>[\dXx×＊*]+)\s*号"
)

#: 机构类型判定：长后缀优先，避免「有限公司」被「公司」抢先匹配
ORG_KINDS: tuple[tuple[str, str], ...] = (
    ("律师事务所有限公司", "firm"),
    ("律师事务所", "firm"),
    ("会计师事务所", "firm"),
    ("专利代理事务所", "firm"),
    ("有限责任公司", "company"),
    ("股份有限公司", "company"),
    ("有限公司", "company"),
    ("研究院", "research"),
    ("研究所", "research"),
    ("设计院", "research"),
    ("银行", "bank"),
    ("支行", "bank"),
    ("分行", "bank"),
    ("医院", "hospital"),
    ("卫生院", "hospital"),
    ("大学", "school"),
    ("学院", "school"),
    ("中学", "school"),
    ("小学", "school"),
    ("学校", "school"),
    ("幼儿园", "school"),
    ("协会", "ngo"),
    ("商会", "ngo"),
    ("基金会", "ngo"),
    ("合作社", "company"),
    ("公司", "company"),
    ("工厂", "factory"),
    ("厂", "factory"),
    ("商店", "shop"),
    ("超市", "shop"),
    ("门店", "shop"),
    ("店", "shop"),
)


@dataclass
class MappingEntry:
    type: str
    original: str
    placeholder: str
    count: int = 0

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "original": self.original,
            "placeholder": self.placeholder,
            "count": self.count,
        }


@dataclass
class PlaceholderAllocator:
    lexicon: Lexicon
    rules: Rules
    scope: str = ""

    _pool_cursor: dict[str, int] = field(default_factory=dict)
    #: 各词池里已被占用的词。续跑（多次脱密）时从已有映射表恢复，
    #: 保证新实体不会拿到上一轮已经发出去的代号。
    _pool_used: dict[str, set[str]] = field(default_factory=dict)
    _by_original: dict[tuple[str, str], str] = field(default_factory=dict)
    _entries: dict[tuple[str, str], MappingEntry] = field(default_factory=dict)
    #: 掩码全局唯一编号。键含类型，但序号跨类型连续递增——
    #: 若按类型各自从 1 编号，身份证 XXX1 会与手机 XXX1 撞号，
    #: 回填时「孤立出现的 XXX1」就无法判定是哪一个，这是不可接受的。
    _mask_index: dict[tuple[str, str], int] = field(default_factory=dict)
    #: 掩码序号上限。续跑时从既有映射恢复，新编号从这个值往后加，
    #: 保证不会与上一轮的 XXX1/XXX2 重号。
    _mask_seq: int = 0
    _case_no_index: int = 0
    _prepared: bool = False

    # ------------------------------------------------------------------ 阶段一

    def prepare(self, entities: Iterable[Entity]) -> None:
        """预扫描：目前只做统计占位。

        掩码编号不依赖「本批出现几个不同值」——因为流水线是逐文件调用本方法的，
        若按批统计，第二个文件会把第一个文件已发出的代号口径改掉。
        故改为全局唯一、按需递增，跨文件天然一致。
        """
        _ = entities
        self._prepared = True

    # ------------------------------------------------------------------ 阶段二

    def placeholder_for(self, entity: Entity) -> str:
        """返回该实体应替换成的文本。policy=keep 时原样返回。

        映射键对 PLACE 使用「去后缀的专名」，于是「云南省」与「云南」共享
        同一个代号——否则同一地点在文里会出现两个代号，法律分析会把一个
        地方读成两个。回填时映射表里存的是专名，后缀留在正文中，不会重复。
        """
        policy = self.rules.policy_for(entity.type)
        if policy == "keep":
            return entity.text

        key, original = self._identity(entity)
        if key in self._by_original:
            self._entries[key].count += 1
            return self._render(entity, self._by_original[key])

        base = self._allocate(entity, policy)
        self._by_original[key] = base
        self._entries[key] = MappingEntry(
            type=entity.type.value,
            original=original,
            placeholder=base,
            count=1,
        )
        return self._render(entity, base)

    def _identity(self, entity: Entity) -> tuple[tuple[str, str], str]:
        """返回（映射键, 映射表记录的原文）。

        PLACE 用去掉后缀的专名作键，于是「云南省」与「云南」共享代号。
        CASE_NO 用「年份|院代字|案件类型」作键，忽略序号——同一份文书里
        案号可能一处写全（民初18143号）、一处被原文自己隐去序号（民初XX号），
        若按完整文本做键会分配两个代号，读者会以为是两个案子。
        """
        if entity.type is EntityType.PLACE:
            stem, _ = self._split_place(entity.text)
            if stem:
                return (entity.type.value, stem), stem
        if entity.type is EntityType.CASE_NO:
            m = CASE_NO_RE.search(entity.text)
            if m:
                prefix = f"{m.group('year')}|{m.group('court')}|{m.group('kind')}"
                return (entity.type.value, prefix), entity.text
        return (entity.type.value, entity.text), entity.text

    def _render(self, entity: Entity, base: str) -> str:
        """基础代号 → 实际替换文本。PLACE 需补回原后缀。"""
        if entity.type is EntityType.PLACE:
            _, suffix = self._split_place(entity.text)
            return base + suffix
        return base

    def _allocate(self, entity: Entity, policy: str) -> str:
        if policy == "code_word":
            return self._code_word(entity)
        if policy == "case_no":
            return self._case_no(entity)
        return self._mask(entity)

    # ------------------------------------------------------------------ 具体策略

    def _next_word(self, pool_name: str) -> str:
        """取一个尚未被占用的词。

        续跑时词池里可能有词已经被上一轮发出去（「张三」已占【熊猫】），
        必须跳过它们，否则同一个词会对应两个主体。用「已用集合」而不是
        简单游标，正是为了让从映射表恢复的场景也能正确接着往下发。
        """
        pool = self.lexicon.pool(pool_name)
        used = self._pool_used.setdefault(pool_name, set())
        for word in pool:
            if word in used:
                continue
            used.add(word)
            return word
        raise PoolExhausted(
            f"词池 {pool_name} 已用尽（{len(pool)} 个，其中 {len(used)} 个已被占用）。"
            f"请在 config/lexicon.yaml 的 pools.{pool_name} 下追加词条后重跑。"
        )

    # ------------------------------------------------------------------ 续跑

    @classmethod
    def from_entries(
        cls, lexicon: Lexicon, rules: Rules, entries: Iterable[MappingEntry], scope: str = ""
    ) -> "PlaceholderAllocator":
        """从已有映射表恢复分配器，用于多次脱密（续跑）。

        恢复三样东西：
          - 原文 → 代号 的映射（同一原文直接返回原代号，绝不重新分配）
          - 已占用的池词（新实体从剩余词里取，不会撞车）
          - 掩码编号上限（XXX1/XXX2 接着往下编）
        """
        alloc = cls(lexicon, rules, scope)
        alloc.seed(entries)
        return alloc

    def seed(self, entries: Iterable[MappingEntry]) -> None:
        """把既有映射灌进分配器。"""
        for entry in entries:
            key = (entry.type, entry.original)
            self._by_original[key] = entry.placeholder
            self._entries[key] = entry

            word = _placeholder_word(entry.placeholder)
            if word:
                pool_name = _POOL_BY_TYPE.get(entry.type)
                if pool_name:
                    self._pool_used.setdefault(pool_name, set()).add(word)

            # 掩码编号：XXX7 → 至少从 8 开始
            if entry.placeholder.startswith(self.lexicon.mask_token):
                tail = entry.placeholder[len(self.lexicon.mask_token):]
                if tail.isdigit():
                    self._mask_index[key] = int(tail)

        # 掩码序号可能有空洞（XXX1、XXX5），按最大值续编，避免重号
        if self._mask_index:
            self._mask_seq = max(self._mask_index.values())
        self._prepared = True

    def _code_word(self, entity: Entity) -> str:
        # 注意：PLACE 返回的 base 不含后缀，由 _render 补回；
        # ORG / PERSON 返回的就是最终文本。
        if entity.type is EntityType.PERSON:
            return self.lexicon.wrap(self._next_word("person"))

        if entity.type is EntityType.PLACE:
            return self.lexicon.wrap(self._next_word("place"))

        if entity.type is EntityType.ORG:
            kind = self._org_kind(entity.text)
            suffix = self.lexicon.org_suffix.get(kind, "单位")
            return f"{self.lexicon.wrap(self._next_word('org'))}{suffix}"

        if entity.type is EntityType.ADDRESS:
            return self.lexicon.wrap(self._next_word("place"))

        return self.lexicon.wrap(self._next_word("person"))

    def _mask(self, entity: Entity) -> str:
        """掩码：XXX1、XXX2… 全局唯一，保证回填可精确到具体原值。

        只有一个掩码实体时也是 XXX1 而非 XXX——一致性优先于好看：
        跨文件处理时，第一个文件只有手机号、第二个文件才有多个值，
        若单值不编号就会前后口径不一致。
        """
        token = self.lexicon.mask_token
        if not self.lexicon.mask_enumerate:
            return token
        key = (entity.type.value, entity.text)
        if key not in self._mask_index:
            # 用独立计数器而非 len()+1：续跑时既有编号可能有空洞，
            # 按 len 续编会与已有编号撞号。
            self._mask_seq += 1
            self._mask_index[key] = self._mask_seq
        return f"{token}{self._mask_index[key]}"

    def _case_no(self, entity: Entity) -> str:
        cfg = self.rules.case_no
        m = CASE_NO_RE.search(entity.text)
        if not m:
            return f"({self._mask(entity)})"
        self._case_no_index += 1
        year = m.group("year") if cfg.get("keep_year", True) else "2024"
        kind = m.group("kind") if cfg.get("keep_case_type", True) else "民初"
        court = cfg.get("court_code", "甲法")
        serial = self._case_no_index if cfg.get("renumber", True) else m.group("serial")
        return f"({year}){court}{kind}{serial}号"

    # ------------------------------------------------------------------ 工具

    def _split_place(self, text: str) -> tuple[str, str]:
        """把「杭州市」拆成（杭州, 市）。后缀信息保留，层级不丢。

        专名部分至少要有 2 个字，否则裸市名会被后缀吃掉单字：
        「杭州」不能拆成（杭, 州），否则输出变成「【昆仑】州」。
        同一问题影响广州/苏州/福州/郑州/兰州等全部「州」结尾的城市。
        """
        for suffix in sorted(self.lexicon.place_suffix, key=len, reverse=True):
            if text.endswith(suffix) and len(text) - len(suffix) >= 2:
                return text[: -len(suffix)], suffix
        return text, ""

    def _org_kind(self, text: str) -> str:
        for suffix, kind in ORG_KINDS:
            if text.endswith(suffix):
                return kind
        return "other"

    # ------------------------------------------------------------------ 导出

    @property
    def entries(self) -> list[MappingEntry]:
        return list(self._entries.values())

    def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in self._entries.values():
            out[entry.type] = out.get(entry.type, 0) + 1
        return out

    def reset(self) -> None:
        self._pool_cursor.clear()
        self._pool_used.clear()
        self._by_original.clear()
        self._entries.clear()
        self._mask_index.clear()
        self._mask_seq = 0
        self._case_no_index = 0
        self._prepared = False

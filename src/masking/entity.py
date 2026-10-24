"""脱敏核心数据模型。

设计原则
--------
1. 格式无关：docx / xlsx / pdf 三种处理器各自把内容切成 TextBlock，
   检测器只面对 ``TextBlock.text``，不关心底层是 XML 还是 PDF 内容流。
2. 偏移可靠：Entity.start / end 是相对于所属 TextBlock.text 的字符偏移，
   处理器负责把「块内偏移」翻译成「可回写的位置」（run、单元格、quad）。
3. 检测与替换分离：检测器只报告「哪里有什么」，替换策略由 allocator 决定。

任何新增格式，只要实现 ``Formats`` 协议并产出 TextBlock 列表即可，
检测器与映射逻辑无需改动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, Sequence, runtime_checkable


class EntityType(str, Enum):
    """实体类型。

    命名与 config/rules.yaml 的键一一对应，便于用户按类型开关。
    """

    PERSON = "person"          # 自然人姓名
    ORG = "org"                # 公司、律所、医院、学校等组织
    PLACE = "place"            # 行政区划、道路、地名
    ADDRESS = "address"        # 完整住址（已含 place 时由合并器消解）
    ID_CARD = "id_card"        # 居民身份证号
    PHONE = "phone"            # 手机号 / 固定电话
    BANK_CARD = "bank_card"    # 银行卡号
    USCC = "uscc"              # 统一社会信用代码
    PLATE = "plate"            # 车牌号
    EMAIL = "email"            # 电子邮箱
    CASE_NO = "case_no"        # 案号
    DATE = "date"              # 日期（默认保留，仅识别不替换）
    AMOUNT = "amount"          # 金额（默认保留）
    CUSTOM = "custom"          # 用户词表命中


#: 需要「一致性代号」的实体类型：同一条目全篇使用同一个占位符。
CODED_TYPES: frozenset[EntityType] = frozenset(
    {
        EntityType.PERSON,
        EntityType.ORG,
        EntityType.PLACE,
        EntityType.ADDRESS,
        EntityType.ID_CARD,
        EntityType.PHONE,
        EntityType.BANK_CARD,
        EntityType.USCC,
        EntityType.PLATE,
        EntityType.EMAIL,
        EntityType.CASE_NO,
        EntityType.CUSTOM,
    }
)

#: 默认替换策略：mask_xxx 表示整体掩码，code_word 表示词库代号。
DEFAULT_POLICY: dict[EntityType, str] = {
    EntityType.PERSON: "code_word",
    EntityType.ORG: "code_word",
    EntityType.PLACE: "code_word",
    EntityType.ADDRESS: "code_word",
    EntityType.ID_CARD: "mask",
    EntityType.PHONE: "mask",
    EntityType.BANK_CARD: "mask",
    EntityType.USCC: "mask",
    EntityType.PLATE: "mask",
    EntityType.EMAIL: "mask",
    EntityType.CASE_NO: "case_no",
    EntityType.DATE: "keep",
    EntityType.AMOUNT: "keep",
    EntityType.CUSTOM: "mask",
}


@dataclass(frozen=True, slots=True)
class Entity:
    """一次命中。start/end 为块内字符偏移，半开区间 [start, end)。"""

    type: EntityType
    text: str
    start: int
    end: int
    confidence: float = 1.0
    detector: str = ""

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"非法偏移: {self.start}..{self.end}")

    @property
    def span(self) -> tuple[int, int]:
        return (self.start, self.end)


@dataclass
class TextBlock:
    """一段待检测文本。

    ``locator`` 是格式相关的定位信息，处理器自己解释，检测器不得触碰。
    ``role`` 区分正文与附属区域，便于审计报告按区域统计。
    """

    text: str
    locator: Any = None
    role: str = "body"  # body | header | footer | comment | footnote | metadata | cell


@dataclass
class MatchContext:
    """跨块上下文：已确认实体的原文 → 类型。

    用途：当「【白桦】公司」已被识别，后文出现「该公司」或简称时，
    姓名/组织检测器可借助上下文提高召回。
    """

    known: dict[str, EntityType] = field(default_factory=dict)
    scope: str = ""
    stats: dict[str, int] = field(default_factory=dict)

    def remember(self, entity: Entity) -> None:
        self.known.setdefault(entity.text, entity.type)


@runtime_checkable
class Detector(Protocol):
    """检测器协议。实现者只需保证：不修改 block.text，返回的偏移在界内。"""

    name: str

    def detect(self, block: TextBlock, ctx: MatchContext) -> Sequence[Entity]: ...


@runtime_checkable
class FormatHandler(Protocol):
    """格式处理器协议。

    extract  -> 取出所有可检测文本块
    apply    -> 按 (块, 实体, 替换文本) 回写并保存到目标路径
    audit    -> 返回该格式特有的泄露点（元数据、批注、修订记录等）
    """

    name: str
    suffixes: tuple[str, ...]

    def extract(self, path: str) -> list[TextBlock]: ...

    def apply(self, path: str, out_path: str, edits: Sequence["Edit"]) -> None: ...

    def audit(self, path: str) -> list["Finding"]: ...


@dataclass(frozen=True, slots=True)
class Edit:
    """一次替换指令。"""

    block: TextBlock
    entity: Entity
    replacement: str


@dataclass(frozen=True, slots=True)
class Finding:
    """审计发现。level: info | warn | critical"""

    level: str
    code: str
    message: str
    location: str = ""

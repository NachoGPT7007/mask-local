"""脱敏编排层：提取 → 检测 → 重叠消解 → 生成代号 → 回写 → 审计。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .allocator import MappingEntry, PlaceholderAllocator, PoolExhausted
from .config import Settings
from .entity import Edit, Entity, EntityType, Finding, MatchContext, TextBlock


#: 重叠消解优先级：数字越大越优先保留
#:
#: CUSTOM 排在 PERSON 之上是刻意的：用户把某个词写进 custom_terms.txt，
#: 就是明确表达了「这个词必须掩掉」；若让人名检测器先抢走，
#: 用户显式指定的 mask 策略会失效，且失败得无声无息。
TYPE_PRIORITY: dict[EntityType, int] = {
    EntityType.CUSTOM: 110,
    EntityType.ID_CARD: 100,
    EntityType.USCC: 100,
    EntityType.BANK_CARD: 100,
    EntityType.PHONE: 95,
    EntityType.PLATE: 95,
    EntityType.EMAIL: 95,
    EntityType.CASE_NO: 90,
    EntityType.PERSON: 80,
    EntityType.ORG: 70,
    EntityType.ADDRESS: 60,
    EntityType.PLACE: 55,
    EntityType.DATE: 20,
    EntityType.AMOUNT: 20,
}


def resolve_overlaps(entities: Sequence[Entity]) -> list[Entity]:
    """消解重叠命中。

    规则：区间长者优先；同长按类型优先级；再按置信度高者。
    被完全包含的短实体丢弃，部分重叠的裁剪或丢弃。
    """
    ordered = sorted(
        entities,
        key=lambda e: (-(e.end - e.start), -TYPE_PRIORITY.get(e.type, 0), -e.confidence),
    )
    kept: list[Entity] = []
    for cand in ordered:
        clash = False
        for k in kept:
            if cand.end <= k.start or cand.start >= k.end:
                continue
            clash = True
            break
        if not clash:
            kept.append(cand)
    kept.sort(key=lambda e: e.start)
    return kept


@dataclass
class FileResult:
    source: str
    output: str = ""
    ok: bool = False
    error: str = ""
    blocks: int = 0
    entities: int = 0
    findings: list[Finding] = field(default_factory=list)
    needs_review: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    scope: str = ""
    files: list[FileResult] = field(default_factory=list)
    entries: list[MappingEntry] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for f in self.files if f.ok)

    @property
    def failed(self) -> list[FileResult]:
        return [f for f in self.files if not f.ok]


def build_edits(
    blocks: Sequence[TextBlock],
    detectors: Sequence,
    ctx: MatchContext,
    allocator: PlaceholderAllocator,
) -> tuple[list[Edit], int]:
    """对一组文本块执行检测并生成替换指令。

    注意：同一个 allocator 在多个文件间复用时，编号连续——
    一个案件里 docx 与 pdf 提到的同一个人拿到同一个代号。
    """
    per_block: list[tuple[TextBlock, list[Entity]]] = []
    for block in blocks:
        hits: list[Entity] = []
        for det in detectors:
            try:
                hits.extend(det.detect(block, ctx))
            except Exception:  # 单个检测器失败不应中断整个文件，但要记账
                key = f"detector_error:{getattr(det, 'name', '?')}"
                ctx.stats[key] = ctx.stats.get(key, 0) + 1
        kept = resolve_overlaps(hits)
        for e in kept:
            ctx.remember(e)
        per_block.append((block, kept))

    all_entities = [e for _, hits in per_block for e in hits]
    allocator.prepare(all_entities)

    edits: list[Edit] = []
    for block, hits in per_block:
        for e in hits:
            replacement = allocator.placeholder_for(e)
            if replacement == e.text:
                continue  # keep 策略
            edits.append(Edit(block=block, entity=e, replacement=replacement))
    return edits, len(all_entities)

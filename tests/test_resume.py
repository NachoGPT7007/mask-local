"""续跑（多次脱密）与手动补词的回归测试。

这两条能力最容易在后续改动里被悄悄破坏，尤其是：
  - 续跑时给同一原文分配了新代号 → 旧映射表失效，回填全部错位
  - 补词被其他检测器抢走 → 用户显式指定的词没有生效，而且没有任何报错
  - 掩码编号重号 → XXX1 同时对应两个原值，回填无法判定
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from masking.allocator import PlaceholderAllocator  # noqa: E402
from masking.config import Settings  # noqa: E402
from masking.entity import Entity, EntityType as T  # noqa: E402
from masking.pipeline import mask_path_detailed  # noqa: E402
from masking.vault import Vault  # noqa: E402


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load()


def _alloc(settings: Settings) -> PlaceholderAllocator:
    return PlaceholderAllocator(settings.lexicon, settings.rules)


# ------------------------------------------------------------------ 分配器层


def test_resume_returns_same_placeholder(settings: Settings) -> None:
    """同一原文在续跑时必须拿回原代号，否则第一份映射表立刻失效。"""
    a1 = _alloc(settings)
    first = a1.placeholder_for(Entity(T.PERSON, "张三", 0, 2))

    a2 = PlaceholderAllocator.from_entries(settings.lexicon, settings.rules, a1.entries)
    again = a2.placeholder_for(Entity(T.PERSON, "张三", 0, 2))

    assert again == first


def test_resume_never_reuses_pool_word(settings: Settings) -> None:
    """续跑后新实体必须拿未占用的词，不能和上一轮撞号。"""
    a1 = _alloc(settings)
    used = {
        a1.placeholder_for(Entity(T.PERSON, name, 0, len(name)))
        for name in ("张三", "李四", "王五")
    }

    a2 = PlaceholderAllocator.from_entries(settings.lexicon, settings.rules, a1.entries)
    fresh = a2.placeholder_for(Entity(T.PERSON, "赵六", 0, 2))

    assert fresh not in used


def test_resume_mask_sequence_continues(settings: Settings) -> None:
    """掩码编号从既有最大值续编，不能重号。"""
    a1 = _alloc(settings)
    first = a1.placeholder_for(Entity(T.PHONE, "13800138000", 0, 11))
    second = a1.placeholder_for(Entity(T.PHONE, "13900139000", 0, 11))

    a2 = PlaceholderAllocator.from_entries(settings.lexicon, settings.rules, a1.entries)
    third = a2.placeholder_for(Entity(T.PHONE, "13700137000", 0, 11))

    assert len({first, second, third}) == 3


def test_resume_keeps_mapping_merged(settings: Settings) -> None:
    """续跑后的 entries 是「旧 + 新」合并结果，不是只有新增。"""
    a1 = _alloc(settings)
    a1.placeholder_for(Entity(T.PERSON, "张三", 0, 2))

    a2 = PlaceholderAllocator.from_entries(settings.lexicon, settings.rules, a1.entries)
    a2.placeholder_for(Entity(T.PERSON, "李四", 0, 2))

    originals = {e.original for e in a2.entries}
    assert {"张三", "李四"} <= originals


def test_manual_terms_take_priority(settings: Settings) -> None:
    """用户指定的词按 CUSTOM 处理，优先级高于人名检测器。"""
    from masking.pipeline import _InlineTermsDetector

    det = _InlineTermsDetector(["阿某古某"])
    from masking.entity import MatchContext, TextBlock

    hits = det.detect(TextBlock("原告阿某古某与被告张三"), MatchContext())
    assert any(h.text == "阿某古某" and h.type is T.CUSTOM for h in hits)


# ------------------------------------------------------------------ 流水线层


def _make_docx(path: pathlib.Path, text: str) -> None:
    from docx import Document

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_paragraph(text)
    doc.save(str(path))


def test_pipeline_term_and_resume(tmp_path: pathlib.Path, settings: Settings) -> None:
    """端到端：第一轮不加补词 → 第二轮续跑 + 补词，老代号不变、新词追加。"""
    case = tmp_path / "in"
    _make_docx(case / "材料.docx", "原告阿某古某与被告张三，电话13800138000。")

    out1 = tmp_path / "out1"
    r1 = mask_path_detailed(str(case), out_dir=str(out1), settings=settings)
    assert r1.result.ok_count == 1
    vault1 = r1.vault
    assert vault1 is not None
    zhang1 = next(e.placeholder for e in vault1.entries if e.original == "张三")
    # 规则抓不到「阿某古某」，第一轮不应出现
    assert all(e.original != "阿某古某" for e in vault1.entries)

    out2 = tmp_path / "out2"
    r2 = mask_path_detailed(
        str(case),
        out_dir=str(out2),
        settings=settings,
        vault=vault1,
        extra_terms=["阿某古某"],
    )
    assert r2.result.ok_count == 1
    vault2 = r2.vault
    assert vault2 is not None

    zhang2 = next(e.placeholder for e in vault2.entries if e.original == "张三")
    assert zhang2 == zhang1, "续跑改动了已有代号"

    ayi = next(e for e in vault2.entries if e.original == "阿某古某")
    assert ayi.type == T.CUSTOM.value
    # 与已有掩码不重号
    others = {e.placeholder for e in vault2.entries if e.original != "阿某古某"}
    assert ayi.placeholder not in others


def test_pipeline_resume_writes_merged_vault(tmp_path: pathlib.Path, settings: Settings) -> None:
    """第二轮写出的映射表必须包含第一轮的条目。"""
    case = tmp_path / "in"
    _make_docx(case / "材料.docx", "被告张三，电话13800138000。")

    r1 = mask_path_detailed(str(case), out_dir=str(tmp_path / "o1"), settings=settings)
    r2 = mask_path_detailed(
        str(case), out_dir=str(tmp_path / "o2"), settings=settings, vault=r1.vault
    )
    assert r2.vault_path is not None
    reloaded = Vault.load(str(r2.vault_path))
    assert len(reloaded.entries) >= len(r1.vault.entries)

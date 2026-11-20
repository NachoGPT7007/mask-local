"""pipeline / vault / restore / audit 的集成测试。

依赖策略
--------
detectors/ 与 formats/ 由其他成员并行开发，本测试**不依赖**它们的实现：
- 检测器注入一个只认固定样例词表的 ``StubDetector``；
- docx 处理器注入 ``StubDocxHandler``（实现 FormatHandler 协议）。
这样 pipeline 的真实编排逻辑（共享 allocator、写回、残留复扫、映射表、报告）
全部被测到，等真实实现就绪后无需改动本文件即可跑集成测试。
"""

from __future__ import annotations

import pathlib
import re
import shutil
import stat
import sys
from typing import Any, Iterator, Sequence

import pytest

# 让 `masking` 可导入（项目未 pip install，入口依赖 PYTHONPATH=src）
ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docx import Document  # noqa: E402

from masking.allocator import MappingEntry, PlaceholderAllocator, PoolExhausted  # noqa: E402
from masking.audit import audit_file, scan_text  # noqa: E402
from masking.config import Lexicon, Rules, Settings  # noqa: E402
from masking.entity import Edit, Entity, EntityType, Finding, TextBlock  # noqa: E402
from masking.pipeline import mask_path, mask_path_detailed, output_dirs  # noqa: E402
from masking.restore import RestoreError, restore_file, restore_text  # noqa: E402
from masking.vault import Vault, VaultKeyMissing, VaultKeyInvalid  # noqa: E402

CONFIG = ROOT / "config"


# --------------------------------------------------------------------- 测试替身


NAME_RE = re.compile(r"张三|李四|王五|赵六")
PLACE_RE = re.compile(r"杭州市|西湖区")
ID_RE = re.compile(r"\d{17}[\dXx]")
CASE_NO_RE = re.compile(
    r"[\(（]\s*\d{4}\s*[\)）]\s*[\u4e00-\u9fa5]{1,3}\d{0,4}[\u4e00-\u9fa5]{1,3}\d+\s*号"
)


class StubDetector:
    """只认样例词表的检测器（模拟真实检测层的行为）。"""

    name = "stub-cn"
    order = 10

    def detect(self, block: TextBlock, ctx) -> list[Entity]:
        hits: list[Entity] = []
        for match in NAME_RE.finditer(block.text):
            hits.append(Entity(EntityType.PERSON, match.group(), match.start(), match.end(), 0.92, self.name))
        for match in PLACE_RE.finditer(block.text):
            hits.append(Entity(EntityType.PLACE, match.group(), match.start(), match.end(), 0.90, self.name))
        for match in ID_RE.finditer(block.text):
            hits.append(Entity(EntityType.ID_CARD, match.group(), match.start(), match.end(), 0.99, self.name))
        for match in CASE_NO_RE.finditer(block.text):
            hits.append(Entity(EntityType.CASE_NO, match.group(), match.start(), match.end(), 0.96, self.name))
        return hits


def _iter_docx_paragraphs(document: Any) -> Iterator[Any]:
    """遍历 docx 全部段落（正文 / 表格 / 页眉页脚），顺序必须稳定。"""
    for paragraph in document.paragraphs:
        yield paragraph
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    yield paragraph
    for section in document.sections:
        for container in (section.header, section.footer):
            if container is None:
                continue
            for paragraph in container.paragraphs:
                yield paragraph


class StubDocxHandler:
    """实现 FormatHandler 协议的 docx 处理器（测试用最小实现）。"""

    name = "docx-stub"
    suffixes = (".docx",)

    def extract(self, path: str) -> list[TextBlock]:
        document = Document(str(path))
        return [
            TextBlock(text=p.text, locator=("p", i), role="body")
            for i, p in enumerate(_iter_docx_paragraphs(document))
        ]

    def apply(self, path: str, out_path: str, edits: Sequence[Edit]) -> None:
        document = Document(str(path))
        paragraphs = list(_iter_docx_paragraphs(document))
        grouped: dict[Any, list[Edit]] = {}
        for edit in edits:
            grouped.setdefault(edit.block.locator, []).append(edit)
        for locator, items in grouped.items():
            paragraph = paragraphs[locator[1]]
            text = paragraph.text
            for edit in sorted(items, key=lambda e: e.entity.start, reverse=True):
                text = text[: edit.entity.start] + edit.replacement + text[edit.entity.end:]
            if paragraph.runs:
                paragraph.runs[0].text = text
                for run in paragraph.runs[1:]:
                    run.text = ""
            else:
                paragraph.add_run(text)
        document.save(out_path)

    def audit(self, path: str) -> list[Finding]:
        document = Document(str(path))
        author = (document.core_properties.author or "").strip()
        if author:
            return [Finding("warn", "docx_author", f"文档属性作者为「{author}」，脱敏件仍保留该属性", str(path))]
        return []


class TxtHandler:
    """纯文本处理器，用于多格式混排测试。"""

    name = "txt-stub"
    suffixes = (".txt",)

    def extract(self, path: str) -> list[TextBlock]:
        return [TextBlock(text=pathlib.Path(path).read_text(encoding="utf-8"), locator=None, role="body")]

    def apply(self, path: str, out_path: str, edits: Sequence[Edit]) -> None:
        text = pathlib.Path(path).read_text(encoding="utf-8")
        for edit in sorted(edits, key=lambda e: e.entity.start, reverse=True):
            text = text[: edit.entity.start] + edit.replacement + text[edit.entity.end:]
        pathlib.Path(out_path).write_text(text, encoding="utf-8")

    def audit(self, path: str) -> list[Finding]:
        return []


# --------------------------------------------------------------------- 夹具


@pytest.fixture()
def settings() -> Settings:
    """真实配置：词库与规则都从 config/ 读，确保测试的是线上配置。"""
    return Settings.load(CONFIG / "lexicon.yaml", CONFIG / "rules.yaml")


@pytest.fixture()
def sandbox(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """把 project_home() 指到临时目录，避免测试往仓库 vault/ 里写东西。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MASK_LOCAL_HOME", str(home))
    return tmp_path


def _make_docx(path: pathlib.Path, paragraphs: Sequence[str], author: str = "测试作者") -> pathlib.Path:
    document = Document()
    for line in paragraphs:
        document.add_paragraph(line)
    document.core_properties.author = author
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))
    return path


def _read_docx_text(path: pathlib.Path) -> str:
    document = Document(str(path))
    return "\n".join(p.text for p in _iter_docx_paragraphs(document))


def _run_mask(
    case: pathlib.Path,
    sandbox: pathlib.Path,
    settings: Settings,
    *,
    dry_run: bool = False,
    detectors: Sequence[Any] | None = None,
    handlers: Sequence[Any] | None = None,
):
    return mask_path_detailed(
        case,
        out_dir=sandbox / "out",
        settings=settings,
        detectors=list(detectors) if detectors is not None else [StubDetector()],
        handlers=list(handlers) if handlers is not None else [StubDocxHandler(), TxtHandler()],
        dry_run=dry_run,
    )


CASE_PARAGRAPHS = (
    "原告张三，男，1980年1月1日出生，住杭州市西湖区。",
    "被告李四，身份证号330102199001011234。",
    "本院认为，张三与李四之间的借款合同纠纷一案，应予受理。",
)


# --------------------------------------------------------------------- 映射表


def test_vault_roundtrip_and_plaintext_readable(tmp_path: pathlib.Path) -> None:
    """加密前的 JSON 必须可读；加密后不得出现明文原文。"""
    vault = Vault.from_entries(
        "借款纠纷案",
        [
            MappingEntry("person", "张三", "【熊猫】", 3),
            MappingEntry("id_card", "330102199001011234", "XXX", 1),
        ],
    )
    path = vault.save(tmp_path / "case.vault.json")
    assert path.exists()

    # 加密前的 JSON 结构可读（ensure_ascii=False）
    plaintext = vault.to_json_text()
    assert "张三" in plaintext and '"entries"' in plaintext and "\n  " in plaintext

    # 落盘文件里不能有明文
    raw = path.read_bytes()
    assert "张三".encode("utf-8") not in raw
    assert b"330102199001011234" not in raw

    loaded = Vault.load(path)
    assert loaded.scope == "借款纠纷案"
    assert loaded.find_original("【熊猫】") == "张三"
    assert loaded.find_placeholder("330102199001011234") == "XXX"
    assert loaded.total_occurrences() == 4
    assert loaded.type_stats() == {"person": 1, "id_card": 1}
    assert "2 个不同实体" in loaded.describe()


def test_vault_permissions_are_600(tmp_path: pathlib.Path) -> None:
    vault = Vault.from_entries("case", [MappingEntry("person", "张三", "【熊猫】", 1)])
    path = vault.save(tmp_path / "case.vault.json")
    key = tmp_path / ".key"
    assert key.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_vault_missing_key_is_explicit(tmp_path: pathlib.Path) -> None:
    vault = Vault.from_entries("case", [MappingEntry("person", "张三", "【熊猫】", 1)])
    path = vault.save(tmp_path / "case.vault.json")
    elsewhere = tmp_path / "moved"
    elsewhere.mkdir()
    moved = elsewhere / "case.vault.json"
    shutil.copy(path, moved)  # 只搬映射表，不搬 .key

    with pytest.raises(VaultKeyMissing) as excinfo:
        Vault.load(moved)
    message = str(excinfo.value)
    assert "映射不可恢复，需重新脱敏" in message
    assert ".key" in message


def test_vault_tampered_key_is_explicit(tmp_path: pathlib.Path) -> None:
    vault = Vault.from_entries("case", [MappingEntry("person", "张三", "【熊猫】", 1)])
    path = vault.save(tmp_path / "case.vault.json")
    (tmp_path / ".key").write_bytes(b"not-a-fernet-key")
    with pytest.raises(VaultKeyInvalid) as excinfo:
        Vault.load(path)
    assert "密钥文件损坏" in str(excinfo.value)


# --------------------------------------------------------------------- 回填


def test_restore_text_tolerant(sandbox: pathlib.Path) -> None:
    """代号被拆开、加空格、换半角括号、ASCII 大小写变化都要能回填。"""
    vault = Vault.from_entries(
        "case",
        [
            MappingEntry("person", "张三", "【熊猫】", 3),
            MappingEntry("phone", "13800138000", "XXX1", 2),
        ],
    )
    place_vault = Vault.from_entries("case", [MappingEntry("place", "文三路", "【蓬莱】路", 1)])
    assert restore_text("住【蓬莱】路100号", place_vault) == "住文三路100号"
    assert restore_text("住【 蓬 莱 】路100号", place_vault) == "住文三路100号"

    assert restore_text("【 熊 猫 】是原告", vault) == "张三是原告"
    assert restore_text("【熊 猫】是原告", vault) == "张三是原告"
    assert restore_text("[熊猫]是原告", vault) == "张三是原告"
    assert restore_text("电话 xxx1 请拨打", vault) == "电话 13800138000 请拨打"
    # 不带框的裸词不动，避免把真动物改成人名
    assert restore_text("动物园里的熊猫", vault) == "动物园里的熊猫"

    stats: dict[str, int] = {}
    restored = restore_text("【熊猫】与【熊猫】", vault, stats=stats)
    assert restored == "张三与张三"
    assert stats["replaced"] == 2


def test_restore_text_real_to_code_and_ambiguity(sandbox: pathlib.Path) -> None:
    vault = Vault.from_entries("case", [MappingEntry("person", "张三", "【熊猫】", 1)])
    assert restore_text("张三是原告", vault, direction="real_to_code") == "【熊猫】是原告"

    # 旧版映射表的人为构造：两个原始手机号同掩码 XXX。当前 allocator 已全局唯一，
    # 这里保留作为「旧映射表 / 异常映射」的防御性测试。
    ambiguous = Vault.from_entries(
        "case",
        [
            MappingEntry("phone", "13800138000", "XXX", 1),
            MappingEntry("phone", "13900139000", "XXX", 1),
        ],
    )
    warnings: list[str] = []
    assert restore_text("号码 XXX", ambiguous, warnings=warnings) == "号码 XXX"
    assert warnings and "同时对应多个原文" in warnings[0]

    # 撞号但附近有类型线索时，应能逐处判定
    mixed = Vault.from_entries(
        "case",
        [
            MappingEntry("id_card", "330102199001011234", "XXX1", 1),
            MappingEntry("phone", "13800138000", "XXX1", 1),
        ],
    )
    stats: dict[str, int] = {}
    text = "身份证号XXX1，联系电话XXX1；另一处 XXX1 无法判断。"
    restored = restore_text(text, mixed, stats=stats)
    assert restored.startswith("身份证号330102199001011234，联系电话13800138000；")
    assert "另一处 XXX1" in restored
    assert stats["resolved_ambiguous"] == 2
    assert stats["skipped_ambiguous"] == 1


def test_restore_file_roundtrip(sandbox: pathlib.Path, settings: Settings) -> None:
    case = sandbox / "借款纠纷案"
    source = _make_docx(case / "起诉状.docx", CASE_PARAGRAPHS)
    outcome = _run_mask(case, sandbox, settings)
    masked = pathlib.Path(outcome.result.files[0].output)
    vault = Vault.load(outcome.vault_path)

    restored = sandbox / "回填" / "起诉状.docx"
    stats: dict[str, int] = {}
    restore_file(masked, restored, vault, stats=stats)

    text = _read_docx_text(restored)
    assert "张三" in text and "李四" in text and "杭州市" in text
    assert "330102199001011234" in text
    assert "【熊猫】" not in text
    # 计数必须与实际可回填处数一致（不能把段落级比对与 run 级替换重复累加）
    expected = sum(
        e.count
        for e in vault.entries
        if e.placeholder not in vault.ambiguous_placeholders()
    )
    assert stats["replaced"] == expected
    assert masked.read_bytes() != restored.read_bytes()


def test_restore_xlsx_uses_column_header(tmp_path: pathlib.Path) -> None:
    """xlsx 单元格只有 XXX1，靠表头「身份证号 / 联系电话」消歧。"""
    from openpyxl import Workbook, load_workbook

    source = tmp_path / "m.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["姓名", "身份证号", "联系电话"])
    sheet.append(["张三", "XXX1", "XXX1"])
    workbook.save(source)

    vault = Vault.from_entries(
        "case",
        [
            MappingEntry("id_card", "330102199001011234", "XXX1", 1),
            MappingEntry("phone", "13800138000", "XXX1", 1),
        ],
    )
    out = tmp_path / "r.xlsx"
    stats: dict[str, int] = {}
    restore_file(source, out, vault, stats=stats)
    cells = [c.value for c in load_workbook(out).active[2]]
    assert cells[1] == "330102199001011234"
    assert cells[2] == "13800138000"
    assert stats["resolved_ambiguous"] == 2


def test_restore_docx_preserves_newlines_and_splits(tmp_path: pathlib.Path) -> None:
    """回填不能把段内换行挪位置，也要能处理被拆到多个 run 的代号。"""
    source = tmp_path / "linebreak.docx"
    document = Document()
    first = document.add_paragraph()
    run = first.add_run()
    run.add_text("【昆仑】市西湖区人民法院")
    run.add_break()
    run.add_text("民 事 判 决 书")
    second = document.add_paragraph()
    second.add_run("原告【熊")
    second.add_run("猫】，住【昆")
    second.add_run("仑】市。")
    document.save(source)

    vault = Vault.from_entries(
        "case",
        [
            MappingEntry("place", "杭州市", "【昆仑】市", 3),
            MappingEntry("place", "西湖区", "【不周】区", 1),
            MappingEntry("person", "张三", "【熊猫】", 1),
        ],
    )
    out = tmp_path / "linebreak-restored.docx"
    stats: dict[str, int] = {}
    restore_file(source, out, vault, stats=stats)

    paragraphs = [p.text for p in Document(str(out)).paragraphs]
    assert paragraphs[0] == "杭州市西湖区人民法院\n民 事 判 决 书"
    assert paragraphs[1] == "原告张三，住杭州市。"
    assert stats["replaced"] == 3


def test_restore_file_refuses_to_overwrite_source(sandbox: pathlib.Path) -> None:
    vault = Vault.from_entries("case", [MappingEntry("person", "张三", "【熊猫】", 1)])
    source = sandbox / "a.txt"
    source.write_text("【熊猫】", encoding="utf-8")
    with pytest.raises(RestoreError) as excinfo:
        restore_file(source, source, vault)
    assert "拒绝覆盖原件" in str(excinfo.value)
    assert source.read_text(encoding="utf-8") == "【熊猫】"


# --------------------------------------------------------------------- 审计


def test_scan_text_finds_adjacent_chinese_id(sandbox: pathlib.Path, settings: Settings) -> None:
    """中文紧邻数字时（身份证号3301…）也必须命中，不能因为 \\b 漏扫。"""
    findings = scan_text("身份证号330102199001019999，请核查。", settings.rules, location="x.txt")
    assert any(f.level == "critical" and "身份证" in f.code for f in findings)
    # 报告里的命中片段应打码，不能把完整身份证写进报告
    assert all("330102199001019999" not in f.message for f in findings)


def test_scan_text_warn_for_unmasked_org(sandbox: pathlib.Path, settings: Settings) -> None:
    findings = scan_text("甲方为杭州某某建材有限公司。", settings.rules, location="x.txt")
    assert any(f.level == "warn" and "机构" in f.code for f in findings)


def test_audit_file_fallback_without_handler(tmp_path: pathlib.Path, settings: Settings) -> None:
    leak = tmp_path / "leak.txt"
    leak.write_text("联系方式 13800138000，身份证号330102199001019999。", encoding="utf-8")
    findings = audit_file(leak, settings.rules)
    assert any(f.level == "critical" for f in findings)
    assert all(f.location for f in findings)


def test_audit_detects_id_injected_into_masked_text(sandbox: pathlib.Path, settings: Settings) -> None:
    """在脱敏产物里人为塞一个身份证号，残留扫描必须报 critical。"""
    case = sandbox / "借款纠纷案"
    _make_docx(case / "起诉状.docx", CASE_PARAGRAPHS)
    outcome = _run_mask(case, sandbox, settings)
    masked = pathlib.Path(outcome.result.files[0].output)
    handler = StubDocxHandler()
    text = "\n".join(b.text for b in handler.extract(str(masked)))
    tampered = f"{text}\n补充：身份证号330102199001019999"

    findings = scan_text(tampered, settings.rules, location="起诉状.docx(脱敏后)")
    assert any(f.level == "critical" for f in findings)


def test_scan_text_ignores_own_outputs(settings: Settings) -> None:
    """自编案号/代号是本次产物，不能被当成残留；真实泄露仍要报。"""
    rules = Rules(residual={"未脱敏案号": [r"\(2024\)甲法民初\d+号"]})
    text = "案号(2024)甲法民初1号；另发现(2024)乙法民初1234号"
    assert scan_text(text, rules), "未排除时应当有命中"
    findings = scan_text(text, rules, ignore_values=["(2024)甲法民初1号"])
    assert findings == []

    # 真实泄露不能被顺手放过
    leak_rules = Rules(residual={"未脱敏案号": [r"[\(（]\s*\d{4}\s*[\)）]\s*[\u4e00-\u9fa5]{1,3}\d{0,4}[\u4e00-\u9fa5]{1,3}\d+\s*号"]})
    hits = scan_text(text, leak_rules, ignore_values=["(2024)甲法民初1号"])
    assert len(hits) == 1
    assert hits[0].code == "residual:未脱敏案号"
    assert "另发现" in hits[0].message  # 命中真实泄露，而不是自编案号


def test_masked_case_no_is_not_treated_as_residual(sandbox: pathlib.Path, settings: Settings) -> None:
    """自编案号 (2024)甲法民初1号 不能被流水线判成 critical 残留。"""
    case = sandbox / "案号案件"
    _make_docx(case / "判决书.docx", ("原告张三。(2024)乙法民初1234号",))
    outcome = _run_mask(case, sandbox, settings)
    file_result = outcome.result.files[0]
    assert file_result.ok, file_result.error
    masked_text = _read_docx_text(pathlib.Path(file_result.output))
    assert "甲法" in masked_text
    assert not [f for f in file_result.findings if "案号" in f.code]


# --------------------------------------------------------------------- 主流程


def test_mask_folder_end_to_end(sandbox: pathlib.Path, settings: Settings) -> None:
    case = sandbox / "借款纠纷案"
    source = _make_docx(case / "起诉状.docx", CASE_PARAGRAPHS)
    original_bytes = source.read_bytes()

    outcome = _run_mask(case, sandbox, settings)
    result = outcome.result
    assert result.scope == "借款纠纷案"
    assert len(result.files) == 1
    file_result = result.files[0]
    assert file_result.ok, file_result.error
    assert file_result.entities >= 4

    # 输出文件存在且路径符合 output 配置
    masked = pathlib.Path(file_result.output)
    assert masked.exists()
    assert masked == sandbox / "out" / "起诉状.docx"

    # 原件一个字节都不能变
    assert source.read_bytes() == original_bytes

    # 原文消失、代号出现
    masked_text = _read_docx_text(masked)
    assert "张三" not in masked_text
    assert "李四" not in masked_text
    assert "杭州市" not in masked_text
    assert "330102199001011234" not in masked_text
    assert "【熊猫】" in masked_text
    assert "】市" in masked_text  # 地名代号保留「市」后缀
    assert "XXX1" in masked_text  # 掩码全局编号（即使只有一个值也从 1 开始）

    # 映射表存在、可解密、权限正确
    assert outcome.vault_path is not None and outcome.vault_path.exists()
    assert outcome.vault_path.parent == sandbox / "out" / "vault"
    assert outcome.report_path == sandbox / "out" / "脱敏报告.md"
    assert stat.S_IMODE(outcome.vault_path.stat().st_mode) == 0o600
    key = outcome.vault_path.parent / ".key"
    assert key.exists() and stat.S_IMODE(key.stat().st_mode) == 0o600
    vault = Vault.load(outcome.vault_path)
    assert vault.find_original("【熊猫】") == "张三"
    assert vault.scope == "借款纠纷案"
    assert vault.entries

    # 可交付报告：含三张清单 + 风险提示，但**不含任何真名**
    assert outcome.report_path == sandbox / "out" / "脱敏报告.md"
    assert outcome.report_path.exists()
    report = outcome.report_path.read_text(encoding="utf-8")
    for heading in ("一、已处理", "二、需人工复核", "三、无法处理", "四、风险提示"):
        assert heading in report
    assert "日期与金额默认保留" in report
    assert "按实体类型统计" in report
    assert "回填命令" in report
    assert "全局唯一" in report
    assert "（已隐去）" in report
    for secret in ("张三", "李四", "杭州市", "330102199001011234", "13800138000"):
        assert secret not in report
    # 可交付报告是给人看的，权限 644 即可
    assert stat.S_IMODE(outcome.report_path.stat().st_mode) == 0o644

    # 复核清单：含真名，只在 vault 目录，0600，首行警告
    assert outcome.review_path == sandbox / "out" / "vault" / "复核清单.md"
    assert outcome.review_path.exists()
    assert stat.S_IMODE(outcome.review_path.stat().st_mode) == 0o600
    review = outcome.review_path.read_text(encoding="utf-8")
    assert review.startswith("> **本文件含真实姓名")
    assert "张三" in review and "李四" in review
    assert "330102199001011234" in review

    # 残留复扫：没有 critical
    assert not [f for f in file_result.findings if f.level == "critical"]


def test_mask_reuses_same_code_across_files(sandbox: pathlib.Path, settings: Settings) -> None:
    """同一 allocator 跨文件复用：同一人拿到同一代号。"""
    case = sandbox / "跨文件案件"
    _make_docx(case / "a起诉状.docx", ("原告张三，住杭州市。",))
    _make_docx(case / "b答辩状.docx", ("被告张三，与李四系同事。",))

    outcome = _run_mask(case, sandbox, settings)
    assert outcome.result.ok_count == 2
    texts = {pathlib.Path(fr.output).name: _read_docx_text(pathlib.Path(fr.output)) for fr in outcome.result.files}
    assert "【熊猫】" in texts["a起诉状.docx"]
    assert "【熊猫】" in texts["b答辩状.docx"]
    vault = Vault.load(outcome.vault_path)
    persons = {e.original: e.placeholder for e in vault.entries if e.type == "person"}
    assert persons["张三"] == "【熊猫】"
    assert persons["李四"] != persons["张三"]


def test_mask_skips_hidden_and_output_dir(sandbox: pathlib.Path, settings: Settings) -> None:
    case = sandbox / "含隐藏文件"
    _make_docx(case / "正常.docx", ("原告张三。",))
    _make_docx(case / ".hidden.docx", ("原告李四。",))
    _make_docx(case / ".git" / "内部.docx", ("原告王五。",))

    outcome = _run_mask(case, sandbox, settings)
    names = {pathlib.Path(fr.source).name for fr in outcome.result.files}
    assert names == {"正常.docx"}

    # 第二次运行不应把上次的输出当成输入
    outcome2 = _run_mask(case, sandbox, settings)
    assert len(outcome2.result.files) == 1


def test_dry_run_writes_nothing(sandbox: pathlib.Path, settings: Settings) -> None:
    case = sandbox / "借款纠纷案"
    _make_docx(case / "起诉状.docx", CASE_PARAGRAPHS)

    outcome = _run_mask(case, sandbox, settings, dry_run=True)
    assert outcome.result.ok_count == 1
    assert outcome.result.files[0].entities >= 4
    assert outcome.vault_path is None
    assert outcome.report_path is None
    assert not (sandbox / "out").exists()
    assert not (sandbox / "home" / "vault").exists()
    # dry-run 也在内存里做了残留预扫（这里没有被检测规则覆盖的实体，故无 critical）
    assert not [f for f in outcome.result.files[0].findings if f.level == "critical"]


def test_unsupported_format_recorded_but_others_continue(sandbox: pathlib.Path, settings: Settings) -> None:
    case = sandbox / "混合格式"
    _make_docx(case / "good.docx", ("原告张三。",))
    (case / "bad.bin").write_bytes(b"\x00\x01\x02")

    outcome = _run_mask(case, sandbox, settings)
    by_name = {pathlib.Path(fr.source).name: fr for fr in outcome.result.files}
    assert by_name["good.docx"].ok
    assert not by_name["bad.bin"].ok
    assert "不支持的格式" in by_name["bad.bin"].error
    # 部分失败 → 报告照旧生成
    assert outcome.report_path is not None and outcome.report_path.exists()
    assert "三、无法处理" in outcome.report_path.read_text(encoding="utf-8")


def test_mask_path_contract_returns_run_result(sandbox: pathlib.Path, settings: Settings) -> None:
    case = sandbox / "借款纠纷案"
    _make_docx(case / "起诉状.docx", ("原告张三。",))
    result = mask_path(
        case,
        out_dir=sandbox / "out",
        settings=settings,
        detectors=[StubDetector()],
        handlers=[StubDocxHandler()],
    )
    assert result.scope == "借款纠纷案"
    assert result.ok_count == 1
    assert result.entries


def test_critical_residual_blocks_delivery(sandbox: pathlib.Path, settings: Settings) -> None:
    """输出仍有 critical 残留：照常交付，但错误说明与报告必须标红。

    口径是「自动处理 + 重点复核」，不是「全自动零漏检」：2 万字文书里
    500+ 处替换成功、只剩 1 处边角时判「失败」，会让用户以为整份白做。
    正确做法是交付可用件，同时在报告与复核清单里点名这几处。
    """
    case = sandbox / "残留案件"
    _make_docx(case / "含手机号.docx", ("原告张三，联系电话13800138000。",))
    outcome = _run_mask(case, sandbox, settings)
    file_result = outcome.result.files[0]
    assert file_result.ok
    assert "需人工处理" in file_result.error
    assert any(f.level == "critical" for f in file_result.findings)
    assert pathlib.Path(file_result.output).exists()
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "critical" in report


def test_dry_run_reports_undeliverable_without_writing(sandbox: pathlib.Path, settings: Settings) -> None:
    """dry-run 也要提前发现「按当前规则脱完仍会泄露」，且不写任何文件。"""
    case = sandbox / "预扫拦截案"
    _make_docx(case / "含手机号.docx", ("原告张三，联系电话13800138000。",))
    outcome = _run_mask(case, sandbox, settings, dry_run=True)
    file_result = outcome.result.files[0]
    assert not file_result.ok
    assert "预扫" in file_result.error and "critical" in file_result.error
    assert not (sandbox / "out").exists()


class EncryptedPdfHandler:
    """模拟 pdf-fmt：加密 PDF 的 extract 抛 RuntimeError。"""

    name = "pdf-encrypted-stub"
    suffixes = (".pdf",)

    def extract(self, path: str) -> list[TextBlock]:
        raise RuntimeError("PDF is encrypted, password required")

    def apply(self, path: str, out_path: str, edits) -> None:  # pragma: no cover
        raise AssertionError("加密文件不应进入 apply")

    def audit(self, path: str) -> list[Finding]:
        return []


class LastFindingsHandler(StubDocxHandler):
    """模拟 pdf-fmt：把页级告警挂在 last_findings 上。"""

    def __init__(self) -> None:
        self.last_findings: list[Finding] = []

    def apply(self, path: str, out_path: str, edits) -> None:
        super().apply(path, out_path, edits)
        self.last_findings = [
            Finding("warn", "pdf.placeholder_overflow", "代号「【昆仑】市」在 20pt 空间内放不下", "page=1"),
            Finding("warn", "pdf.image", "第 1 页含 1 张图片（扫描底图/印章）", "page=1"),
        ]


def test_encrypted_pdf_counted_as_failed(sandbox: pathlib.Path, settings: Settings) -> None:
    """加密 PDF 必须进「无法处理」，不能算成功 0 处替换。"""
    case = sandbox / "加密案"
    case.mkdir()
    (case / "加密合同.pdf").write_bytes(b"%PDF-1.7\n%fake encrypted")
    outcome = mask_path_detailed(
        case,
        out_dir=sandbox / "out",
        settings=settings,
        detectors=[StubDetector()],
        handlers=[EncryptedPdfHandler()],
    )
    file_result = outcome.result.files[0]
    assert not file_result.ok
    assert "已加密" in file_result.error and "需密码" in file_result.error
    assert not pathlib.Path(file_result.output).exists()
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "密码" in report
    assert "三、无法处理" in report


def test_handler_last_findings_merged_and_images_forced_review(
    sandbox: pathlib.Path, settings: Settings
) -> None:
    """last_findings 必须并入报告；图片一律进人工复核清单。"""
    case = sandbox / "页级告警案"
    _make_docx(case / "材料.docx", ("原告张三。",))
    outcome = mask_path_detailed(
        case,
        out_dir=sandbox / "out",
        settings=settings,
        detectors=[StubDetector()],
        handlers=[LastFindingsHandler()],
    )
    file_result = outcome.result.files[0]
    codes = {f.code for f in file_result.findings}
    assert "pdf.placeholder_overflow" in codes
    assert "pdf.image" in codes
    assert any("图片" in item and "请人工确认是否需要遮盖" in item for item in file_result.needs_review)
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "2.0 图片内容（强制复核）" in report
    assert "证件照/印章/手写签名" in report


def test_pdf_text_companion_export(tmp_path: pathlib.Path) -> None:
    """PDF 伴生 .txt 用 sort=True，并保留分页符。"""
    import pymupdf

    from masking.pipeline import _export_pdf_text

    pdf = tmp_path / "m.pdf"
    document = pymupdf.open()
    first = document.new_page()
    first.insert_text((72, 100), "第一页：原告【熊猫】", fontname="china-s")
    second = document.new_page()
    second.insert_text((72, 100), "第二页：被告【长颈鹿】", fontname="china-s")
    document.save(pdf)
    document.close()

    txt = _export_pdf_text(pdf, tmp_path / "m.txt")
    text = txt.read_text(encoding="utf-8")
    assert "【熊猫】" in text and "【长颈鹿】" in text
    assert "\f" in text  # 分页符
    assert text.index("【熊猫】") < text.index("【长颈鹿】")


# --------------------------------------------------------------------- 词池用尽


def _tiny_lexicon() -> Lexicon:
    return Lexicon(
        pools={"person": ["甲", "乙"], "place": ["东", "南"], "org": ["一", "二"]},
        org_suffix={"other": "单位"},
        place_suffix=["市"],
        mask_token="XXX",
    )


def test_allocator_pool_exhausted_has_guidance(settings: Settings) -> None:
    allocator = PlaceholderAllocator(_tiny_lexicon(), settings.rules, scope="case")
    persons = [
        Entity(EntityType.PERSON, name, 0, len(name)) for name in ("张三", "李四", "王五")
    ]
    allocator.prepare(persons)
    allocator.placeholder_for(persons[0])
    allocator.placeholder_for(persons[1])
    with pytest.raises(PoolExhausted) as excinfo:
        allocator.placeholder_for(persons[2])
    message = str(excinfo.value)
    assert "词池" in message and "用尽" in message
    assert "config/lexicon.yaml" in message
    assert "pools.person" in message


def test_pipeline_surfaces_pool_exhausted(sandbox: pathlib.Path, settings: Settings) -> None:
    tiny_settings = Settings(lexicon=_tiny_lexicon(), rules=settings.rules)
    case = sandbox / "词池告急案"
    _make_docx(
        case / "多原告.docx",
        ("原告张三、李四、王五、赵六系同村村民。",),
    )
    outcome = _run_mask(case, sandbox, tiny_settings)
    file_result = outcome.result.files[0]
    assert not file_result.ok
    assert "词池" in file_result.error
    assert "lexicon.yaml" in file_result.error
    assert not pathlib.Path(file_result.output).exists()
    # 失败也要留下可读的报告，告诉用户改哪里
    assert outcome.report_path is not None and outcome.report_path.exists()
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "词池" in report and "三、无法处理" in report


# --------------------------------------------------------------------- 目录布局


def test_output_dirs_follow_rules_yaml(sandbox: pathlib.Path, settings: Settings) -> None:
    case = sandbox / "案件A"
    dirs = output_dirs(case, None, settings)
    assert dirs.scope == "案件A"
    assert dirs.masked == sandbox / "home" / "output" / "案件A"
    assert dirs.vault == sandbox / "home" / "vault" / "案件A"

    other = output_dirs(case, sandbox / "自定义", settings)
    # 传了 out_dir：交付包自包含，脱敏件直接放 out_dir，映射表在其 vault/ 子目录
    assert other.masked == sandbox / "自定义"
    assert other.vault == sandbox / "自定义" / "vault"
    assert other.report_dir == sandbox / "自定义"
    assert other.external is True

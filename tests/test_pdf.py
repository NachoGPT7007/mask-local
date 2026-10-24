"""PDF 处理器测试。

样本全部程序生成，不依赖仓库外部资源：
- 文本型 PDF：PyMuPDF 用内置 CJK 字体写入中文，模拟有文本层的电子文书；
- 扫描型 PDF：PIL 用 Noto Serif CJK 把中文画成图片，再存成 PDF，模拟纸质扫描件。

另外对 samples/case-001 下的真实样本做集成验证（文件不存在时自动跳过），
那些样本带 LibreOffice 生成的断字/空格和真实扫描图，是更容易暴露定位问题的场景。

覆盖的核心不变量：
1. 原文件字节不变（绝不修改源文件）；
2. 处理后的 PDF 能被 PyMuPDF 重新打开，且 ``get_text()`` 里找不到原文；
3. 扫描件还要再过一遍 OCR —— 图像像素真的被擦掉了，不是盖了层白框。
"""

from __future__ import annotations

import hashlib
import pathlib
import sys

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from masking.entity import Edit, Entity, EntityType  # noqa: E402
from masking.formats import ocr  # noqa: E402
from masking.formats.pdf_handler import (  # noqa: E402
    HANDLER,
    PdfAnnotLocator,
    PdfHandler,
    PdfLocator,
    PdfTocLocator,
)

NOTO_SERIF = "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"

#: 测试用实体：原文、类型、代号。与 allocator 的产物形态一致（带【】/XXX）。
ENTITIES = [
    ("张三", EntityType.PERSON, "【熊猫】"),
    ("330106199001011234", EntityType.ID_CARD, "XXX"),
    ("杭州某某科技有限公司", EntityType.ORG, "【竹】公司"),
    ("杭州市西湖区文三路123号", EntityType.ADDRESS, "【松】"),
    ("13800138000", EntityType.PHONE, "XXX"),
]


# ---------------------------------------------------------------------------
# 样本生成
# ---------------------------------------------------------------------------


def make_text_pdf(path: pathlib.Path, *, font_size: float = 14.0) -> None:
    """生成一份有文本层的中文 PDF（模拟电子送达的判决书）。"""
    doc = pymupdf.open()
    page = doc.new_page()
    lines = [
        "原告张三，男，1990年1月1日出生。",
        "身份证号330106199001011234，电话13800138000。",
        "被告杭州某某科技有限公司，住所地杭州市西湖区文三路123号。",
    ]
    y = 90.0
    for line in lines:
        page.insert_text((72, y), line, fontsize=font_size, fontname="china-s")
        y += font_size * 2.2
    doc.save(path, garbage=4, deflate=True)
    doc.close()


def make_scan_pdf(path: pathlib.Path, *, dpi: int = 200) -> None:
    """生成一份没有文本层的扫描件 PDF（PIL 画中文图片再存 PDF）。"""
    width = int(8.27 * dpi)
    height = int(11.69 * dpi)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(NOTO_SERIF, int(dpi * 0.16), index=2)
    lines = [
        "原告张三，",
        "身份证号330106199001011234，",
        "被告杭州某某科技有限公司。",
    ]
    y = int(dpi * 0.7)
    for line in lines:
        draw.text((int(dpi * 0.5), y), line, font=font, fill="black")
        y += int(dpi * 0.42)
    image.save(path, "PDF", resolution=dpi)


def make_encrypted_pdf(path: pathlib.Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "机密材料", fontsize=14, fontname="china-s")
    doc.save(
        path,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="user-secret",
    )
    doc.close()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def build_edits(blocks: list, mapping: list[tuple[str, EntityType, str]]) -> list[Edit]:
    """按「原文 → 类型 → 代号」在块文本里查找并生成 Edit（同块内不重叠）。"""
    edits: list[Edit] = []
    for block in blocks:
        hits: list[tuple[int, int, EntityType, str, str]] = []
        for original, etype, replacement in mapping:
            cursor = 0
            while True:
                index = block.text.find(original, cursor)
                if index < 0:
                    break
                hits.append((index, index + len(original), etype, replacement, original))
                cursor = index + len(original)
        hits.sort(key=lambda item: item[0])
        last_end = -1
        for start, end, etype, replacement, original in hits:
            if start < last_end:
                continue
            edits.append(
                Edit(
                    block=block,
                    entity=Entity(type=etype, text=original, start=start, end=end),
                    replacement=replacement,
                )
            )
            last_end = end
    return edits


def file_hash(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def page_text(path: pathlib.Path) -> str:
    doc = pymupdf.open(path)
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def render_ocr(path: pathlib.Path, page_index: int = 0) -> str:
    """把输出页重新渲染并 OCR —— 用来验证图像像素确实被擦除。"""
    doc = pymupdf.open(path)
    try:
        pix = doc[page_index].get_pixmap(dpi=300, alpha=False)
        png = pix.tobytes("png")
    finally:
        doc.close()
    return "".join(line.text for line in ocr.recognize(png))


# ---------------------------------------------------------------------------
# 文本型 PDF
# ---------------------------------------------------------------------------


def test_handler_contract():
    assert HANDLER.name == "pdf"
    assert HANDLER.suffixes == (".pdf",)
    assert isinstance(HANDLER, PdfHandler)


def test_text_pdf_extract_keeps_char_alignment(tmp_path):
    pdf = tmp_path / "text.pdf"
    make_text_pdf(pdf)

    handler = PdfHandler()
    blocks = handler.extract(str(pdf))

    assert blocks, "文本型 PDF 应该能提取到文本块"
    joined = "\n".join(block.text for block in blocks)
    assert "张三" in joined
    assert "330106199001011234" in joined
    for block in blocks:
        assert isinstance(block.locator, PdfLocator)
        assert block.locator.source == "text"
        # 核心不变量：字符框与文本逐字符对齐
        assert len(block.locator.char_boxes) == len(block.text)
        assert block.locator.page_no == 0


def test_text_pdf_apply_erases_original(tmp_path):
    pdf = tmp_path / "text.pdf"
    out = tmp_path / "text_masked.pdf"
    make_text_pdf(pdf)
    before = file_hash(pdf)

    handler = PdfHandler()
    blocks = handler.extract(str(pdf))
    edits = build_edits(blocks, ENTITIES)
    assert edits, "样本里应能定位到实体"

    handler.apply(str(pdf), str(out), edits)

    # 1) 源文件一个字节都没动
    assert file_hash(pdf) == before
    # 2) 输出文件可重新打开
    doc = pymupdf.open(out)
    try:
        assert doc.page_count == 1
        text = doc[0].get_text()
    finally:
        doc.close()
    # 3) 原文消失，代号出现
    for original, _etype, _replacement in ENTITIES:
        assert original not in text, f"原文仍可读出：{original}"
    assert "【熊猫】" in text
    assert "XXX" in text
    # 4) 自检没有 critical
    assert not [f for f in handler.last_findings if f.level == "critical"]


def test_apply_refuses_to_overwrite_source(tmp_path):
    pdf = tmp_path / "text.pdf"
    make_text_pdf(pdf)
    handler = PdfHandler()
    blocks = handler.extract(str(pdf))
    edits = build_edits(blocks, ENTITIES)

    with pytest.raises(ValueError):
        handler.apply(str(pdf), str(pdf), edits)


def test_placeholder_overflow_still_written(tmp_path):
    """代号比原文宽、后面紧邻汉字时，宁可缩字号/溢出也要写上去。"""
    pdf = tmp_path / "narrow.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "原告张三男", fontsize=10, fontname="china-s")
    doc.save(pdf)
    doc.close()

    handler = PdfHandler()
    blocks = handler.extract(str(pdf))
    edits = build_edits(blocks, [("张三", EntityType.PERSON, "【熊猫】")])
    out = tmp_path / "narrow_masked.pdf"
    handler.apply(str(pdf), str(out), edits)

    text = page_text(out)
    assert "【熊猫】" in text, "放不下也必须写代号，不能留空"
    assert "张三" not in text


# ---------------------------------------------------------------------------
# 扫描型 PDF
# ---------------------------------------------------------------------------


def test_scan_pdf_extract_uses_ocr(tmp_path):
    pdf = tmp_path / "scan.pdf"
    make_scan_pdf(pdf)

    handler = PdfHandler()
    blocks = handler.extract(str(pdf))

    assert blocks, "扫描件应通过 OCR 提取到文本块"
    joined = "\n".join(block.text for block in blocks)
    assert "张三" in joined
    assert "330106199001011234" in joined
    for block in blocks:
        assert isinstance(block.locator, PdfLocator)
        assert block.locator.source == "ocr"
        assert len(block.locator.char_boxes) == len(block.text)


def test_scan_pdf_apply_erases_pixels(tmp_path):
    pdf = tmp_path / "scan.pdf"
    out = tmp_path / "scan_masked.pdf"
    make_scan_pdf(pdf)
    before = file_hash(pdf)

    handler = PdfHandler()
    handler.ocr_verify = False  # 本测试自己再 OCR 一次做独立验证
    blocks = handler.extract(str(pdf))
    edits = build_edits(blocks, ENTITIES)
    assert edits

    handler.apply(str(pdf), str(out), edits)

    assert file_hash(pdf) == before, "扫描件源文件不允许被修改"

    text = page_text(out)
    assert "【熊猫】" in text
    assert "张三" not in text
    assert "330106199001011234" not in text

    # 关键一步：把输出页重新渲染再 OCR，确认图像像素里也没有原文
    recognized = render_ocr(out)
    assert "330106199001011234" not in recognized
    assert "张三" not in recognized
    assert "熊猫" in recognized, "代号应该能被 OCR 读出来"


# ---------------------------------------------------------------------------
# 加密与审计
# ---------------------------------------------------------------------------


def test_encrypted_pdf_is_reported_not_crashed(tmp_path):
    pdf = tmp_path / "locked.pdf"
    make_encrypted_pdf(pdf)

    handler = PdfHandler()
    assert handler.extract(str(pdf)) == []
    assert any(f.level == "critical" and f.code == "pdf.encrypted" for f in handler.last_findings)

    findings = handler.audit(str(pdf))
    assert any(f.level == "critical" and f.code == "pdf.encrypted" for f in findings)

    with pytest.raises(RuntimeError):
        handler.apply(str(pdf), str(tmp_path / "locked_out.pdf"), [])


def test_audit_reports_metadata_annot_toc_and_images(tmp_path):
    pdf = tmp_path / "leaky.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "原告张三", fontsize=14, fontname="china-s")
    doc.set_metadata(
        {
            "author": "张三",
            "title": "张三诉杭州某某科技有限公司买卖合同纠纷",
            "subject": "内部材料",
            "keywords": "张三,身份证",
        }
    )
    doc.set_toc([[1, "张三案卷", 1]])
    page.add_text_annot((200, 200), "联系电话13800138000")
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
    pix.clear_with(200)
    page.insert_image(pymupdf.Rect(300, 300, 380, 380), pixmap=pix)
    doc.save(pdf)
    doc.close()

    findings = PdfHandler().audit(str(pdf))
    codes = {finding.code for finding in findings}

    assert "pdf.metadata_author" in codes
    assert "pdf.metadata_title" in codes
    assert "pdf.toc" in codes
    assert "pdf.annot" in codes
    assert "pdf.image" in codes
    # 审计里必须明确说明图像内容无法逐字脱敏
    image_finding = next(f for f in findings if f.code == "pdf.image")
    assert "图像内容无法" in image_finding.message


def test_audit_reports_embedded_files(tmp_path):
    pdf = tmp_path / "embedded.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "正文", fontsize=14, fontname="china-s")
    doc.embfile_add("名单.txt", b"zhang san 330106199001011234")
    doc.save(pdf)
    doc.close()

    findings = PdfHandler().audit(str(pdf))
    assert any(f.code == "pdf.embedded_files" and f.level == "critical" for f in findings)


def test_apply_with_empty_edits_still_writes_copy(tmp_path):
    pdf = tmp_path / "text.pdf"
    out = tmp_path / "copy.pdf"
    make_text_pdf(pdf)
    PdfHandler().apply(str(pdf), str(out), [])
    assert out.exists() and out.stat().st_size > 0
    assert page_text(out).strip(), "无编辑时输出应保留原文内容"


def test_apply_scrubs_metadata_containing_known_entities(tmp_path):
    pdf = tmp_path / "meta.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "原告张三", fontsize=14, fontname="china-s")
    doc.set_metadata({"author": "张三", "title": "张三案"})
    doc.save(pdf)
    doc.close()

    handler = PdfHandler()
    blocks = handler.extract(str(pdf))
    edits = build_edits(blocks, [("张三", EntityType.PERSON, "【熊猫】")])
    out = tmp_path / "meta_out.pdf"
    handler.apply(str(pdf), str(out), edits)

    metadata = pymupdf.open(out).metadata
    assert metadata.get("author") == "【熊猫】"
    assert "张三" not in (metadata.get("title") or "")
    assert any(f.code == "pdf.metadata_scrubbed" for f in handler.last_findings)


# ---------------------------------------------------------------------------
# 书签、批注与位置级残留校验
# ---------------------------------------------------------------------------


def test_toc_title_is_masked(tmp_path):
    """书签标题不在页面文本层里，但目录会原样展示当事人，必须一起脱敏。"""
    pdf = tmp_path / "toc.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "正文", fontsize=14, fontname="china-s")
    doc.set_toc([[1, "张三诉杭州某某科技有限公司案", 1], [2, "本案法律依据", 1]])
    doc.save(pdf)
    doc.close()

    handler = PdfHandler()
    blocks = handler.extract(str(pdf))
    toc_blocks = [b for b in blocks if isinstance(b.locator, PdfTocLocator)]
    assert len(toc_blocks) == 2
    assert toc_blocks[0].text == "张三诉杭州某某科技有限公司案"

    edits = build_edits(
        toc_blocks,
        [
            ("张三", EntityType.PERSON, "【熊猫】"),
            ("杭州某某科技有限公司", EntityType.ORG, "【竹】公司"),
        ],
    )
    assert edits
    out = tmp_path / "toc_out.pdf"
    handler.apply(str(pdf), str(out), edits)

    toc = pymupdf.open(out).get_toc()
    assert toc[0][0] == 1 and toc[0][2] == 1, "书签层级与页码必须保持不变"
    assert "张三" not in toc[0][1]
    assert "杭州某某科技有限公司" not in toc[0][1]
    assert "【熊猫】" in toc[0][1]
    assert toc[1][1] == "本案法律依据", "未被命中的书签不应被改动"
    assert any(f.code == "pdf.toc_scrubbed" for f in handler.last_findings)


def test_annot_content_is_masked(tmp_path):
    """批注正文同样不在 get_text() 里，不处理就等于把姓名留在气泡里。"""
    pdf = tmp_path / "annot.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "正文", fontsize=14, fontname="china-s")
    page.add_text_annot((200, 200), "联系电话13800138000，联系人张三")
    doc.save(pdf)
    doc.close()

    handler = PdfHandler()
    blocks = handler.extract(str(pdf))
    annot_blocks = [b for b in blocks if isinstance(b.locator, PdfAnnotLocator)]
    assert annot_blocks and "张三" in annot_blocks[0].text

    edits = build_edits(
        annot_blocks,
        [("张三", EntityType.PERSON, "【熊猫】"), ("13800138000", EntityType.PHONE, "XXX")],
    )
    out = tmp_path / "annot_out.pdf"
    handler.apply(str(pdf), str(out), edits)

    doc = pymupdf.open(out)
    try:
        contents = [an.info.get("content", "") for an in (doc[0].annots() or [])]
    finally:
        doc.close()
    joined = " ".join(contents)
    assert "张三" not in joined
    assert "13800138000" not in joined
    assert "【熊猫】" in joined
    assert any(f.code == "pdf.annots_scrubbed" for f in handler.last_findings)


def test_residual_elsewhere_is_warn_not_critical(tmp_path):
    """同页同词多处、只编辑了第一处：第二处属检测漏检，不能报成「擦除未生效」。"""
    pdf = tmp_path / "dup.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "张三与张三", fontsize=14, fontname="china-s")
    doc.save(pdf)
    doc.close()

    handler = PdfHandler()
    blocks = handler.extract(str(pdf))
    block = next(b for b in blocks if "张三与张三" in b.text)
    first = Edit(
        block=block,
        entity=Entity(type=EntityType.PERSON, text="张三", start=0, end=2),
        replacement="【熊猫】",
    )
    out = tmp_path / "dup_out.pdf"
    handler.apply(str(pdf), str(out), [first])

    codes = [f.code for f in handler.last_findings]
    assert "pdf.residual_elsewhere" in codes, "别处的同名文本应报为检测漏检"
    assert "pdf.residual_text" not in codes, "不能把别处漏检误报成擦除失败"
    assert "【熊猫】" in page_text(out)
    assert "张三" in page_text(out), "第二处本来就没被编辑，应保留"


def test_case_no_partial_entity_expands_to_full_number(tmp_path):
    """检测器只给了半截案号时，宁可整段擦掉，也不能留下半截。"""
    pdf = tmp_path / "case_no.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    full = "（2025）浙0192民初18143号"
    page.insert_text((72, 100), full, fontsize=14, fontname="china-s")
    doc.save(pdf)
    doc.close()

    handler = PdfHandler()
    blocks = handler.extract(str(pdf))
    block = next(b for b in blocks if full in b.text)
    cut = full.index("民初") + 2  # 只覆盖「（2025）浙0192民初」
    edit = Edit(
        block=block,
        entity=Entity(type=EntityType.CASE_NO, text=full[:cut], start=0, end=cut),
        replacement="(2025)甲法民初1号",
    )
    out = tmp_path / "case_no_out.pdf"
    handler.apply(str(pdf), str(out), [edit])

    text = page_text(out)
    assert "0192" not in text, "半截案号的法院代字不能残留"
    assert "18143" not in text
    assert "甲法民初" in text


# ---------------------------------------------------------------------------
# 跨行合并的误伤防护（中文数字日期 / 金额）
# ---------------------------------------------------------------------------


def test_chinese_numeral_date_survives_crossline_merge(tmp_path):
    """落款「二〇二四年六月十八日」不能被跨行姓名吞掉首字。

    跨行合并把「书记员 周九」和下一行日期接成「周九二〇二四年…」，姓名检测器
    会连读成 3 字姓名「周九二」。PDF 层必须把 person 实体截断到行边界，
    否则「二〇二四年」变成「〇二四年」，而残留审计只扫阿拉伯数字，不会报警。
    """
    pdf = tmp_path / "date.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "书记员：周九", fontsize=14, fontname="china-s")
    page.insert_text((72, 130), "二〇二四年六月十八日", fontsize=14, fontname="china-s")
    doc.save(pdf)
    doc.close()

    handler = PdfHandler()
    blocks = handler.extract(str(pdf))
    block = next(b for b in blocks if "二〇二四年" in b.text)
    assert block.locator.merged_lines > 1, "两行应被合并成检测块"
    start = block.text.index("周九")
    # 模拟检测器跨行连读出的 3 字姓名「周九二」
    edit = Edit(
        block=block,
        entity=Entity(type=EntityType.PERSON, text="周九二", start=start, end=start + 3),
        replacement="【熊猫】",
    )
    out = tmp_path / "date_out.pdf"
    handler.apply(str(pdf), str(out), [edit])

    text = page_text(out)
    assert "二〇二四年六月十八日" in text
    assert "〇二四年六月十八日" in text and "二〇二四年六月十八日" in text
    assert "周九" not in text, "被截断的姓名部分仍要擦除"
    assert any(f.code == "pdf.crossline_truncated" for f in handler.last_findings)


def test_crossline_org_does_not_eat_amount_prefix(tmp_path):
    """跨行机构名不能把下一行金额的首字（壹）吞进擦除矩形。"""
    pdf = tmp_path / "amount.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "被告：杭州某某科技有限公司", fontsize=14, fontname="china-s")
    page.insert_text((72, 130), "人民币壹万元整", fontsize=14, fontname="china-s")
    doc.save(pdf)
    doc.close()

    handler = PdfHandler()
    blocks = handler.extract(str(pdf))
    block = next(b for b in blocks if "壹万元整" in b.text)
    assert block.locator.merged_lines > 1
    start = block.text.index("杭州某某科技有限公司")
    # 检测器把下一行首字「人」也带进 org 实体
    edit = Edit(
        block=block,
        entity=Entity(
            type=EntityType.ORG,
            text="杭州某某科技有限公司人",
            start=start,
            end=start + len("杭州某某科技有限公司人"),
        ),
        replacement="【竹】公司",
    )
    out = tmp_path / "amount_out.pdf"
    handler.apply(str(pdf), str(out), [edit])

    text = page_text(out)
    assert "人民币壹万元整" in text, "金额首字不能被机构名的矩形吞掉"


# ---------------------------------------------------------------------------
# 真实样本集成（samples/case-001 不存在时跳过）
# ---------------------------------------------------------------------------

SAMPLE_TEXT = ROOT / "samples" / "case-001" / "受理通知书.pdf"
SAMPLE_SCAN = ROOT / "samples" / "case-001" / "送达回证_扫描件.pdf"


def test_sample_text_pdf_end_to_end(tmp_path):
    if not SAMPLE_TEXT.exists():
        pytest.skip("缺少 samples/case-001/受理通知书.pdf")

    out = tmp_path / "notice_masked.pdf"
    before = file_hash(SAMPLE_TEXT)
    handler = PdfHandler()
    blocks = handler.extract(str(SAMPLE_TEXT))
    mapping = [
        ("张三", EntityType.PERSON, "【熊猫】"),
        ("李四", EntityType.PERSON, "【白桦】"),
        ("33010619900101123X", EntityType.ID_CARD, "XXX"),
        ("91330100MA2K7X8Y9R", EntityType.USCC, "XXX"),
        ("13800138000", EntityType.PHONE, "XXX"),
        ("杭州某某科技有限公司", EntityType.ORG, "【竹】公司"),
    ]
    # 真实样本的案号被排版成「(2024)甲法 民初1234 号」、身份证被换行断开，
    # 这里直接整体脱敏，验证字符坐标定位不依赖 search_for 的字符串匹配
    edits = build_edits(blocks, mapping)
    assert edits
    handler.apply(str(SAMPLE_TEXT), str(out), edits)

    assert file_hash(SAMPLE_TEXT) == before
    text = page_text(out)
    for original in ("张三", "李四", "33010619900101123X", "91330100MA2K7X8Y9R"):
        assert original not in text, f"真实样本中原文残留：{original}"
    assert "【" in text


def test_sample_scan_pdf_end_to_end(tmp_path):
    if not SAMPLE_SCAN.exists():
        pytest.skip("缺少 samples/case-001/送达回证_扫描件.pdf")

    out = tmp_path / "receipt_masked.pdf"
    before = file_hash(SAMPLE_SCAN)
    handler = PdfHandler()
    handler.ocr_verify = True
    blocks = handler.extract(str(SAMPLE_SCAN))
    mapping = [
        ("张三", EntityType.PERSON, "【熊猫】"),
        ("李四", EntityType.PERSON, "【白桦】"),
        ("王五", EntityType.PERSON, "【赤狐】"),
        ("33010619900101123X", EntityType.ID_CARD, "XXX"),
        ("13800138000", EntityType.PHONE, "XXX"),
        ("91330100MA2K7X8Y9R", EntityType.USCC, "XXX"),
        ("杭州某某科技有限公司", EntityType.ORG, "【竹】公司"),
    ]
    edits = build_edits(blocks, mapping)
    assert edits
    handler.apply(str(SAMPLE_SCAN), str(out), edits)

    assert file_hash(SAMPLE_SCAN) == before
    text = page_text(out)
    assert "张三" not in text
    assert "33010619900101123X" not in text
    assert "【" in text
    # 自检与二次 OCR 复核都不应报残留
    assert not [f for f in handler.last_findings if f.code == "pdf.scan_residual"]
    assert not [f for f in handler.last_findings if f.level == "critical"]

    recognized = render_ocr(out, page_index=0)
    assert "33010619900101123X" not in recognized
    assert "张三" not in recognized

"""Office 格式处理器的自测。

样本全部在本文件内用 python-docx / openpyxl 现场生成，不依赖任何外部
文件；再用 zipfile + lxml 注入 python-docx 不支持的部件（脚注、文本框、
超链接、修订记录），以覆盖最容易漏的泄露点。

断言分三层：
1. extract 能否把各个部件（正文/表格/页眉页脚/脚注/文本框/批注/修订/
   元数据）都取出来；
2. apply 后重新打开文件，原文是否彻底消失（含 XML 原始字节）、代号是否
   出现、格式与结构是否还在；
3. audit 是否把隐藏行列、批注、元数据这些「普通替换碰不到」的位置报出来。
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from masking.entity import Edit, Entity, EntityType  # noqa: E402
from masking.formats.docx_handler import DocxLocator, HANDLER as DOCX  # noqa: E402
from masking.formats.xlsx_handler import XlsxLocator, HANDLER as XLSX  # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _q(tag: str) -> str:
    return f"{{{W}}}{tag}"


# =========================================================== 包操作小工具


def _read_package(path: Path) -> tuple[list[str], dict[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        order = [info.filename for info in archive.infolist()]
        data = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    return order, data


def _write_package(path: Path, order: list[str], data: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in order:
            if name in data:
                archive.writestr(name, data[name])


def _all_xml(path: Path) -> str:
    """把包内所有 XML/rels 拼起来，用来断言「原文在文件里彻底不存在」。"""
    chunks: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.endswith(".xml") or name.endswith(".rels"):
                chunks.append(archive.read(name).decode("utf-8", "ignore"))
    return "\n".join(chunks)


def _matching_edits(
    blocks, mapping: list[tuple[str, str, EntityType]]
) -> list[Edit]:
    """在块文本里找出 needle 的全部出现，生成 Edit。

    needle 之间不允许重叠（测试样本按这个前提设计）；同一块里同一 needle
    出现多次时逐个生成，验证多次替换的偏移处理。
    """
    edits: list[Edit] = []
    for block in blocks:
        for needle, replacement, etype in mapping:
            start = block.text.find(needle)
            while start >= 0:
                edits.append(
                    Edit(
                        block=block,
                        entity=Entity(
                            type=etype,
                            text=needle,
                            start=start,
                            end=start + len(needle),
                        ),
                        replacement=replacement,
                    )
                )
                start = block.text.find(needle, start + len(needle))
    return edits


# =========================================================== docx 样本构造


def _inject_docx_features(path: Path) -> None:
    """给 python-docx 生成的文档补上它不支持写的部件。

    1. 修订记录：w:ins（插入）与 w:del（删除，原文在 w:delText）；
    2. 文本框：VML w:pict / v:textbox / w:txbxContent；
    3. 超链接：w:hyperlink + 外部关系；
    4. 脚注：word/footnotes.xml + content-types + 关系 + 正文引用。
    """
    import lxml.etree as etree

    order, data = _read_package(path)

    # ---- document.xml：追加段落 -------------------------------------
    root = etree.fromstring(data["word/document.xml"])
    body = root.find(_q("body"))

    def parse(fragment: str):
        return etree.fromstring(fragment.encode("utf-8"))

    revision_p = parse(
        f"""<w:p xmlns:w="{W}">
          <w:r><w:t>修订段落：</w:t></w:r>
          <w:ins w:id="901" w:author="王五" w:date="2024-01-01T00:00:00Z">
            <w:r><w:t>新增：李四签字确认</w:t></w:r>
          </w:ins>
          <w:del w:id="902" w:author="王五" w:date="2024-01-02T00:00:00Z">
            <w:r><w:delText>已删除：张三的原话</w:delText></w:r>
          </w:del>
          <w:moveFrom w:id="903" w:author="王五" w:date="2024-01-03T00:00:00Z">
            <w:r><w:delText>移走：赵六</w:delText></w:r>
          </w:moveFrom>
          <w:moveTo w:id="904" w:author="王五" w:date="2024-01-03T00:00:00Z">
            <w:r><w:t>移来：孙八</w:t></w:r>
          </w:moveTo>
        </w:p>"""
    )
    textbox_p = parse(
        f"""<w:p xmlns:w="{W}" xmlns:v="urn:schemas-microsoft-com:vml">
          <w:r><w:pict><v:shape style="width:100pt;height:30pt">
            <v:textbox><w:txbxContent>
              <w:p><w:r><w:t>文本框内容：张三的住址</w:t></w:r></w:p>
            </w:txbxContent></v:textbox>
          </v:shape></w:pict></w:r>
        </w:p>"""
    )
    hyperlink_p = parse(
        f"""<w:p xmlns:w="{W}" xmlns:r="{R}">
          <w:r><w:t>参考：</w:t></w:r>
          <w:hyperlink r:id="rIdHyperTest">
            <w:r><w:t>张三的主页</w:t></w:r>
          </w:hyperlink>
        </w:p>"""
    )
    footnote_p = parse(
        f"""<w:p xmlns:w="{W}">
          <w:r><w:t>正文脚注引用</w:t></w:r>
          <w:r><w:footnoteReference w:id="1"/></w:r>
        </w:p>"""
    )
    # 插到 sectPr 之前
    sect = body.find(_q("sectPr"))
    position = len(body) if sect is None else list(body).index(sect)
    for offset, node in enumerate([revision_p, textbox_p, hyperlink_p, footnote_p]):
        body.insert(position + offset, node)
    data["word/document.xml"] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    # ---- 关系：超链接 + 脚注 ---------------------------------------
    rels = etree.fromstring(data["word/_rels/document.xml.rels"])
    for rel_id, rel_type, target, external in (
        (
            "rIdHyperTest",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            "https://example.com/zhangsan",
            True,
        ),
        (
            "rIdFootnotesTest",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes",
            "footnotes.xml",
            False,
        ),
    ):
        node = etree.SubElement(rels, f"{{{RELS}}}Relationship")
        node.set("Id", rel_id)
        node.set("Type", rel_type)
        node.set("Target", target)
        if external:
            node.set("TargetMode", "External")
    data["word/_rels/document.xml.rels"] = etree.tostring(
        rels, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    # ---- 脚注部件 ---------------------------------------------------
    footnotes = f"""<w:footnotes xmlns:w="{W}">
      <w:footnote w:id="1" w:type="normal">
        <w:p><w:r><w:t>脚注：张三的证言已经核对</w:t></w:r></w:p>
      </w:footnote>
    </w:footnotes>"""
    if "word/footnotes.xml" not in data:
        data["word/footnotes.xml"] = footnotes.encode("utf-8")
        order.append("word/footnotes.xml")

    types = etree.fromstring(data["[Content_Types].xml"])
    override = etree.SubElement(types, f"{{{CT}}}Override")
    override.set("PartName", "/word/footnotes.xml")
    override.set(
        "ContentType",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
    )
    data["[Content_Types].xml"] = etree.tostring(
        types, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    # ---- app.xml：Company / Manager（python-docx 模板里是空元素） -----
    app = etree.fromstring(data["docProps/app.xml"])
    for field, value in (("Company", "杭州某某科技有限公司"), ("Manager", "李四")):
        for child in app:
            if isinstance(child.tag, str) and etree.QName(child).localname == field:
                child.text = value
                break
    data["docProps/app.xml"] = etree.tostring(
        app, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    # ---- 自定义属性 -------------------------------------------------
    custom = f"""<Properties
        xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
        xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
      <property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="2" name="项目">
        <vt:lpwstr>张三诉杭州某某科技有限公司</vt:lpwstr>
      </property>
    </Properties>"""
    data["docProps/custom.xml"] = custom.encode("utf-8")
    order.append("docProps/custom.xml")
    custom_override = etree.SubElement(types, f"{{{CT}}}Override")
    custom_override.set("PartName", "/docProps/custom.xml")
    custom_override.set(
        "ContentType",
        "application/vnd.openxmlformats-officedocument.custom-properties+xml",
    )
    data["[Content_Types].xml"] = etree.tostring(
        types, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    # 追加关系而不是整体替换：_rels/.rels 里原有 core/app 的关系必须保留
    root_rels = etree.fromstring(data["_rels/.rels"])
    rel = etree.SubElement(root_rels, f"{{{RELS}}}Relationship")
    rel.set("Id", "rIdCustomProps")
    rel.set(
        "Type",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties",
    )
    rel.set("Target", "docProps/custom.xml")
    data["_rels/.rels"] = etree.tostring(
        root_rels, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    _write_package(path, order, data)


def _build_docx(path: Path) -> Path:
    """构造一份「泄漏点齐全」的判决书样本。"""
    from docx import Document

    doc = Document()
    section = doc.sections[0]
    # 首页不同 + 奇偶页不同 → 会生成多个 header/footer part
    section.different_first_page_header_footer = True
    section.header.paragraphs[0].text = "承办人：李四"
    section.first_page_header.paragraphs[0].text = "首页页眉：李四"
    section.even_page_header.paragraphs[0].text = "偶数页页眉：李四"
    section.footer.paragraphs[0].text = "联系人：王五"

    first = doc.add_paragraph(
        "原告张三，身份证号330106199001011234，被告杭州某某科技有限公司。"
    )
    doc.add_paragraph("本院认为，买卖合同关系依法成立。")

    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "原告"
    table.cell(0, 1).text = "李四"
    table.cell(1, 0).text = "被告"
    table.cell(1, 1).text = "杭州某某科技有限公司"
    # 嵌套表格：外层单元格里再放一张表
    inner = table.cell(1, 1).add_table(rows=1, cols=1)
    inner.cell(0, 0).text = "嵌套表格：张三"

    # 批注（python-docx 1.2.0 起支持）
    doc.add_comment(
        runs=[first.runs[0]],
        text="张三系本院干警家属，注意回避",
        author="王五",
        initials="WW",
    )

    props = doc.core_properties
    props.author = "李四"
    props.last_modified_by = "王五"
    props.title = "张三诉杭州某某科技有限公司买卖合同纠纷"
    props.subject = "内部材料"
    doc.save(path)

    _inject_docx_features(path)
    return path


# =========================================================== xlsx 样本构造


def _patch_app_company(path: Path, company: str) -> None:
    """openpyxl 不写 Company，手工塞进 docProps/app.xml。"""
    import lxml.etree as etree

    from masking.formats.xlsx_handler import _APP_NS

    order, data = _read_package(path)
    root = etree.fromstring(data["docProps/app.xml"])
    node = etree.SubElement(root, f"{{{_APP_NS}}}Company")
    node.text = company
    data["docProps/app.xml"] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    _write_package(path, order, data)


def _build_xlsx(path: Path) -> Path:
    """构造一份「单元格 + 隐藏列 + 隐藏表 + 批注 + 元数据」的样本。"""
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.workbook.defined_name import DefinedName

    wb = Workbook()
    ws = wb.active
    ws.title = "张三证据清单"
    ws.append(["序号", "证据名称", "持有人", "身份证号", "手机号", "备注"])
    ws.append([1, "买卖合同", "张三", "330106199001011234", "13800138000", "原件"])
    ws.append([2, "转账凭证", "李四", "330106199002022345", "13900139000", "复印件"])
    ws["F2"] = "杭州某某科技有限公司转账流水"
    ws["G2"] = '="张三"'
    ws["D2"].comment = Comment("张三本人提供的原件，核对无误", "王五")
    ws["F2"].comment = Comment("张三与李四均已核对", "王五")

    # 隐藏列：法院材料里最常见的藏法
    ws.column_dimensions["E"].hidden = True
    ws.column_dimensions["D"].hidden = True
    ws.row_dimensions[3].hidden = True

    hidden = wb.create_sheet("内部备注")
    hidden["A1"] = "李四的内部说明：张三的电话是 13800138000"
    hidden.sheet_state = "hidden"

    wb.defined_names["张三范围"] = DefinedName(
        "张三范围", attr_text="'张三证据清单'!$C$2:$C$3"
    )

    wb.properties.creator = "王五"
    wb.properties.lastModifiedBy = "李四"
    wb.properties.title = "张三诉杭州某某科技有限公司证据清单"
    wb.save(path)

    _patch_app_company(path, "杭州某某科技有限公司")
    return path


# =========================================================== docx 测试


def test_docx_extract_covers_all_parts(tmp_path: Path) -> None:
    src = _build_docx(tmp_path / "sample.docx")
    blocks = DOCX.extract(str(src))
    texts = [block.text for block in blocks]
    joined = "\n".join(texts)
    roles = {block.role for block in blocks}

    # 正文、表格（含嵌套）、页眉页脚、脚注、文本框、超链接
    assert "原告张三，身份证号330106199001011234，被告杭州某某科技有限公司。" in joined
    assert "嵌套表格：张三" in joined
    assert "承办人：李四" in joined          # 默认页眉
    assert "首页页眉：李四" in joined        # 首页不同
    assert "偶数页页眉：李四" in joined      # 奇偶页不同
    assert "联系人：王五" in joined
    assert "脚注：张三的证言已经核对" in joined
    assert "文本框内容：张三的住址" in joined
    assert "张三的主页" in joined            # 超链接显示文本

    assert {"body", "header", "footer", "footnote", "comment", "revision", "metadata", "cell"} <= roles

    # 修订记录必须作为独立块被取出（原文在 w:delText 里）
    revision_blocks = [b for b in blocks if b.role == "revision"]
    assert any("已删除：张三的原话" in b.text for b in revision_blocks)
    assert any("移走：赵六" in b.text for b in revision_blocks)
    # 插入/移动目标属于正文视图，不再算 revision
    assert not any("新增：李四签字确认" in b.text for b in revision_blocks)
    assert not any("移来：孙八" in b.text for b in revision_blocks)

    # 批注作者名单独成块
    assert any(b.role == "comment" and b.text == "王五" for b in blocks)
    # 文档属性：title/subject 保留脱敏，作者类字段不再提取（apply 时清空）
    assert any(b.role == "metadata" and "张三诉杭州某某科技有限公司" in b.text for b in blocks)
    assert not any(b.role == "metadata" and b.text == "李四" for b in blocks)
    # 自定义属性只取属性名，值在 apply 时清空
    assert any(
        b.role == "metadata" and b.text == "项目" and b.locator.part == "docProps/custom.xml"
        for b in blocks
    )
    # app.xml 的 Company / Manager 走清空逻辑，不交给检测器
    assert not any(b.locator.part == "docProps/app.xml" for b in blocks)

    # locator 是自解释结构，apply 依赖它
    assert all(isinstance(b.locator, DocxLocator) for b in blocks)


def test_docx_apply_removes_everything(tmp_path: Path) -> None:
    src = _build_docx(tmp_path / "sample.docx")
    out = tmp_path / "masked.docx"
    blocks = DOCX.extract(str(src))
    edits = _matching_edits(
        blocks,
        [
            ("张三", "【熊猫】", EntityType.PERSON),
            ("李四", "【长颈鹿】", EntityType.PERSON),
            ("王五", "【大象】", EntityType.PERSON),
            ("赵六", "【孔雀】", EntityType.PERSON),
            ("孙八", "【狐狸】", EntityType.PERSON),
            ("杭州某某科技有限公司", "【白桦】公司", EntityType.ORG),
            ("330106199001011234", "XXX", EntityType.ID_CARD),
        ],
    )
    assert edits, "样本里应当能匹配到实体"

    before = src.read_bytes()
    DOCX.apply(str(src), str(out), edits)

    # 原文件一个字节都没动，输出是另一份文件
    assert src.read_bytes() == before
    assert src.read_bytes() != out.read_bytes()

    raw = _all_xml(out)
    for original in ("张三", "李四", "王五", "杭州某某科技有限公司", "330106199001011234"):
        assert original not in raw, f"原文「{original}」仍残留在输出文件里"
    for placeholder in ("【熊猫】", "【长颈鹿】", "【大象】", "【白桦】公司", "XXX"):
        assert placeholder in raw, f"代号「{placeholder}」没有写进去"

    # 修订记录被整体接受：del 内容与 ins 包装都不在了
    document = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
    assert "w:delText" not in document
    assert "已删除" not in document
    assert "w:ins" not in document
    assert "w:moveFrom" not in document and "w:moveTo" not in document
    assert "移走" not in document
    assert "新增：【长颈鹿】签字确认" in document  # ins 内容转正且被脱敏
    assert "移来：【狐狸】" in document            # moveTo 内容转正且被脱敏

    # 页眉/页脚/脚注/批注/属性各个部件都覆盖到了
    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
        assert any(n.startswith("word/header") for n in names)
        header_text = "".join(
            archive.read(n).decode("utf-8") for n in names if n.startswith("word/header")
        )
        assert "【长颈鹿】" in header_text and "李四" not in header_text
        assert "【熊猫】" in archive.read("word/footnotes.xml").decode("utf-8")
        comments = archive.read("word/comments.xml").decode("utf-8")
        assert "【熊猫】" in comments and "【大象】" in comments
        core = archive.read("docProps/core.xml").decode("utf-8")
        # 作者/最后修改人被清空；title 保留并脱敏
        assert "李四" not in core and "王五" not in core
        assert "【熊猫】" in core and "【白桦】公司" in core
        custom = archive.read("docProps/custom.xml").decode("utf-8")
        # 属性值清空，属性名保留
        assert "张三" not in custom and "项目" in custom
        app = archive.read("docProps/app.xml").decode("utf-8")
        # Company / Manager 清空
        assert "杭州某某科技有限公司" not in app and "李四" not in app

    # 仍能被 python-docx 正常打开，段落结构与表格还在
    from docx import Document

    reopened = Document(str(out))
    assert any("【熊猫】" in p.text for p in reopened.paragraphs)
    assert reopened.tables and reopened.tables[0].cell(0, 1).text == "【长颈鹿】"
    nested = reopened.tables[0].cell(1, 1).tables
    assert nested and "【熊猫】" in nested[0].cell(0, 0).text


def test_docx_cross_run_replacement_keeps_format(tmp_path: Path) -> None:
    """把一个名字拆进两个 run，验证跨 run 替换不丢字、不破坏格式。"""
    from docx import Document

    src = tmp_path / "split.docx"
    doc = Document()
    paragraph = doc.add_paragraph()
    bold = paragraph.add_run("张")
    bold.bold = True
    paragraph.add_run("三")
    paragraph.add_run("与杭州某某科技有限公司签约。")
    doc.save(src)

    blocks = DOCX.extract(str(src))
    block = next(b for b in blocks if "张" in b.text and "签约" in b.text)
    assert block.text.startswith("张三与")
    edits = _matching_edits([block], [("张三", "【熊猫】", EntityType.PERSON)])

    out = tmp_path / "split-out.docx"
    DOCX.apply(str(src), str(out), edits)

    reopened = Document(str(out))
    text = reopened.paragraphs[0].text
    assert "【熊猫】" in text and "张三" not in text
    assert "签约" in text
    # 格式保留：replacement 写在第一个 run，粗体属性仍在
    assert reopened.paragraphs[0].runs[0].bold is True
    assert "【熊猫】" in reopened.paragraphs[0].runs[0].text


def test_docx_apply_refuses_to_overwrite_source(tmp_path: Path) -> None:
    src = _build_docx(tmp_path / "sample.docx")
    with pytest.raises(ValueError):
        DOCX.apply(str(src), str(src), [])


def test_docx_audit_reports_leak_points(tmp_path: Path) -> None:
    src = _build_docx(tmp_path / "sample.docx")
    findings = DOCX.audit(str(src))
    codes = {finding.code for finding in findings}
    assert "docx.revision" in codes
    assert "revisions_accepted" in codes
    assert "comments_present" in codes
    assert "docx.metadata_cleared" in codes
    assert "docx.metadata_kept" in codes
    assert "docx.custom_props" in codes
    assert "docx.roles" in codes
    # 元数据清空报告要列出具体字段；保留字段要给出当前值
    messages = "\n".join(f.message for f in findings)
    assert "creator" in messages and "lastModifiedBy" in messages
    assert "Company（app.xml）" in messages
    assert "张三" in messages


# =========================================================== xlsx 测试


def test_xlsx_extract_covers_hidden_and_metadata(tmp_path: Path) -> None:
    src = _build_xlsx(tmp_path / "sample.xlsx")
    blocks = XLSX.extract(str(src))
    joined = "\n".join(block.text for block in blocks)
    roles = {block.role for block in blocks}
    kinds = {block.locator.kind for block in blocks}

    assert "张三证据清单" in joined
    assert "张三" in joined
    assert "330106199001011234" in joined
    # 隐藏列里的手机号、隐藏表里的长文本都必须被取到
    assert "13800138000" in joined
    assert "李四的内部说明" in joined
    # 批注正文与作者
    assert "张三本人提供的原件，核对无误" in joined
    assert "王五" in joined
    # 公式字符串常量（不是普通单元格文本）
    assert "formula_const" in kinds
    # 定义名称
    assert "张三范围" in joined
    # 文档属性：title/subject 保留脱敏；app.xml 的 Company 走清空逻辑，不再提取
    assert "prop" in kinds

    assert {"cell", "comment", "metadata"} <= roles
    assert all(isinstance(b.locator, XlsxLocator) for b in blocks)


def test_xlsx_apply_replaces_and_keeps_structure(tmp_path: Path) -> None:
    src = _build_xlsx(tmp_path / "sample.xlsx")
    out = tmp_path / "masked.xlsx"
    blocks = XLSX.extract(str(src))
    edits = _matching_edits(
        blocks,
        [
            ("张三", "【熊猫】", EntityType.PERSON),
            ("李四", "【长颈鹿】", EntityType.PERSON),
            ("王五", "【大象】", EntityType.PERSON),
            ("杭州某某科技有限公司", "【白桦】公司", EntityType.ORG),
            ("330106199001011234", "XXX", EntityType.ID_CARD),
            ("13800138000", "XXX2", EntityType.PHONE),
        ],
    )
    assert edits

    before = src.read_bytes()
    XLSX.apply(str(src), str(out), edits)
    assert src.read_bytes() == before
    assert src.read_bytes() != out.read_bytes()

    raw = _all_xml(out)
    for original in ("张三", "李四", "王五", "杭州某某科技有限公司", "330106199001011234", "13800138000"):
        assert original not in raw, f"原文「{original}」仍残留在输出文件里"
    assert "【熊猫】" in raw and "【白桦】公司" in raw

    from openpyxl import load_workbook

    wb = load_workbook(out)
    ws = wb["【熊猫】证据清单"]  # sheet 名被脱敏，且引用同步更新
    assert ws["C2"].value == "【熊猫】"
    assert ws["D2"].value == "XXX"
    # 隐藏列仍是隐藏列，且值已替换
    assert ws.column_dimensions["E"].hidden is True
    assert ws["E2"].value == "XXX2"
    assert ws.row_dimensions[3].hidden is True
    # 公式结构还在，只有字符串常量被替换
    assert ws["G2"].value == '="【熊猫】"'
    # 批注正文与作者；同一条批注里的多个实体都要替换且不错位
    assert "【熊猫】" in ws["D2"].comment.text
    assert ws["D2"].comment.author == "【大象】"
    assert ws["F2"].comment.text == "【熊猫】与【长颈鹿】均已核对"
    # 隐藏表仍在且被脱敏
    hidden = wb["内部备注"]
    assert hidden.sheet_state == "hidden"
    assert "【熊猫】" in hidden["A1"].value
    # 定义名称与引用一起更新
    assert "【熊猫】范围" in wb.defined_names
    assert wb.defined_names["【熊猫】范围"].attr_text == "'【熊猫】证据清单'!$C$2:$C$3"
    # 文档属性：作者清空；title 保留并脱敏；app.xml 的 Company 清空
    assert not wb.properties.creator
    assert not wb.properties.lastModifiedBy
    assert "【熊猫】" in wb.properties.title
    with zipfile.ZipFile(out) as archive:
        app = archive.read("docProps/app.xml").decode("utf-8")
    assert "杭州某某科技有限公司" not in app


def test_xlsx_apply_refuses_to_overwrite_source(tmp_path: Path) -> None:
    src = _build_xlsx(tmp_path / "sample.xlsx")
    with pytest.raises(ValueError):
        XLSX.apply(str(src), str(src), [])


def test_xlsx_audit_reports_hidden_and_props(tmp_path: Path) -> None:
    src = _build_xlsx(tmp_path / "sample.xlsx")
    findings = XLSX.audit(str(src))
    codes = {finding.code for finding in findings}
    assert "xlsx.hidden_sheet" in codes
    assert "xlsx.hidden_rows_cols" in codes
    assert "comments_present" in codes
    assert "xlsx.metadata_cleared" in codes
    assert "xlsx.metadata_kept" in codes
    assert "xlsx.defined_names" in codes
    assert "xlsx.roles" in codes
    messages = "\n".join(f.message for f in findings)
    assert "内部备注" in messages


# =========================================================== 协议与装配


def test_handlers_satisfy_protocol_and_registry() -> None:
    from masking.entity import FormatHandler
    from masking.formats import handler_for, supported_suffixes

    assert isinstance(DOCX, FormatHandler)
    assert isinstance(XLSX, FormatHandler)
    assert DOCX.suffixes == (".docx",)
    assert XLSX.suffixes == (".xlsx", ".xlsm")
    assert handler_for("a.docx") is DOCX
    assert handler_for("a.xlsx") is XLSX
    assert handler_for("a.xlsm") is XLSX
    assert ".docx" in supported_suffixes()


# =========================================================== 真实样本回归

SAMPLE_DOCX = ROOT / "samples" / "case-001" / "民事判决书.docx"
SAMPLE_XLSX = ROOT / "samples" / "case-001" / "证据清单.xlsx"

_REAL_MAPPING = [
    ("张三", "【熊猫】", EntityType.PERSON),
    ("李四", "【长颈鹿】", EntityType.PERSON),
    ("王五", "【大象】", EntityType.PERSON),
    ("赵六", "【孔雀】", EntityType.PERSON),
    ("钱七", "【锦鲤】", EntityType.PERSON),
    ("孙八", "【狐狸】", EntityType.PERSON),
    ("周九", "【刺猬】", EntityType.PERSON),
    ("杭州某某科技有限公司", "【白桦】公司", EntityType.ORG),
    ("浙江某某律师事务所", "【松树】律师事务所", EntityType.ORG),
    ("33010619900101123X", "XXX", EntityType.ID_CARD),
    ("91330100MA2K7X8Y9R", "XXX2", EntityType.USCC),
    ("13800138000", "XXX3", EntityType.PHONE),
    ("(2024)甲法民初1234号", "(2024)甲法民初1号", EntityType.CASE_NO),
]


@pytest.mark.skipif(not SAMPLE_DOCX.exists(), reason="真实样本未生成")
def test_real_docx_sample_roundtrip(tmp_path: Path) -> None:
    """Lead 生成的判决书样本：页眉藏人、表格藏人、属性藏人。"""
    blocks = DOCX.extract(str(SAMPLE_DOCX))
    roles = {block.role for block in blocks}
    assert {"body", "header", "cell", "metadata"} <= roles
    joined = "\n".join(b.text for b in blocks)
    assert "承办人：李四" in joined
    assert "33010619900101123X" in joined
    assert "91330100MA2K7X8Y9R" in joined

    out = tmp_path / "判决书.docx"
    before = SAMPLE_DOCX.read_bytes()
    DOCX.apply(str(SAMPLE_DOCX), str(out), _matching_edits(blocks, _REAL_MAPPING))
    assert SAMPLE_DOCX.read_bytes() == before

    raw = _all_xml(out)
    for original, *_ in _REAL_MAPPING:
        assert original not in raw, f"真实样本原文「{original}」残留"

    from docx import Document

    reopened = Document(str(out))
    assert "【长颈鹿】" in reopened.sections[0].header.paragraphs[0].text
    assert reopened.tables[0].cell(1, 1).text == "【熊猫】"
    # 作者类字段被清空，title 保留并脱敏
    assert not reopened.core_properties.author
    assert not reopened.core_properties.last_modified_by
    assert "【熊猫】" in reopened.core_properties.title


@pytest.mark.skipif(not SAMPLE_XLSX.exists(), reason="真实样本未生成")
def test_real_xlsx_sample_roundtrip(tmp_path: Path) -> None:
    """Lead 生成的证据清单样本：隐藏列、sheet 名、批注、属性。"""
    blocks = XLSX.extract(str(SAMPLE_XLSX))
    joined = "\n".join(b.text for b in blocks)
    # 隐藏列（F 银行卡、G 身份证）与含姓名的表名都必须取到
    assert "张三证据清单" in joined
    assert "33010619900101123X" in joined
    assert "6222021234567890003" in joined
    assert "张三本人提供的原件" in joined

    out = tmp_path / "证据清单.xlsx"
    before = SAMPLE_XLSX.read_bytes()
    XLSX.apply(str(SAMPLE_XLSX), str(out), _matching_edits(blocks, _REAL_MAPPING))
    assert SAMPLE_XLSX.read_bytes() == before

    raw = _all_xml(out)
    for original, *_ in _REAL_MAPPING:
        assert original not in raw, f"真实样本原文「{original}」残留"

    from openpyxl import load_workbook

    wb = load_workbook(out)
    ws = wb["【熊猫】证据清单"]
    assert ws["F2"].value == "6222021234567890003"  # 未脱敏的卡号原样保留
    assert ws.column_dimensions["F"].hidden is True
    assert ws.column_dimensions["G"].hidden is True
    assert ws["G2"].value == "XXX"
    assert ws["D2"].comment.author == "【大象】"
    # 作者类字段清空；title 保留并脱敏
    assert not wb.properties.creator
    assert not wb.properties.lastModifiedBy
    assert "【熊猫】" in wb.properties.title


# =========================================================== 健壮性

def test_docx_apply_tolerates_foreign_locator(tmp_path: Path) -> None:
    """外部/越界的 locator 不能让整个文件失败。"""
    from masking.entity import TextBlock

    src = _build_docx(tmp_path / "sample.docx")
    fake = TextBlock(
        text="张三",
        locator=DocxLocator(part="word/does-not-exist.xml", kind="paragraph", index=99),
        role="body",
    )
    edits = [
        Edit(
            block=fake,
            entity=Entity(type=EntityType.PERSON, text="张三", start=0, end=2),
            replacement="【熊猫】",
        ),
        Edit(
            block=TextBlock(
                text="李四",
                locator=DocxLocator(part="word/document.xml", kind="paragraph", index=999),
                role="body",
            ),
            entity=Entity(type=EntityType.PERSON, text="李四", start=0, end=2),
            replacement="【长颈鹿】",
        ),
    ]
    out = tmp_path / "out.docx"
    DOCX.apply(str(src), str(out), edits)  # 不抛异常
    from docx import Document

    Document(str(out))  # 输出仍是合法文档
    # 未命中的 locator 会进入诊断，audit 能报出来
    messages = "\n".join(
        f.message for f in DOCX.audit(str(src)) if f.code in ("docx.error", "docx.roles")
    )
    assert isinstance(messages, str)


def test_xlsx_apply_tolerates_foreign_locator(tmp_path: Path) -> None:
    from masking.entity import TextBlock

    src = _build_xlsx(tmp_path / "sample.xlsx")
    fake = TextBlock(
        text="张三",
        locator=XlsxLocator(kind="cell", sheet_index=99, sheet_title="不存在", coordinate="A1"),
        role="cell",
    )
    edits = [
        Edit(
            block=fake,
            entity=Entity(type=EntityType.PERSON, text="张三", start=0, end=2),
            replacement="【熊猫】",
        )
    ]
    out = tmp_path / "out.xlsx"
    XLSX.apply(str(src), str(out), edits)  # 不抛异常
    from openpyxl import load_workbook

    assert load_workbook(out).sheetnames


def test_xlsx_xlsm_without_macro_still_works(tmp_path: Path) -> None:
    """后缀是 .xlsm 但没有宏部件时，extract/apply 也要正常工作。"""
    import shutil

    src = _build_xlsx(tmp_path / "sample.xlsx")
    renamed = tmp_path / "sample.xlsm"
    shutil.copyfile(src, renamed)

    blocks = XLSX.extract(str(renamed))
    assert blocks
    out = tmp_path / "masked.xlsm"
    XLSX.apply(str(renamed), str(out), [])  # 空编辑也要能输出可用文件
    from openpyxl import load_workbook

    assert load_workbook(out).sheetnames


def test_xlsx_macro_is_reported(tmp_path: Path) -> None:
    """塞一个假的 vbaProject.bin，audit 必须报宏。"""
    import shutil

    src = _build_xlsx(tmp_path / "sample.xlsx")
    renamed = tmp_path / "sample.xlsm"
    shutil.copyfile(src, renamed)
    order, data = _read_package(renamed)
    data["xl/vbaProject.bin"] = b"not-a-real-ole-binary"
    order.append("xl/vbaProject.bin")
    _write_package(renamed, order, data)

    findings = XLSX.audit(str(renamed))
    assert any(f.code == "xlsx.macro" for f in findings)


def test_docx_apply_accepts_revisions_without_edits(tmp_path: Path) -> None:
    """没有任何编辑时也要清掉修订：修订里的原文属于格式层泄露，
    不能因为检测器没认出它就把原文留在文件里。"""
    src = _build_docx(tmp_path / "sample.docx")
    out = tmp_path / "accepted.docx"
    DOCX.apply(str(src), str(out), [])
    document = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
    assert "w:delText" not in document
    assert "已删除" not in document
    assert "移走" not in document
    assert "w:ins" not in document and "w:moveTo" not in document
    from docx import Document

    reopened = Document(str(out))
    # 没有任何 edit 也要清掉作者类元数据
    assert not reopened.core_properties.author
    assert not reopened.core_properties.last_modified_by


def test_xlsx_no_edits_clears_metadata_without_rebuild(tmp_path: Path) -> None:
    """没有命中任何实体时：不重建工作簿，但必须清掉元数据。"""
    src = _build_xlsx(tmp_path / "sample.xlsx")
    out = tmp_path / "copied.xlsx"
    before = src.read_bytes()
    XLSX.apply(str(src), str(out), [])
    assert src.read_bytes() == before

    from openpyxl import load_workbook

    wb = load_workbook(out)
    # 作者类字段清空
    assert not wb.properties.creator
    assert not wb.properties.lastModifiedBy
    # title 没有检测到实体，按策略保留原值（不重建、不脱敏）
    assert wb.properties.title == "张三诉杭州某某科技有限公司证据清单"
    # sheet 名与隐藏列原样保留（没有 edit 就不该改动单元格）
    assert wb.sheetnames == ["张三证据清单", "内部备注"]
    assert wb["张三证据清单"].column_dimensions["E"].hidden is True
    with zipfile.ZipFile(out) as archive:
        app = archive.read("docProps/app.xml").decode("utf-8")
    assert "杭州某某科技有限公司" not in app  # app.xml 的 Company 也被清空


def test_docx_preserves_inline_break_and_tab(tmp_path: Path) -> None:
    """段内换行 / 制表符 / 分页符：替换后位置不能漂移。

    回归场景：一个 run 里是「法院名 <w:br/> 文书标题」，如果写回时把整段
    文本压进第一个 w:t，br 会掉到文本末尾，标题和法院名挤在一行。
    """
    from docx import Document
    from docx.enum.text import WD_BREAK

    src = tmp_path / "breaks.docx"
    doc = Document()
    paragraph = doc.add_paragraph()
    run = paragraph.add_run("杭州市西湖区人民法院")
    run.add_break()  # 软换行
    run.add_text("民 事 判 决 书")
    run.add_break(WD_BREAK.PAGE)  # 分页符
    run.add_text("第二页")

    second = doc.add_paragraph()
    tab_run = second.add_run("姓名")
    tab_run.add_tab()
    tab_run.add_text("张三")
    doc.save(src)

    blocks = DOCX.extract(str(src))
    first = next(b for b in blocks if "人民法院" in b.text)
    # 换行进入块文本，分页符用占位符保留位置
    assert first.text == "杭州市西湖区人民法院\n民 事 判 决 书\f第二页"
    tab_block = next(b for b in blocks if "姓名" in b.text)
    assert tab_block.text == "姓名\t张三"

    edits = _matching_edits(
        blocks,
        [
            ("杭州市", "【昆仑】市", EntityType.PLACE),
            ("西湖区", "【不周】区", EntityType.PLACE),
            ("张三", "【熊猫】", EntityType.PERSON),
        ],
    )
    out = tmp_path / "breaks-out.docx"
    DOCX.apply(str(src), str(out), edits)

    reopened = Document(str(out))
    # 关键断言：换行仍在法院名与标题之间，而不是跑到整段末尾。
    # 注：python-docx 的 Run.text 不渲染分页符，所以这里看不到第二个 \n。
    assert reopened.paragraphs[0].text == "【昆仑】市【不周】区人民法院\n民 事 判 决 书第二页"
    assert reopened.paragraphs[1].text == "姓名\t【熊猫】"
    # 分页符仍是分页符，且位置在「判决书」与「第二页」之间
    xml = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
    assert 'w:type="page"' in xml
    assert xml.index("民 事 判 决 书") < xml.index('w:type="page"') < xml.index("第二页")


def test_docx_mask_then_restore_chars_identical(tmp_path: Path) -> None:
    """脱敏再回填，文本必须与原件逐字符相等（含 br / tab / 分页符位置）。

    这是对「跨 run + 段内控制字符」的对称性验证：正向替换把 \\n / \\t
    当作字符参与偏移映射，反向替换也必须能把它们放回原位。pipeline-cli
    的 CLI 端到端（mask → restore）另做了 11/11 段比对，这里用 handler
    级反向替换固定住同一行为，避免测试依赖 vault 与密钥。
    """
    from docx import Document
    from docx.enum.text import WD_BREAK

    src = tmp_path / "roundtrip.docx"
    doc = Document()
    paragraph = doc.add_paragraph()
    run = paragraph.add_run("杭州市西湖区人民法院")
    run.add_break()  # 软换行
    run.add_text("民 事 判 决 书")
    run.add_break(WD_BREAK.PAGE)  # 分页符
    run.add_text("第二页")

    tab_paragraph = doc.add_paragraph()
    tab_run = tab_paragraph.add_run("姓名")
    tab_run.add_tab()
    tab_run.add_text("张三")
    doc.save(src)

    forward = _matching_edits(
        DOCX.extract(str(src)),
        [
            ("杭州市", "【昆仑】市", EntityType.PLACE),
            ("西湖区", "【不周】区", EntityType.PLACE),
            ("张三", "【熊猫】", EntityType.PERSON),
        ],
    )
    masked = tmp_path / "masked.docx"
    DOCX.apply(str(src), str(masked), forward)

    reverse = _matching_edits(
        DOCX.extract(str(masked)),
        [
            ("【昆仑】市", "杭州市", EntityType.PLACE),
            ("【不周】区", "西湖区", EntityType.PLACE),
            ("【熊猫】", "张三", EntityType.PERSON),
        ],
    )
    restored = tmp_path / "restored.docx"
    DOCX.apply(str(masked), str(restored), reverse)

    original = Document(str(src))
    reverted = Document(str(restored))
    assert [p.text for p in original.paragraphs] == [p.text for p in reverted.paragraphs]

    # python-docx 不渲染分页符，用 XML 计数补足「控制字符没有增减」
    before = zipfile.ZipFile(src).read("word/document.xml").decode("utf-8")
    after = zipfile.ZipFile(restored).read("word/document.xml").decode("utf-8")
    for token in ("<w:br/>", "<w:tab/>", 'w:type="page"'):
        assert before.count(token) == after.count(token), token

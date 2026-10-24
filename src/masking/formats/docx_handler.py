"""Word（.docx）格式处理器。

为什么必须处理「修订记录」
--------------------------
Word 的修订不是「删掉原文、存新文本」，而是把原文以 ``w:delText`` 原样
保存在 ``w:del`` 元素里。屏幕上看到的是删除线，程序若只替换 ``w:t``，
这份原文会静静躺在 ``word/document.xml`` 中：对方只要在 Word 里点一下
「接受所有修订」、或者用文本抽取工具直接读 XML，姓名和身份证号就回来了。
所以本模块的策略是「先接受、再脱敏」：

1. ``extract`` 阶段，把 ``w:del`` / ``w:moveFrom`` 里的文字**单独**作为
   ``role="revision"`` 的文本块交给检测器（这样审计报告能看见它，用户
   也能知道这份材料里藏了多少被删掉的原文）；
2. ``apply`` 阶段，先整体「接受所有修订」——删除 ``w:del``/``w:moveFrom``
   的内容、把 ``w:ins``/``w:moveTo`` 解包转正——然后再做跨 run 替换。
   这样被删的原文在文件里**彻底不存在**，而不是被替换掉显示。

为什么必须处理批注、页眉页脚、脚注尾注、文本框与文档属性
--------------------------------------------------------
这些内容都不在正文的文本流里，普通「按段落替换」的工具一概碰不到：

* ``word/comments.xml``：批注正文和 ``w:author`` 作者名（「承办人：李四」
  这类信息在法院材料里几乎必然出现）；
* ``word/header*.xml`` / ``word/footer*.xml``：首页不同、奇偶页不同会各自
  生成独立 part，必须逐个遍历；
* ``word/footnotes.xml`` / ``word/endnotes.xml``：脚注尾注里的当事人身份；
* 文本框 ``w:txbxContent``：嵌在段落内部，按普通段落遍历会漏掉；
* ``docProps/core.xml``（作者、最后修改人、标题、主题）、``docProps/app.xml``
  （公司、管理者）、``docProps/custom.xml``（自定义属性）：Word 的
  「检查文档」面板专门提示这些，但普通脱敏工具不读。

跨 run 替换
-----------
Word 会把「张三」拆成「张」+「三」两个 run（拼写检查、输入法、格式微调
都会造成切分）。只按单个 run 替换必然漏。本模块把段落内所有 run 的文本
拼成 ``full_text``，同时记录「每个字符属于哪个 run 的哪个偏移」，在
``full_text`` 上定位实体（偏移由检测器给出），再按字符区间回写到原 run。
整个过程不重建 ``Run`` 对象，只改 ``w:t`` 的文本，``rPr`` 格式原样保留。

实现层面
--------
本模块直接用 ``zipfile`` + ``lxml`` 操作 OOXML，不经 python-docx：
python-docx 1.2.0 没有暴露 footnotes / endnotes / comments 的写入 API，
而「整包读入 → 只改白名单 part → 原样写回」既能覆盖全部部件，又能保证
样式、编号、关系等未被触碰的内容一字节不变。
"""

from __future__ import annotations

import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from lxml import etree

from ..entity import Edit, Finding, TextBlock

# --------------------------------------------------------------------- 常量

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

#: 需要处理的 word/ 下 XML 部件。styles.xml / numbering.xml / settings.xml
#: 不含正文文本，原样拷贝，避免破坏样式与编号。
_TEXT_PART_RE = re.compile(
    r"^word/(document|footnotes|endnotes|comments|header\d*|footer\d*)\.xml$"
)

#: 允许出现的 run 文本子元素。instrText 是域代码（如 MERGEFIELD 张三），
#: 同样可能带出姓名，一并纳入。
_TEXT_TAGS = ("t", "delText", "instrText")
_TEXT_TAGS_Q = frozenset(f"{{{W}}}{tag}" for tag in _TEXT_TAGS)

#: core.xml 中「保留并脱敏」的字段。标题与主题含案由信息，对后续法律分析
#: 有用，因此替换成代号而不是清空。
_KEEP_CORE_FIELDS = frozenset({"title", "subject"})

#: core.xml 中「直接清空」的字段。这些字段回答的是「谁、在什么时候、
#: 用什么工具建的这份文档」，脱敏后没有分析价值；替换成代号还会留下
#: 「作者=【长颈鹿】」这类结构性痕迹，清空更彻底。revision（修订次数）
#: 也一并清掉，避免反推文档被改过几稿。
_CLEAR_CORE_FIELDS = frozenset(
    {
        "creator",
        "lastModifiedBy",
        "keywords",
        "description",
        "category",
        "identifier",
        "contentStatus",
        "version",
        "revision",
    }
)

#: docProps/app.xml 中可能带出当事人或经办人的字段。
_APP_FIELDS = ("Company", "Manager")

#: docProps/custom.xml 的属性元素使用自己的命名空间。
_CUSTOM_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"

#: apply 时需要「清空敏感字段」的部件。这些部件即使没有任何 Edit 也要改写：
#: 作者、公司、自定义属性值不可能靠正文替换覆盖，必须在输出里清掉。
_DOC_PROPS_PARTS = frozenset(
    {"docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"}
)

#: 修订相关的「旧属性」容器：删除它们不影响正文，可减少残留信息。
_CHANGE_TAGS = (
    "pPrChange",
    "rPrChange",
    "tblPrChange",
    "trPrChange",
    "tcPrChange",
    "sectPrChange",
    "numberingChange",
)


def _q(tag: str) -> str:
    """拼出 WordprocessingML 命名空间下的 Clark 记法标签。"""
    return f"{{{W}}}{tag}"


# --------------------------------------------------------------------- 定位


@dataclass(frozen=True)
class DocxLocator:
    """docx 文本块的定位信息，由本模块自己解释。

    ``kind`` 取值：

    * ``paragraph``      —— ``part`` 内按文档顺序的第 ``index`` 个 ``w:p``。
      段落文本是该段所有 run 拼接后的结果（已剔除 ``w:del`` 内容）。
    * ``revision``       —— 同一段内 ``w:del`` / ``w:moveFrom`` 里的文本。
      回写时整体删除，对应的 Edit 会被忽略（原文已经不存在了）。
    * ``comment_author`` —— ``part`` 内第 ``index`` 个 ``w:comment`` 的
      ``attr`` 属性（``w:author`` 或 ``w:initials``）。
    * ``xml_node``       —— ``part`` 内从根算起的元素路径 ``path``；
      ``attr`` 为空表示改元素文本，否则改该属性。
    """

    part: str
    kind: str
    index: int = -1
    path: str = ""
    attr: str = ""


# ------------------------------------------------------------------ XML 工具


def _nearest_paragraph(el: etree._Element) -> etree._Element | None:
    """向上找最近的 ``w:p``。用于把嵌套文本框里的 run 判给内层段落。"""
    cur = el.getparent()
    while cur is not None:
        if cur.tag == _q("p"):
            return cur
        cur = cur.getparent()
    return None


def _in_deleted(run: etree._Element, paragraph: etree._Element) -> bool:
    """run 是否位于 ``w:del`` / ``w:moveFrom`` 内（即会被「接受修订」删掉）。"""
    cur = run.getparent()
    while cur is not None and cur is not paragraph:
        if cur.tag in (_q("del"), _q("moveFrom")):
            return True
        cur = cur.getparent()
    return False


def _own_runs(paragraph: etree._Element) -> list[etree._Element]:
    """属于该段落的 run，按文档顺序。

    嵌套文本框里的 run 由内层段落负责，这里靠「最近祖先 w:p」排除，
    否则外层段落会把文本框文字算两遍。
    """
    return [r for r in paragraph.iter(_q("r")) if _nearest_paragraph(r) is paragraph]


#: 分页符在块文本里的占位字符。换行会改变「版式位置」，必须让它参与字符
#: 偏移映射：如果把它当空白忽略，重建 run 时分页符会被挤到文本末尾。
PAGE_BREAK = "\f"


def _is_content_element(el: etree._Element) -> bool:
    """run 里承载文字内容/换行/制表符的子元素。"""
    return (
        el.tag in _TEXT_TAGS_Q
        or el.tag == _q("tab")
        or el.tag == _q("br")
        or el.tag == _q("cr")
    )


def _run_text(run: etree._Element) -> str:
    """run 的文本，按渲染顺序还原制表符、软换行与分页符。

    ``w:tab`` -> ``\t``，``w:br``/``w:cr`` -> ``\n``，
    ``w:br w:type="page"`` -> ``\f``（占位符，保证重建时分页位置不漂移）。

    必须把换行/制表符算进字符序列，否则检测器看到的偏移与写回时重建的
    文本对不上——这正是段内换行被挪到行尾的根因。
    """
    parts: list[str] = []
    for child in run:
        if child.tag in _TEXT_TAGS_Q:
            parts.append(child.text or "")
        elif child.tag == _q("tab"):
            parts.append("\t")
        elif child.tag == _q("br"):
            parts.append(PAGE_BREAK if child.get(_q("type")) == "page" else "\n")
        elif child.tag == _q("cr"):
            parts.append("\n")
    return "".join(parts)


def _make_text_node(tag: str, value: str) -> etree._Element:
    node = etree.Element(tag)
    node.text = value
    # 前后空格在 Word 里会被折叠，显式声明 preserve 才不会改变排版
    node.set(XML_SPACE, "preserve")
    return node


def _set_run_text(run: etree._Element, text: str) -> None:
    """把新文本写回 run，按位置重建 ``w:t`` / ``w:br`` / ``w:tab``。

    为什么不能只改第一个 ``w:t``：一个 run 里可能是
    ``<w:t>法院名</w:t><w:br/><w:t>文书标题</w:t>``。如果把整段文本压进
    第一个 w:t、把后面的 w:t 清空，``<w:br/>`` 会落到文本末尾——原本
    「法院名 | 换行 | 标题」的版式就变成挤在一行。正确做法是把 ``\n`` /
    ``\t`` 当成字符参与偏移映射，写回时再按字符位置重建节点。

    只重建文字内容节点，``w:rPr`` 与图片等其它子元素原样保留。
    """
    children = list(run)
    indices = [i for i, child in enumerate(children) if _is_content_element(child)]
    if not indices:
        return
    first = children[indices[0]]
    # 域代码 run 只含 w:instrText，若改写成 w:t 会破坏域，保持原类型
    primary = first.tag if first.tag in _TEXT_TAGS_Q else _q("t")
    insert_at = indices[0]

    for child in children:
        if _is_content_element(child):
            run.remove(child)

    nodes: list[etree._Element] = []
    if primary == _q("instrText"):
        nodes.append(_make_text_node(primary, text))
    else:
        buffer: list[str] = []

        def flush() -> None:
            if buffer:
                nodes.append(_make_text_node(primary, "".join(buffer)))
                buffer.clear()

        for char in text:
            if char == "\n":
                flush()
                nodes.append(etree.Element(_q("br")))
            elif char == PAGE_BREAK:
                flush()
                page_break = etree.Element(_q("br"))
                page_break.set(_q("type"), "page")
                nodes.append(page_break)
            elif char == "\t":
                flush()
                nodes.append(etree.Element(_q("tab")))
            else:
                buffer.append(char)
        flush()
        if not nodes:  # 空文本也保留一个空节点，避免 run 变成无文本结构
            nodes.append(_make_text_node(primary, ""))

    for offset, node in enumerate(nodes):
        run.insert(insert_at + offset, node)


def _in_table(paragraph: etree._Element) -> bool:
    """段落是否在表格（含嵌套表格）内。"""
    cur = paragraph.getparent()
    while cur is not None:
        if cur.tag == _q("tbl"):
            return True
        cur = cur.getparent()
    return False


def _node_path(root: etree._Element, node: etree._Element) -> str:
    """元素相对 part 根节点的下标链，如 ``"0/2/1"``。

    同一份文件两次解析得到的路径一致，因此可以用来在 apply 阶段重新定位。
    """
    chain: list[str] = []
    cur = node
    while cur is not root:
        parent = cur.getparent()
        if parent is None:
            return ""
        chain.append(str(parent.index(cur)))
        cur = parent
    return "/".join(reversed(chain))


def _resolve_node(root: etree._Element, path: str) -> etree._Element | None:
    """按 ``_node_path`` 的格式取回元素。"""
    if not path:
        return root
    cur = root
    for part in path.split("/"):
        try:
            idx = int(part)
        except ValueError:
            return None
        if idx < 0 or idx >= len(cur):
            return None
        cur = cur[idx]
    return cur


def _splice(text: str, edits: Sequence[Edit]) -> str:
    """把若干块内区间替换应用到一段纯文本。

    从后往前改，避免前面的替换导致后面偏移失效。区间互不重叠由
    engine 的 ``resolve_overlaps`` 保证。
    """
    out = text
    for edit in sorted(edits, key=lambda e: e.entity.start, reverse=True):
        start, end = edit.entity.start, edit.entity.end
        if start < 0 or end > len(out) or end < start:
            continue
        out = out[:start] + edit.replacement + out[end:]
    return out


def _accept_revisions(root: etree._Element) -> None:
    """接受 XML 里的全部修订。

    1. 删掉 ``w:del`` / ``w:moveFrom``（连同里面的 ``w:delText`` 原文）；
    2. 解包 ``w:ins`` / ``w:moveTo``，内容转正；
    3. 删掉 ``*PrChange`` 这类「旧属性快照」，减少残留。
    """
    for tag in ("del", "moveFrom"):
        for el in list(root.iter(_q(tag))):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)

    for tag in ("ins", "moveTo"):
        for el in list(root.iter(_q(tag))):
            parent = el.getparent()
            if parent is None:
                continue
            idx = parent.index(el)
            for child in list(el):
                el.remove(child)
                parent.insert(idx, child)
                idx += 1
            parent.remove(el)

    for tag in _CHANGE_TAGS:
        for el in list(root.iter(_q(tag))):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)


def _clear_doc_props(root: etree._Element, part: str) -> None:
    """清空文档属性里的敏感字段。

    * core.xml：作者、最后修改人、关键词、描述、分类、标识、内容状态、
      版本、修订次数一律置空；title / subject 保留（它们由 Edit 替换）。
    * app.xml：Company / Manager 置空。
    * custom.xml：只清空属性的**值**，保留属性名与 property 结构——
      直接删部件还要同步清理 [Content_Types].xml 与 _rels/.rels，
      漏一处 Word 就会报文件损坏，清值更稳。
    """
    if part == "docProps/core.xml":
        for node in root:
            if not isinstance(node.tag, str):
                continue
            if etree.QName(node).localname in _CLEAR_CORE_FIELDS:
                node.text = ""
        return

    if part == "docProps/app.xml":
        for field in _APP_FIELDS:
            for child in root:
                if isinstance(child.tag, str) and etree.QName(child).localname == field:
                    child.text = ""
        return

    if part == "docProps/custom.xml":
        for prop in root.iter(f"{{{_CUSTOM_NS}}}property"):
            for child in prop:
                if isinstance(child.tag, str):
                    child.text = ""


def _part_role(part: str) -> str:
    """按部件名给出 role。表格内段落再由调用方降级成 ``cell``。"""
    if part.startswith("word/header"):
        return "header"
    if part.startswith("word/footer"):
        return "footer"
    if part in ("word/footnotes.xml", "word/endnotes.xml"):
        return "footnote"
    if part == "word/comments.xml":
        return "comment"
    return "body"


# --------------------------------------------------------------------- 处理器


class DocxHandler:
    """Word 文档的 extract / apply / audit 实现。"""

    name = "docx"
    suffixes: tuple[str, ...] = (".docx",)

    def __init__(self) -> None:
        # 按文件路径记录非致命错误，audit 时汇报——单个段落坏了不能中断整个文件
        self._diagnostics: dict[str, list[str]] = {}

    # ---------------------------------------------------------------- 诊断

    def _key(self, path: str) -> str:
        try:
            return str(Path(path).resolve())
        except OSError:  # 极端路径问题不至于让整个流程崩
            return str(path)

    def _note(self, path: str, message: str) -> None:
        self._diagnostics.setdefault(self._key(path), []).append(message)

    # ---------------------------------------------------------------- 读取

    def _read_package(self, path: str) -> list[tuple[zipfile.ZipInfo, bytes]]:
        with zipfile.ZipFile(path) as archive:
            return [(info, archive.read(info.filename)) for info in archive.infolist()]

    def _parse_part(self, path: str, part: str, blob: bytes) -> etree._Element | None:
        """解析一个 XML 部件。失败只记录，不中断整个文件。"""
        try:
            return etree.fromstring(blob)
        except etree.XMLSyntaxError as exc:
            self._note(path, f"{part} 解析失败：{exc}")
            return None

    # ------------------------------------------------------------- extract

    def extract(self, path: str) -> list[TextBlock]:
        """取出所有可检测文本块。

        覆盖：正文、表格（含嵌套）、页眉页脚、脚注尾注、文本框、超链接
        显示文本、批注正文与作者、修订记录（role=revision）、文档属性。
        """
        blocks: list[TextBlock] = []
        entries = self._read_package(path)

        for info, blob in entries:
            part = info.filename
            if info.is_dir() or not part.endswith(".xml"):
                continue

            if _TEXT_PART_RE.match(part):
                root = self._parse_part(path, part, blob)
                if root is not None:
                    self._extract_text_part(path, part, root, blocks)
            elif part == "docProps/core.xml":
                root = self._parse_part(path, part, blob)
                if root is not None:
                    self._extract_doc_props(path, part, root, blocks)
            elif part == "docProps/custom.xml":
                root = self._parse_part(path, part, blob)
                if root is not None:
                    self._extract_custom_props(path, part, root, blocks)

        return blocks

    def _extract_text_part(
        self,
        path: str,
        part: str,
        root: etree._Element,
        blocks: list[TextBlock],
    ) -> None:
        """处理 document / header / footer / footnotes / endnotes / comments。"""
        role_default = _part_role(part)

        for index, paragraph in enumerate(root.iter(_q("p"))):
            try:
                runs = _own_runs(paragraph)
            except Exception as exc:  # noqa: BLE001 - 单个段落失败不影响整体
                self._note(path, f"{part} 第 {index} 段遍历失败：{exc}")
                continue

            normal = [r for r in runs if not _in_deleted(r, paragraph)]
            deleted = [r for r in runs if _in_deleted(r, paragraph)]

            text = "".join(_run_text(r) for r in normal)
            if text:
                role = role_default
                if role == "body" and _in_table(paragraph):
                    role = "cell"  # 表格单元格单独标注，便于审计统计
                blocks.append(
                    TextBlock(
                        text=text,
                        locator=DocxLocator(part=part, kind="paragraph", index=index),
                        role=role,
                    )
                )

            revision = "".join(_run_text(r) for r in deleted)
            if revision:
                # 修订记录里的原文单独成块：显示层看不到，但文件里真实存在
                blocks.append(
                    TextBlock(
                        text=revision,
                        locator=DocxLocator(part=part, kind="revision", index=index),
                        role="revision",
                    )
                )

        if part == "word/comments.xml":
            self._extract_comment_authors(path, part, root, blocks)

    def _extract_comment_authors(
        self,
        path: str,
        part: str,
        root: etree._Element,
        blocks: list[TextBlock],
    ) -> None:
        """批注作者名 / 缩写：属性值不在任何段落里，必须单独取。"""
        for index, comment in enumerate(root.iter(_q("comment"))):
            for attr in ("author", "initials"):
                value = comment.get(_q(attr))
                if value and value.strip():
                    blocks.append(
                        TextBlock(
                            text=value,
                            locator=DocxLocator(
                                part=part, kind="comment_author", index=index, attr=attr
                            ),
                            role="comment",
                        )
                    )

    def _extract_doc_props(
        self,
        path: str,
        part: str,
        root: etree._Element,
        blocks: list[TextBlock],
    ) -> None:
        """docProps/core.xml。

        只提取 title / subject：其余字段（作者、最后修改人、公司、管理者）
        在 apply 阶段直接清空，提取出来交给检测器只会白占一个代号，而那个
        代号最终不会出现在输出文件里。app.xml 同理，不再提取。
        """
        for node in root:
            if not isinstance(node.tag, str):
                continue
            local = etree.QName(node).localname
            if local not in _KEEP_CORE_FIELDS:
                continue
            value = node.text or ""
            if not value.strip():
                continue
            node_path = _node_path(root, node)
            if not node_path:
                continue
            blocks.append(
                TextBlock(
                    text=value,
                    locator=DocxLocator(part=part, kind="xml_node", path=node_path),
                    role="metadata",
                )
            )

    def _extract_custom_props(
        self,
        path: str,
        part: str,
        root: etree._Element,
        blocks: list[TextBlock],
    ) -> None:
        """docProps/custom.xml：只提取属性名，值在 apply 时清空。

        自定义属性名是标签（「项目」「承办人」这类），保留结构便于用户看懂
        文件；名字本身可能含敏感串（「张三案」），所以交给检测器替换。
        属性值一律清空——它常被用来存「项目名/当事人」，留着就是泄露。
        """
        for prop in root.iter(f"{{{_CUSTOM_NS}}}property"):
            node_path = _node_path(root, prop)
            if not node_path:
                continue
            name = prop.get("name")
            if name:
                blocks.append(
                    TextBlock(
                        text=name,
                        locator=DocxLocator(part=part, kind="xml_node", path=node_path, attr="name"),
                        role="metadata",
                    )
                )

    # --------------------------------------------------------------- apply

    def apply(self, path: str, out_path: str, edits: Sequence[Edit]) -> None:
        """按 edits 回写并保存到 out_path，绝不覆盖原文件。"""
        if Path(path).resolve() == Path(out_path).resolve():
            raise ValueError(f"out_path 不能与原文件相同：{path}")

        # 只带 DocxLocator 的编辑属于本处理器；其余静默跳过
        grouped: dict[str, dict[str, list[Edit]]] = {}
        for edit in edits:
            locator = edit.block.locator
            if not isinstance(locator, DocxLocator):
                continue
            bucket = grouped.setdefault(locator.part, {})
            if locator.kind == "xml_node":
                key = f"node:{locator.path}:{locator.attr}"
            elif locator.kind == "comment_author":
                key = f"author:{locator.index}:{locator.attr}"
            elif locator.kind == "paragraph":
                key = f"para:{locator.index}"
            else:
                key = f"{locator.kind}:{locator.index}"
            bucket.setdefault(key, []).append(edit)

        entries = self._read_package(path)
        out_entries: list[tuple[zipfile.ZipInfo, bytes]] = []

        for info, blob in entries:
            part = info.filename
            # 文本部件即使本次没有命中实体，也要接受修订：修订里的原文属于
            # 「格式规范化」问题，不能因为检测器没认出它就把原文留在文件里。
            needs_accept = bool(_TEXT_PART_RE.match(part))
            needs_clear = part in _DOC_PROPS_PARTS
            if info.is_dir() or not part.endswith(".xml"):
                out_entries.append((info, blob))
                continue
            if not (needs_accept or needs_clear or grouped.get(part)):
                out_entries.append((info, blob))
                continue
            root = self._parse_part(path, part, blob)
            if root is None:
                out_entries.append((info, blob))
                continue
            original = blob
            try:
                # 先接受修订：del 原文彻底删除，ins 内容转正
                if needs_accept:
                    _accept_revisions(root)
                self._apply_part(path, part, root, grouped.get(part, {}))
                # 清空放在替换之后：即使外部传入了指向「作者」这类字段的
                # Edit，最终结果也一定是空值，不会被替换文本复活
                if needs_clear:
                    _clear_doc_props(root, part)
                blob = etree.tostring(
                    root, xml_declaration=True, encoding="UTF-8", standalone=True
                )
            except Exception as exc:  # noqa: BLE001 - 单部件失败保留原样并记录
                self._note(path, f"{part} 回写失败，已保留原部件：{exc}")
                blob = original
            out_entries.append((info, blob))

        out_parent = Path(out_path).parent
        if out_parent and not out_parent.exists():
            out_parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out_path, "w") as archive:
            for info, blob in out_entries:
                archive.writestr(info, blob)

    def _apply_part(
        self,
        path: str,
        part: str,
        root: etree._Element,
        bucket: dict[str, list[Edit]],
    ) -> None:
        """在一个已解析的 part 上应用全部编辑。"""
        # 段落：接受修订后的 w:p 顺序与 extract 时一致（修订不增删段落）
        paragraph_edits: dict[int, list[Edit]] = {}
        for key, items in bucket.items():
            if key.startswith("para:"):
                paragraph_edits[int(key.split(":", 1)[1])] = items

        if paragraph_edits:
            for index, paragraph in enumerate(root.iter(_q("p"))):
                current = paragraph_edits.get(index)
                if not current:
                    continue
                try:
                    self._rewrite_paragraph(paragraph, current)
                except Exception as exc:  # noqa: BLE001
                    self._note(path, f"{part} 第 {index} 段回写失败：{exc}")

        # 批注作者
        author_edits = {
            int(key.split(":", 2)[1]): items
            for key, items in bucket.items()
            if key.startswith("author:")
        }
        if author_edits:
            comments = list(root.iter(_q("comment")))
            for index, items in author_edits.items():
                if index >= len(comments):
                    self._note(path, f"{part} 批注序号 {index} 越界")
                    continue
                attr = items[0].block.locator.attr  # type: ignore[union-attr]
                comments[index].set(_q(attr), _splice(items[0].block.text, items))

        # 文档属性
        for key, items in bucket.items():
            if not key.startswith("node:"):
                continue
            locator = items[0].block.locator
            node = _resolve_node(root, locator.path)  # type: ignore[union-attr]
            if node is None:
                self._note(path, f"{part} 找不到属性节点 {locator.path}")  # type: ignore[union-attr]
                continue
            # 以提取时的块文本为基准重建：docProps 可能被外部工具改写，
            # 按块文本拼接才能保证「原文 → 代号」的映射不被破坏
            base = items[0].block.text
            if locator.attr:  # type: ignore[union-attr]
                node.set(locator.attr, _splice(base, items))  # type: ignore[union-attr]
            else:
                node.text = _splice(base, items)

    def _rewrite_paragraph(self, paragraph: etree._Element, edits: Sequence[Edit]) -> None:
        """跨 run 替换一个段落。

        步骤：
        1. 按文档顺序取本段的 run（``w:del`` 内容已被删除）；
        2. 拼接 ``full_text``，记录每个 run 的起点；
        3. 逐个 run 计算新文本：被实体覆盖的字符删掉，replacement 写在
           实体起点所在的 run 里；
        4. 只改文本元素，不动 ``w:r`` / ``w:rPr`` 结构。
        """
        runs = _own_runs(paragraph)
        if not runs:
            return
        texts = [_run_text(run) for run in runs]
        full = "".join(texts)
        if not full:
            return

        starts: list[int] = []
        cursor = 0
        for text in texts:
            starts.append(cursor)
            cursor += len(text)

        ordered = sorted(edits, key=lambda e: e.entity.start)
        new_texts: list[str] = []
        for i, text in enumerate(texts):
            run_start = starts[i]
            run_end = run_start + len(text)
            out: list[str] = []
            consumed = run_start
            for edit in ordered:
                start, end = edit.entity.start, edit.entity.end
                if end <= run_start or start >= run_end:
                    continue
                if start > consumed:
                    out.append(text[consumed - run_start : start - run_start])
                if start >= run_start:
                    # replacement 只落在实体起点所在的 run，避免重复插入
                    out.append(edit.replacement)
                consumed = max(consumed, min(end, run_end))
            if consumed < run_end:
                out.append(text[consumed - run_start :])
            new_texts.append("".join(out))

        for run, new_text in zip(runs, new_texts):
            if new_text != _run_text(run):
                _set_run_text(run, new_text)

    # --------------------------------------------------------------- audit

    def audit(self, path: str) -> list[Finding]:
        """报告该文件的泄露点。

        重点不是「有哪些内容」，而是「哪些位置是普通正文替换碰不到的」：
        修订记录、批注、文档属性、嵌入对象、宏。
        """
        findings: list[Finding] = []
        entries = self._read_package(path)
        names = [info.filename for info, _ in entries]
        lowered = path.lower()

        # ---- 宏 ------------------------------------------------------
        if lowered.endswith(".docm") or any(n.endswith("vbaProject.bin") for n in names):
            findings.append(
                Finding(
                    level="critical",
                    code="docx.macro",
                    message="文件包含 VBA 宏（vbaProject.bin / .docm）。宏可读取并外传原文，"
                    "脱敏后应确认宏是否仍然必要，或另存为无宏的 .docx。",
                    location=path,
                )
            )

        # ---- 嵌入对象与图片 -----------------------------------------
        embedded = [n for n in names if n.startswith("word/embeddings/")]
        if embedded:
            findings.append(
                Finding(
                    level="critical",
                    code="docx.embedded_object",
                    message=f"含 {len(embedded)} 个嵌入对象（{', '.join(embedded[:3])}）。"
                    "嵌入的 Excel/PDF 内部保留独立原文，正文替换覆盖不到。",
                    location=path,
                )
            )
        media = [n for n in names if n.startswith("word/media/")]
        if media:
            findings.append(
                Finding(
                    level="info",
                    code="docx.media",
                    message=f"含 {len(media)} 个图片/媒体文件。图片中的文字不在文本层，"
                    "若图片含证件照片或截图，需人工复核。",
                    location=path,
                )
            )

        # ---- 逐 part 检查 -------------------------------------------
        revision_total = 0
        comment_total = 0
        authors: list[str] = []
        initials_list: list[str] = []
        cleared_fields: list[str] = []
        kept_fields: list[tuple[str, str]] = []

        for info, blob in entries:
            part = info.filename
            if info.is_dir() or not part.endswith(".xml"):
                continue
            if not (_TEXT_PART_RE.match(part) or part.startswith("docProps/")):
                continue
            root = self._parse_part(path, part, blob)
            if root is None:
                continue

            if _TEXT_PART_RE.match(part):
                revision_total += len(list(root.iter(_q("del")))) + len(
                    list(root.iter(_q("moveFrom")))
                )
                if part == "word/comments.xml":
                    comments = list(root.iter(_q("comment")))
                    comment_total += len(comments)
                    for comment in comments:
                        author = comment.get(_q("author"))
                        if author and author.strip():
                            authors.append(author)
                        initials = comment.get(_q("initials"))
                        if initials and initials.strip():
                            initials_list.append(initials)

            if part == "docProps/core.xml":
                for node in root:
                    if not isinstance(node.tag, str):
                        continue
                    local = etree.QName(node).localname
                    value = (node.text or "").strip()
                    if not value:
                        continue
                    if local in _KEEP_CORE_FIELDS:
                        # title / subject 含案由，保留但替换其中的实体
                        kept_fields.append((local, value))
                    elif local in _CLEAR_CORE_FIELDS:
                        cleared_fields.append(f"{local}（core.xml）")
            elif part == "docProps/app.xml":
                for field in _APP_FIELDS:
                    # 按 localname 找：不同生成器对 app.xml 的命名空间处理不一致
                    value = ""
                    for child in root:
                        if isinstance(child.tag, str) and etree.QName(child).localname == field:
                            value = (child.text or "").strip()
                            break
                    if value:
                        cleared_fields.append(f"{field}（app.xml）")
            elif part == "docProps/custom.xml":
                props = list(root.iter(f"{{{_CUSTOM_NS}}}property"))
                if props:
                    findings.append(
                        Finding(
                            level="warn",
                            code="docx.custom_props",
                            message=f"含 {len(props)} 条自定义文档属性。属性值在 apply 时"
                            "全部清空（常被用来存「项目名/当事人」），属性名保留并脱敏。",
                            location=f"{path}!{part}",
                        )
                    )
                    for prop in props:
                        for child in prop:
                            if isinstance(child.tag, str) and (child.text or "").strip():
                                cleared_fields.append("custom 属性值")

        if revision_total:
            findings.append(
                Finding(
                    level="critical",
                    code="docx.revision",
                    message=f"含 {revision_total} 处修订记录。被删除的原文以 w:delText "
                    "原样保存在 XML 里，接受修订前任何「只改正文」的脱敏都会漏掉它。",
                    location=path,
                )
            )
            findings.append(
                Finding(
                    level="info",
                    code="revisions_accepted",
                    message="已接受全部修订以清除 w:delText 中的原文，输出文件不再保留修订痕迹。"
                    "如果客户需要保留修订痕迹（例如要核对谁改了什么），必须在脱敏前"
                    "人工另存一份原件，因为接受修订是不可逆的文档状态变更。",
                    location=path,
                )
            )
        if comment_total:
            findings.append(
                Finding(
                    level="warn",
                    code="comments_present",
                    message=f"本文件含 {comment_total} 条批注，已脱敏保留；若对外发送原件，"
                    "建议人工删除批注。"
                    f"（作者：{'、'.join(sorted(set(authors))) if authors else '（空）'}"
                    + (
                        f"，缩写：{'、'.join(sorted(set(initials_list)))}"
                        if initials_list
                        else ""
                    )
                    + "；批注正文、作者名与缩写都在 comments.xml 里，独立于正文）",
                    location=path,
                )
            )

        # ---- 元数据清空报告 -----------------------------------------
        if cleared_fields:
            findings.append(
                Finding(
                    level="info",
                    code="docx.metadata_cleared",
                    message=f"已清空 {len(cleared_fields)} 个元数据字段："
                    f"{'、'.join(cleared_fields[:12])}。title 与 subject 保留脱敏后的值"
                    "（含案由信息，对分析有用）。",
                    location=path,
                )
            )
        else:
            findings.append(
                Finding(
                    level="info",
                    code="docx.metadata_cleared",
                    message="元数据字段均已清空（0 个待清），未发现作者/公司等残留。",
                    location=path,
                )
            )
        for field, value in kept_fields:
            findings.append(
                Finding(
                    level="warn",
                    code="docx.metadata_kept",
                    message=f"{field} 保留为「{value}」。该字段按策略保留，交付前请确认"
                    "其中的当事人信息已被替换成代号。",
                    location=f"{path}!docProps/core.xml",
                )
            )

        # ---- role 统计：让审计报告能看出「哪一层被覆盖了」 -----------
        # 直接复用 extract，而不是在 audit 里再写一份遍历逻辑——
        # 两套实现一旦分叉，审计统计就会和实际提取结果对不上。
        try:
            blocks = self.extract(path)
        except Exception as exc:  # noqa: BLE001
            self._note(path, f"审计统计时提取失败：{exc}")
            blocks = []
        counter = Counter(block.role for block in blocks)
        if counter:
            detail = "、".join(f"{role}={count}" for role, count in sorted(counter.items()))
            findings.append(
                Finding(
                    level="info",
                    code="docx.roles",
                    message=f"提取到的文本块按 role 统计：{detail}。",
                    location=path,
                )
            )

        for message in self._diagnostics.get(self._key(path), [])[:20]:
            findings.append(
                Finding(level="warn", code="docx.error", message=message, location=path)
            )
        return findings



HANDLER = DocxHandler()


__all__ = ["DocxHandler", "DocxLocator", "HANDLER"]

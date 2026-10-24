"""PDF 处理器：真实擦除（redaction）与扫描件 OCR 脱敏。

为什么必须用 redaction 而不是画黑框
------------------------------------
在文字上盖一块黑色矩形，视觉上「看不见了」，但 PDF 内容流里的文字对象原样保留：

* ``page.get_text()``、pdftotext、任何法律 AI 工作台的解析器都能把它读出来；
* 复制粘贴、全文搜索、无障碍朗读同样能拿到原文；
* 黑框只是一条绘制指令，用任何 PDF 编辑器一删就复原。

法院材料交给第三方 AI 分析时，「盖黑框」等于没有脱敏。本处理器走两条真实删除链路：

1. **文本型 PDF**：``page.add_redact_annot()`` 标记实体矩形 →
   ``page.apply_redactions(images=PDF_REDACT_IMAGE_PIXELS)``。MuPDF 会从内容流里
   物理移除被矩形覆盖的文字对象，并擦除被文字压住的图像像素——不是叠加、不是遮挡。
2. **扫描型 PDF**：整页就是一张图片，没有文字对象可删。先以 300dpi 渲染交
   RapidOCR 得到行框，按字符宽度估算实体子矩形，再做同样的 redaction；
   ``PDF_REDACT_IMAGE_PIXELS`` 会把该区域的图像像素改写成背景色，原图上的字真正消失。

擦除之后，在原来的位置写入带框代号（如【熊猫】）。代号用**嵌入子集字体**绘制，
因此脱敏件对下游模型「可读」（有文本层）而对人「不可复原」（无原文）。

定位策略（为什么优先用字符坐标，而不是 search_for）
------------------------------------------------------
``search_for(原文)`` 依赖内容流里的字符串与传入文本逐字一致，而真实法院 PDF 里
常见两种破坏：案号被排版成 ``(2024)甲法 民初1234 号``（中间带空格）、身份证号
被 LaTeX/LibreOffice 拆成多个文本对象。此时 search_for 直接返回空，或只命中半截。
``page.get_text("rawdict")`` 给出**每个字符的 bbox**，按字符偏移切片得到的是渲染位置
的真实坐标，不依赖字符串匹配，也不会在同名多处的页面上定位到错误的那一处。
因此顺序是：字符坐标 → search_for（退化兜底）→ 行内均分（最后兜底）。
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import pymupdf

from ..entity import Edit, EntityType, Finding, TextBlock
from .ocr import OcrLine, recognize

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 文本层字符数低于该阈值、且页面含整页图片时，视为扫描页。
#: 阈值不能太高：判决书首页可能只有「民事判决书」几个字，那种页面仍是文本页。
SCAN_TEXT_THRESHOLD = 20

#: 扫描页渲染分辨率。300dpi 是 RapidOCR 的舒适区：再低小字（身份证号）识别率下降，
#: 再高只是线性增加内存与耗时。A4@300dpi 单页 RGB 约 26MB，必须逐页渲染并立即释放。
RENDER_DPI = 300
_PT_PER_PX = 72.0 / RENDER_DPI

#: 写入代号用的字体。默认使用 MuPDF 内置的 CJK 字体（Droid Sans Fallback）。
#:
#: 为什么不用任务里提到的 NotoSerifCJK-Bold.ttc：
#: 该文件是 27MB 的 OTC 全字库，PyMuPDF 取第 0 个 face（JP 变体）整份嵌入，
#: 输出 PDF 每份固定多出约 20MB；而 ``doc.subset_fonts()`` 对它的 CFF2 charstring
#: 报错（MuPDF error: Reserved charstring byte），无法子集化。实测同一份扫描件：
#: Noto → 21MB，内置 cjk 子集化后 → 65KB。体积差距 300 倍，且内置字体渲染正常、
#: 文字可被 PyMuPDF 正确提取。需要 Noto 的字形风格时，把 MASK_PDF_FONT 指向
#: ttf/otf 单文件即可（ttc 请勿使用，理由同上）。
FONT_ENV = "MASK_PDF_FONT"

#: 写入代号时在页面资源里注册的字体名（每页注册一次，供 insert_text 使用）。
_PAGE_FONT_NAME = "maskcjk"

_FONT: Any = None
_FONT_LOCK = threading.Lock()


def _font() -> Any:
    """返回进程内唯一的中文字体对象（懒加载）。"""
    global _FONT
    if _FONT is None:
        with _FONT_LOCK:
            if _FONT is None:
                custom = os.environ.get(FONT_ENV, "").strip()
                if custom and pathlib.Path(custom).is_file():
                    _FONT = pymupdf.Font(fontfile=custom)
                else:
                    _FONT = pymupdf.Font("cjk")
    return _FONT


# ---------------------------------------------------------------------------
# 定位信息
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PdfLocator:
    """一个 TextBlock 在 PDF 页面上的位置。

    核心不变量：``char_boxes`` 与所属 ``TextBlock.text`` **逐字符对齐**。
    检测器给出的 ``Entity.start/end`` 是块内字符偏移，apply 时用
    ``char_boxes[start:end]`` 取并集就是实体矩形——不经过任何字符串匹配，
    因此对换行、断字、同页重复、行内空格都免疫。

    字段
    ----
    page_no    : 0 起的页号
    bbox       : 行/段外接矩形（PDF 点坐标，未旋转）
    char_boxes : 每个字符的 bbox，顺序与 text 一致
    baseline   : 行基线 y（写入代号时对齐用）
    font_size  : 原文字号（估算值；OCR 来源为估算）
    source     : "text"（文本层）或 "ocr"（扫描件识别）
    ocr_score  : OCR 行置信度，文本层为 0
    """

    page_no: int
    bbox: tuple[float, float, float, float]
    char_boxes: tuple[tuple[float, float, float, float], ...]
    baseline: float
    font_size: float
    source: str = "text"
    ocr_score: float = 0.0
    #: 本块由几个原始行合并而来（1 = 未合并）。det-layer 据此关闭折行保护规则。
    merged_lines: int = 1
    #: 合并块中每个原始行结束的字符下标；未合并时为空。
    line_breaks: tuple[int, ...] = ()
    #: 合并块中每个原始行的基线，与 line_breaks 一一对应。
    line_baselines: tuple[float, ...] = ()

    @property
    def is_ocr(self) -> bool:
        return self.source == "ocr"

    @property
    def is_merged(self) -> bool:
        return self.merged_lines > 1


@dataclass(frozen=True, slots=True)
class PdfTocLocator:
    """书签（大纲）条目的定位信息。

    书签标题是**独立的文本来源**：不在页面内容流里，``page.get_text()`` 读不到，
    但打开 PDF 的目录面板会原样展示——法院文书的书签常常就是「张三诉某某公司…案」，
    只脱敏正文等于把当事人姓名留在目录里。所以标题必须当成 TextBlock 参与检测，
    apply 时用 ``doc.set_toc()`` 写回。

    index  : ``get_toc()`` 返回列表的下标（0 起）
    page_no: 条目指向的页（0 起），仅用于定位与审计
    """

    index: int
    page_no: int = 0


@dataclass(frozen=True, slots=True)
class PdfAnnotLocator:
    """页面批注某个文本字段的定位信息。

    批注内容同样不在页面文本层里：``get_text()`` 读不到，但打开 PDF 点开气泡
    就能看到真实姓名、电话。因此按 (页, 批注 xref, 字段) 定位，逐字段检测替换。

    page_no: 0 起的页号
    xref   : 批注对象 xref
    field  : content | title | subject
    """

    page_no: int
    xref: int
    field: str = "content"


#: 中文数字与金额用字。跨行实体若把下一行首字符吞进来，且该字符属于这里，
#: 基本可以断定是「姓名/机构名接上了日期或金额」，必须截断。
_CJK_NUMERALS = frozenset("〇零一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟元角分整两")

#: 允许作为跨行实体末尾单字的字符：机构与地名的常见后缀。
#: 合法的跨行机构名右半可能只有 1 个字（「…职业技术学 / 院」「…有限公 / 司」），
#: 但「…有限公司 / 人」这种把下一行正文首字吞进来的不是——尾字不在这个集合里
#: 且只吞了 1 个字时，判定为误吞并截断。
_CROSSLINE_TAIL_OK = frozenset(
    "院司厂店行部所会社团站馆园心校学省市县区镇乡村路街道巷号局委办厅系"
    "厂矿场台网行业会队组科系部处室中心公司集团学校医院银行"
)

#: 文档级附属内容（书签、批注）的定位类型，不参与页面 redaction。
AUX_LOCATORS = (PdfTocLocator, PdfAnnotLocator)


@dataclass(slots=True)
class _Placeholder:
    """待写入的一条代号（apply 内部使用）。

    rect      : 实际落笔的那一行矩形（跨行实体取字符最多的那一行）
    text      : 代号
    font_size : 原文字号
    baseline  : 该行基线
    original  : 原文（扫描页二次 OCR 复核用）
    safe_right: 右侧第一个字符的左边界；None 表示行尾，可用到页边
    crossline : 实体是否横跨多行
    """

    rect: pymupdf.Rect
    text: str
    font_size: float
    baseline: float
    original: str = ""
    safe_right: float | None = None
    crossline: bool = False


# ---------------------------------------------------------------------------
# 字符宽度估算（扫描件专用）
# ---------------------------------------------------------------------------


def _char_weight(ch: str) -> float:
    """估算单个字符的相对宽度。

    扫描件没有字符级坐标，只能按「中文约等于一个字宽、数字/字母约半个字宽」切分。
    比 naively 平均分配准得多：身份证号 18 位数字挤在「姓名」两个字宽里时，
    均分会让擦除框偏移近一倍。
    """
    if not ch:
        return 1.0
    if ch.isspace():
        return 0.33
    code = ord(ch)
    if code >= 0x2E80:  # CJK 汉字、全角标点
        return 1.0
    if code >= 0x80:  # 其他非 ASCII（希腊字母、注音等）
        return 0.8
    if ch in ".,;:!?()[]{}-/'\"|":
        return 0.35
    # 数字/字母：实测 CJK 字体的半角数字宽约 0.62em（宋体/黑体/仿宋都在 0.5~0.65 之间）。
    # 取 0.62 而不是 0.5：低估数字宽度会把前面的汉字整体拉宽，实体起点系统性偏右，
    # 实测「统一社会信用代码：9133…」的首位数字会整个漏擦。
    return 0.62


def _split_rect_by_chars(
    text: str, rect: tuple[float, float, float, float]
) -> tuple[tuple[float, float, float, float], ...]:
    """把一行矩形按字符权重切成逐字符矩形（扫描件坐标估算）。"""
    x0, y0, x1, y1 = rect
    width = max(0.0, x1 - x0)
    weights = [_char_weight(ch) for ch in text]
    total = sum(weights) or 1.0
    boxes: list[tuple[float, float, float, float]] = []
    cursor = x0
    for weight in weights:
        nxt = cursor + width * weight / total
        boxes.append((cursor, y0, nxt, y1))
        cursor = nxt
    return tuple(boxes)


# ---------------------------------------------------------------------------
# 处理器
# ---------------------------------------------------------------------------


class PdfHandler:
    """PDF 脱敏处理器（满足 FormatHandler 协议）。"""

    name = "pdf"
    suffixes = (".pdf",)

    def __init__(self) -> None:
        #: 最近一次 extract/apply 过程中产生的发现。apply 没有返回值，
        #: 页级失败、定位失败、残留复核等结果通过这里交给 audit / CLI 汇总。
        self._last_findings: list[Finding] = []
        #: 扫描页擦除后的二次 OCR 复核开关。默认开启：扫描件残留是最高危的失败模式，
        #: 多花 1~2s/页换一条「疑似残留」告警是值得的。
        self.ocr_verify = True

    # ------------------------------------------------------------------ 公共接口

    def extract(self, path: str) -> list[TextBlock]:
        """提取全文可检测文本块。加密/损坏文件返回空列表并记录 Finding。"""
        self._last_findings = []
        doc, _ = self._open(path)
        if doc is None:
            return []

        blocks: list[TextBlock] = []
        try:
            for page_no in range(doc.page_count):
                try:
                    page = doc[page_no]
                except Exception as exc:  # 损坏页：跳过，不影响其余页
                    self._last_findings.append(
                        Finding("warn", "pdf.page_open_failed", f"第 {page_no + 1} 页无法打开：{exc}", f"page={page_no + 1}")
                    )
                    continue
                try:
                    blocks.extend(self._extract_page(page, page_no))
                except Exception as exc:
                    self._last_findings.append(
                        Finding("warn", "pdf.page_extract_failed", f"第 {page_no + 1} 页提取失败：{exc}", f"page={page_no + 1}")
                    )
            # 页面内容流之外的文本：书签标题、批注正文。get_text() 读不到它们，
            # 但目录面板和批注气泡会把原文原样展示，必须一并参与检测。
            blocks.extend(self._extract_toc(doc))
            blocks.extend(self._extract_annots(doc))
        finally:
            doc.close()
        return blocks

    def apply(self, path: str, out_path: str, edits: Sequence[Edit]) -> None:
        """按编辑指令真实擦除原文、写入代号，另存到 out_path。

        源文件永远只读打开；``out_path`` 与源文件相同会直接拒绝。
        单页失败只记录 Finding 并继续，不会让整份文件失败。
        """
        self._last_findings = []
        if os.path.abspath(path) == os.path.abspath(out_path):
            raise ValueError("out_path 不能与源文件相同：本工具绝不修改原文件")

        doc, _ = self._open(path)
        if doc is None:
            # 加密或损坏：没有可处理的内容，明确失败而不是产出一个看似成功的空文件
            raise RuntimeError(f"PDF 无法处理（已加密或损坏）：{path}")

        try:
            by_page, toc_edits, annot_edits, orphan = self._group_by_page(edits)
            if orphan:
                self._last_findings.append(
                    Finding("warn", "pdf.edit_without_locator", f"{orphan} 条编辑缺少有效的 PDF 定位信息，已跳过")
                )

            for page_no in sorted(by_page):
                if page_no >= doc.page_count:
                    self._last_findings.append(
                        Finding("warn", "pdf.bad_page", f"第 {page_no + 1} 页不存在，相关编辑已跳过", f"page={page_no + 1}")
                    )
                    continue
                try:
                    self._apply_page(doc, doc[page_no], by_page[page_no])
                except Exception as exc:
                    self._last_findings.append(
                        Finding("critical", "pdf.page_apply_failed", f"第 {page_no + 1} 页脱敏失败：{exc}", f"page={page_no + 1}")
                    )

            # 书签与批注不在页面内容流里，redaction 擦不到，必须单独改写
            self._apply_toc(doc, toc_edits)
            self._apply_annots(doc, annot_edits)
            self._scrub_metadata(doc, edits)
            self._save(doc, out_path)
        finally:
            doc.close()

        self._verify_output(out_path, edits)

    def audit(self, path: str) -> list[Finding]:
        """审计文件级泄露点：元数据、批注、书签、内嵌文件、表单域、图片、加密。"""
        findings: list[Finding] = []
        try:
            doc = pymupdf.open(path)
        except Exception as exc:
            return [Finding("critical", "pdf.unreadable", f"无法打开 PDF：{exc}", path)]

        try:
            if doc.needs_pass:
                return [
                    Finding(
                        "critical",
                        "pdf.encrypted",
                        "PDF 已加密，无法读取内容，也无法脱敏。请先取得密码并另存为未加密副本。",
                        path,
                    )
                ]

            findings.extend(self._audit_metadata(doc))
            findings.extend(self._audit_document(doc))

            # 图片汇总成一条：逐页各报一条会把真正需要关注的告警淹没
            image_pages: list[int] = []
            image_total = 0
            for page_no in range(doc.page_count):
                try:
                    page = doc[page_no]
                    count = len(page.get_images(full=True) or [])
                except Exception:
                    count = 0
                if count:
                    image_pages.append(page_no + 1)
                    image_total += count
                try:
                    findings.extend(self._audit_page(page, page_no))
                except Exception as exc:
                    findings.append(
                        Finding("warn", "pdf.page_audit_failed", f"第 {page_no + 1} 页审计失败：{exc}", f"page={page_no + 1}")
                    )
            if image_pages:
                preview = "、".join(str(p) for p in image_pages[:5])
                more = f" 等 {len(image_pages)} 页" if len(image_pages) > 5 else ""
                findings.append(
                    Finding(
                        "warn",
                        "pdf.image",
                        f"全文 {len(image_pages)} 页共 {image_total} 张图片（{preview}{more}），"
                        "图像内容无法逐字识别脱敏，只能按 OCR 区域擦除；图上的照片、印章、手写签名不在本工具保障范围内",
                        "images",
                    )
                )
        finally:
            doc.close()

        # 把最近一次 extract/apply 的发现并进来（去重），让 CLI 只调 audit 也能看到
        findings.extend(self._last_findings)
        return self._dedupe(findings)

    @property
    def last_findings(self) -> list[Finding]:
        """最近一次 extract/apply 产生的发现（副本）。"""
        return list(self._last_findings)

    # ------------------------------------------------------------------ 打开

    def _open(self, path: str) -> tuple[Any, Any]:
        try:
            doc = pymupdf.open(path)
        except Exception as exc:
            self._last_findings.append(Finding("critical", "pdf.unreadable", f"无法打开 PDF：{exc}", path))
            return None, exc
        if doc.needs_pass:
            doc.close()
            finding = Finding(
                "critical",
                "pdf.encrypted",
                "PDF 已加密，无法处理，需密码。请先解密为未加密副本后再脱敏。",
                path,
            )
            self._last_findings.append(finding)
            return None, finding
        return doc, None

    # ------------------------------------------------------------------ 提取

    def _extract_page(self, page: Any, page_no: int) -> list[TextBlock]:
        try:
            text = page.get_text().strip()
        except Exception:
            text = ""

        if self._is_scan_page(page, text):
            blocks = self._extract_ocr_page(page, page_no)
            if blocks:
                return blocks
            # OCR 一个字也没识别出来：退回文本层，至少把不满 20 字的短文本带上
        return self._extract_text_page(page, page_no)

    @staticmethod
    def _is_scan_page(page: Any, text: str) -> bool:
        """判定扫描页。

        不能只看字符数：判决书封面可能只有「民事判决书」5 个字，那是文本页，
        送去 OCR 只会引入字符级误差。判据是「几乎没有文字 + 页面确实是一张图」。
        """
        if not text:
            return True
        if len(text) >= SCAN_TEXT_THRESHOLD:
            return False
        try:
            return bool(page.get_images(full=True))
        except Exception:
            return False

    def _extract_text_page(self, page: Any, page_no: int) -> list[TextBlock]:
        """文本层提取：以「行」为单位组装 TextBlock。"""
        try:
            raw = page.get_text("rawdict")
        except Exception:
            return []

        page_height = float(page.rect.height)
        blocks: list[TextBlock] = []
        for block in raw.get("blocks", []):
            if block.get("type") != 0:  # 1 = 图像块
                continue
            for line in block.get("lines", []):
                if tuple(round(v, 3) for v in line.get("dir", (1.0, 0.0))) != (1.0, 0.0):
                    # 竖排/旋转文字：字符框坐标系与页面不同，记录并跳过，避免擦错位置
                    self._last_findings.append(
                        Finding("warn", "pdf.rotated_text", f"第 {page_no + 1} 页存在非水平文本行，已跳过", f"page={page_no + 1}")
                    )
                    continue

                pieces: list[str] = []
                boxes: list[tuple[float, float, float, float]] = []
                baseline = 0.0
                sizes: list[float] = []
                chars: list[tuple[str, tuple[float, float, float, float]]] = []
                for span in line.get("spans", []):
                    size = float(span.get("size") or 0.0)
                    if size > 0:
                        sizes.append(size)
                    for ch in span.get("chars", []):
                        char = ch.get("c") or ""
                        if not char:
                            continue
                        chars.append((char, tuple(float(v) for v in ch.get("bbox", (0, 0, 0, 0)))))
                        if baseline == 0.0:
                            origin = ch.get("origin") or (0.0, 0.0)
                            baseline = float(origin[1])

                # 判决书的审判人员署名常排成「人民陪审员　　张　丽」——姓名两字之间
                # 用全角空格对齐。get_text() 里因此没有连续「张丽」，检测器按连续
                # 中文匹配姓名时会整个漏掉。这里把「汉字—空格—汉字」之间的空格段
                # 折叠掉（同时丢弃对应字符框，保持 text 与 char_boxes 对齐）；
                # 只动汉字之间的空格，英文、数字、标点间的空格原样保留。
                text, boxes = _drop_cjk_inner_spaces(chars)
                if not text.strip() or not boxes:
                    continue
                if not text.strip() or not boxes:
                    continue

                line_bbox = tuple(float(v) for v in line.get("bbox", (0, 0, 0, 0)))
                if line_bbox[3] <= 0.0:  # 异常 bbox，用字符并集兜底
                    line_bbox = _union(boxes)

                role = "body"
                if page_height > 0 and line_bbox[3] < page_height * 0.06:
                    role = "header"
                elif page_height > 0 and line_bbox[1] > page_height * 0.94:
                    role = "footer"

                blocks.append(
                    TextBlock(
                        text=text,
                        locator=PdfLocator(
                            page_no=page_no,
                            bbox=line_bbox,
                            char_boxes=tuple(boxes),
                            baseline=baseline or line_bbox[3] - 2.0,
                            font_size=max(sizes) if sizes else 10.0,
                            source="text",
                        ),
                        role=role,
                    )
                )
        return self._merge_adjacent_lines(blocks)

    # ------------------------------------------------------------------ 跨行合并

    def _merge_adjacent_lines(self, blocks: list[TextBlock]) -> list[TextBlock]:
        """把页内相邻行为合并成「检测用块」。

        为什么必须合并：法院文书排版里「云南国防工业职业技术学 / 院」这种跨行断词
        极多（真实材料 34 页 583 处）。按行切块时检测器看不到完整机构名，而残留
        审计规则只认「有限公司/律师事务所」等后缀，「学 院」半截匹配不上，
        于是报告显示「零 critical」但实体漏掉了。

        合并只影响检测：``PdfLocator.line_breaks`` 记录每个原始行的字符边界，
        回写时按行拆成多个 redaction 矩形，代号仍完整写在字符最多的那一行。
        """
        if len(blocks) < 2:
            return blocks

        merged: list[TextBlock] = []
        current = blocks[0]
        last_bbox = current.locator.bbox if isinstance(current.locator, PdfLocator) else None
        for block in blocks[1:]:
            if (
                isinstance(current.locator, PdfLocator)
                and isinstance(block.locator, PdfLocator)
                and last_bbox is not None
                and _can_merge_lines(current, last_bbox, block)
            ):
                current = _merge_two_blocks(current, block)
                last_bbox = block.locator.bbox
            else:
                merged.append(current)
                current = block
                last_bbox = block.locator.bbox if isinstance(block.locator, PdfLocator) else None
        merged.append(current)
        return merged

    def _extract_ocr_page(self, page: Any, page_no: int) -> list[TextBlock]:
        """扫描页提取：300dpi 渲染 → OCR → 换算回 PDF 坐标。

        内存纪律：pixmap 单页 26MB，用完立刻 del；不缓存任何渲染结果。
        """
        try:
            pix = page.get_pixmap(dpi=RENDER_DPI, alpha=False)
        except Exception as exc:
            self._last_findings.append(
                Finding("warn", "pdf.render_failed", f"第 {page_no + 1} 页渲染失败，无法 OCR：{exc}", f"page={page_no + 1}")
            )
            return []
        try:
            png = pix.tobytes("png")
        finally:
            del pix

        try:
            lines: list[OcrLine] = recognize(png)
        except Exception as exc:
            self._last_findings.append(
                Finding("warn", "pdf.ocr_failed", f"第 {page_no + 1} 页 OCR 失败：{exc}", f"page={page_no + 1}")
            )
            return []

        blocks: list[TextBlock] = []
        for line in lines:
            x0, y0, x1, y1 = (v * _PT_PER_PX for v in line.box)
            if x1 - x0 < 1.0 or y1 - y0 < 1.0:
                continue
            rect = (x0, y0, x1, y1)
            height = y1 - y0
            # 字号估算：RapidOCR 的检测框比字形本身高约 20~30%（含行距），
            # 0.78 是把框高还原成字号的实测系数。
            font_size = min(48.0, max(5.0, height * 0.78))
            baseline = y1 - height * 0.32
            ocr_text, ocr_boxes = _drop_cjk_inner_spaces(
                list(zip(line.text, _split_rect_by_chars(line.text, rect)))
            )
            if not ocr_text.strip() or not ocr_boxes:
                continue
            blocks.append(
                TextBlock(
                    text=ocr_text,
                    locator=PdfLocator(
                        page_no=page_no,
                        bbox=rect,
                        char_boxes=tuple(ocr_boxes),
                        baseline=baseline,
                        font_size=font_size,
                        source="ocr",
                        ocr_score=line.score,
                    ),
                    role="body",
                )
            )
        return blocks

    # ------------------------------------------------------------------ 回写

    def _group_by_page(
        self, edits: Sequence[Edit]
    ) -> tuple[dict[int, list[Edit]], list[Edit], list[Edit], int]:
        """把编辑按「页面 / 书签 / 批注」分流。返回值末位是无法识别的孤儿数。"""
        by_page: dict[int, list[Edit]] = {}
        toc_edits: list[Edit] = []
        annot_edits: list[Edit] = []
        orphan = 0
        for edit in edits:
            locator = getattr(edit.block, "locator", None)
            if isinstance(locator, PdfLocator):
                by_page.setdefault(locator.page_no, []).append(edit)
            elif isinstance(locator, PdfTocLocator):
                toc_edits.append(edit)
            elif isinstance(locator, PdfAnnotLocator):
                annot_edits.append(edit)
            else:
                orphan += 1
        return by_page, toc_edits, annot_edits, orphan

    # ------------------------------------------------------------------ 书签 / 批注

    def _extract_toc(self, doc: Any) -> list[TextBlock]:
        """把每一层书签标题当作独立的 TextBlock（role=metadata）。"""
        blocks: list[TextBlock] = []
        try:
            toc = doc.get_toc() or []
        except Exception:
            return blocks
        for index, item in enumerate(toc):
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            title = str(item[1] or "")
            if not title.strip():
                continue
            try:
                page_no = max(0, int(item[2]) - 1)
            except (TypeError, ValueError):
                page_no = 0
            blocks.append(
                TextBlock(
                    text=title,
                    locator=PdfTocLocator(index=index, page_no=page_no),
                    role="metadata",
                )
            )
        return blocks

    def _extract_annots(self, doc: Any) -> list[TextBlock]:
        """把批注的正文/标题/主题当作 TextBlock（role=comment）。"""
        blocks: list[TextBlock] = []
        for page_no in range(doc.page_count):
            try:
                annots = list(doc[page_no].annots() or [])
            except Exception:
                continue
            for annot in annots:
                try:
                    info = annot.info or {}
                    xref = int(annot.xref)
                except Exception:
                    continue
                for field in ("content", "title", "subject"):
                    value = str(info.get(field) or "")
                    if not value.strip():
                        continue
                    blocks.append(
                        TextBlock(
                            text=value,
                            locator=PdfAnnotLocator(page_no=page_no, xref=xref, field=field),
                            role="comment",
                        )
                    )
        return blocks

    def _apply_toc(self, doc: Any, edits: Sequence[Edit]) -> None:
        """按编辑改写书签标题，保持 [层级, 页码] 不变。"""
        if not edits:
            return
        try:
            toc = doc.get_toc() or []
        except Exception as exc:
            self._last_findings.append(Finding("warn", "pdf.toc_read_failed", f"读取书签失败：{exc}", "toc"))
            return

        by_index: dict[int, list[Edit]] = {}
        for edit in edits:
            locator = edit.block.locator
            if isinstance(locator, PdfTocLocator):
                by_index.setdefault(locator.index, []).append(edit)

        changed = 0
        for index, group in by_index.items():
            if not (0 <= index < len(toc)):
                continue
            row = list(toc[index])
            title = str(row[1] or "")
            for edit in sorted(group, key=lambda e: e.entity.start, reverse=True):
                start, end = edit.entity.start, edit.entity.end
                if 0 <= start < end <= len(title):
                    title = title[:start] + edit.replacement + title[end:]
            if title != row[1]:
                row[1] = title
                toc[index] = row
                changed += 1

        if not changed:
            return
        try:
            doc.set_toc(toc)
        except Exception as exc:
            self._last_findings.append(Finding("warn", "pdf.toc_write_failed", f"写回书签失败：{exc}", "toc"))
            return
        self._last_findings.append(
            Finding("info", "pdf.toc_scrubbed", f"已改写 {changed} 条书签标题", "toc")
        )

    def _apply_annots(self, doc: Any, edits: Sequence[Edit]) -> None:
        """按编辑改写批注的文本字段（content/title/subject）。"""
        if not edits:
            return
        by_page: dict[int, list[Edit]] = {}
        for edit in edits:
            locator = edit.block.locator
            if isinstance(locator, PdfAnnotLocator):
                by_page.setdefault(locator.page_no, []).append(edit)

        changed = 0
        for page_no, group in by_page.items():
            if not (0 <= page_no < doc.page_count):
                continue
            try:
                # 必须直接迭代生成器：list() 之后 Annot 会与页面解绑，
                # update() 会抛 "annotation not bound to any page"。
                annots = doc[page_no].annots() or []
            except Exception:
                continue
            for annot in annots:
                try:
                    xref = int(annot.xref)
                except Exception:
                    continue
                matches = [e for e in group if e.block.locator.xref == xref]
                if not matches:
                    continue
                try:
                    info = dict(annot.info or {})
                except Exception:
                    continue
                updated = False
                for field in ("content", "title", "subject"):
                    field_edits = [e for e in matches if e.block.locator.field == field]
                    if not field_edits:
                        continue
                    value = str(info.get(field) or "")
                    if not value:
                        continue
                    original = value
                    for edit in sorted(field_edits, key=lambda e: e.entity.start, reverse=True):
                        start, end = edit.entity.start, edit.entity.end
                        if 0 <= start < end <= len(value):
                            value = value[:start] + edit.replacement + value[end:]
                    if value != original:
                        info[field] = value
                        updated = True
                if not updated:
                    continue
                try:
                    annot.set_info(
                        **{key: info[key] for key in ("content", "title", "subject") if key in info}
                    )
                    annot.update()
                    changed += 1
                except Exception as exc:
                    self._last_findings.append(
                        Finding("warn", "pdf.annot_write_failed", f"第 {page_no + 1} 页批注改写失败：{exc}", f"page={page_no + 1}")
                    )
        if changed:
            self._last_findings.append(
                Finding("info", "pdf.annots_scrubbed", f"已改写 {changed} 条批注文本", "annots")
            )

    def _apply_page(self, doc: Any, page: Any, edits: Sequence[Edit]) -> None:
        """单页：先对本页所有实体一次性 redaction，再统一写代号。

        顺序不能反：apply_redactions 会清空页面内容流中被覆盖的部分，
        先写后擦会把刚写上的代号一起擦掉。
        """
        # 扫描页需要在像素层面校准擦除框：OCR 的字符坐标是按宽度权重估出来的，
        # 汉字与数字混排时累计误差可达一两个字宽（实测信用代码首字符残留）。
        # 渲染一次灰度图，对每个实体的左右边界做投影吸附。
        gray = None
        if any(locator_is_ocr(edit) for edit in edits):
            try:
                gray = self._page_gray(page)
            except Exception as exc:
                self._last_findings.append(
                    Finding("warn", "pdf.gray_render_failed", f"第 {edit_page(edits)} 页灰度渲染失败，按估算坐标擦除：{exc}")
                )

        items: list[_Placeholder] = []
        for edit in edits:
            segments = self._resolve_segments(page, edit)
            if not segments:
                self._last_findings.append(
                    Finding(
                        "warn",
                        "pdf.locate_failed",
                        f"第 {edit.block.locator.page_no + 1} 页无法定位实体（{edit.entity.type.value}），已跳过该处",
                        f"page={edit.block.locator.page_no + 1}",
                    )
                )
                continue

            # 跨行实体按行拆成多个擦除矩形；代号完整写在字符最多的那一行
            main_rect, _, _, main_baseline = max(segments, key=lambda seg: seg[2] - seg[1])
            if main_rect[2] - main_rect[0] < 0.3 or main_rect[3] - main_rect[1] < 0.3:
                self._last_findings.append(
                    Finding(
                        "warn",
                        "pdf.locate_failed",
                        f"第 {edit.block.locator.page_no + 1} 页实体矩形过小（{edit.entity.type.value}），已跳过该处",
                        f"page={edit.block.locator.page_no + 1}",
                    )
                )
                continue

            added = 0
            for rect, _, _, _ in segments:
                redact_rect = pymupdf.Rect(rect)
                if gray is not None and locator_is_ocr(edit):
                    redact_rect = pymupdf.Rect(_refine_ocr_rect(gray, tuple(redact_rect)))
                # 扫描件的字符坐标是按宽度权重估出来的，混排（中文 + 18 位数字）时
                # 累计误差可达半个字符宽，实测会出现「信用代码首字符 9 残留」。
                # 擦除框左右各放一段余量：多擦一点不致泄露，少擦一个字就是事故。
                if locator_is_ocr(edit):
                    unit = float(redact_rect[3]) - float(redact_rect[1])
                    ratio = 0.5 if any(ch.isascii() and ch.isalnum() for ch in edit.entity.text) else 0.3
                    pad = min(6.0, max(1.5, unit * ratio))
                    redact_rect.x0 = max(float(page.rect.x0) + 0.5, redact_rect.x0 - pad)
                    redact_rect.x1 = min(float(page.rect.x1) - 0.5, redact_rect.x1 + pad)
                try:
                    page.add_redact_annot(redact_rect)
                    added += 1
                except Exception as exc:
                    self._last_findings.append(
                        Finding(
                            "warn",
                            "pdf.redact_annot_failed",
                            f"第 {edit.block.locator.page_no + 1} 页添加擦除标记失败：{exc}",
                            f"page={edit.block.locator.page_no + 1}",
                        )
                    )
            if added == 0:
                continue

            crossline = len(segments) > 1
            if crossline:
                self._last_findings.append(
                    Finding(
                        "warn",
                        "pdf.placeholder_crossline",
                        f"第 {edit.block.locator.page_no + 1} 页实体跨 {len(segments)} 行，已逐行擦除、"
                        "代号完整写在字符最多的一行",
                        f"page={edit.block.locator.page_no + 1}",
                    )
                )
            locator = edit.block.locator
            items.append(
                _Placeholder(
                    rect=pymupdf.Rect(main_rect),
                    text=edit.replacement,
                    font_size=locator.font_size,
                    baseline=main_baseline,
                    original=edit.entity.text,
                    safe_right=None if crossline else self._safe_right(edit),
                    crossline=crossline,
                )
            )

        if not items:
            return

        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )

        # 用 insert_text 而不是 TextWriter：TextWriter 不支持水平缩放，
        # 而 6pt 的 4 字代号（24pt）放进 2 字姓名宽度（约 21.5pt）还差 2.5pt。
        # insert_text 的 morph 矩阵能把字形压到 0.75~1.0 倍宽，既不压相邻字符，
        # 也不用把字号降到 6pt 以下。
        try:
            page.insert_font(fontname=_PAGE_FONT_NAME, fontbuffer=_font().buffer)
        except Exception as exc:
            self._last_findings.append(
                Finding("warn", "pdf.font_register_failed", f"第 {edit_page(edits)} 页字体注册失败：{exc}")
            )
        for item in sorted(items, key=lambda it: (it.rect.y0, it.rect.x0)):
            try:
                self._append_placeholder(
                    page, item.rect, item.text, item.font_size, item.baseline, item.safe_right
                )
            except Exception as exc:
                self._last_findings.append(
                    Finding("warn", "pdf.write_failed", f"第 {edit_page(edits)} 页写入代号失败：{exc}")
                )

        if any(isinstance(e.block.locator, PdfLocator) and e.block.locator.is_ocr for e in edits):
            self._verify_ocr_page(page, edits, [(item.rect, item.original, item.text) for item in items])

    @staticmethod
    def _page_gray(page: Any) -> Any:
        """把整页渲染成 300dpi 灰度数组，用于像素级边界校准。"""
        import numpy as np

        pix = page.get_pixmap(dpi=RENDER_DPI, colorspace=pymupdf.csGRAY, alpha=False)
        try:
            array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
            return array.copy()  # pix 释放后 samples 缓冲失效，必须拷贝
        finally:
            del pix

    @staticmethod
    def _safe_right(edit: Edit) -> float | None:
        """实体右侧第一个字符的左边界（硬边界），行尾返回 None。

        旧实现允许代号「吃掉」紧跟的标点，结果「原告：【熊猫】，男」里的逗号
        被代号盖住——文本层没丢字，但渲染图看起来句子结构断了。现在严格按
        「到下一个字符为止」：代号只允许落在自己替换掉的那段宽度里，不够就
        缩字号；右侧空白只能当作额外画布，绝不能压到下一个字符。
        """
        locator = edit.block.locator
        if not isinstance(locator, PdfLocator):
            return None
        boxes = locator.char_boxes
        if len(boxes) != len(edit.block.text):
            return None
        end = edit.entity.end
        if end <= 0 or end >= len(boxes):
            return None
        return float(boxes[end][0])

    def _effective_span(self, edit: Edit) -> tuple[int, int]:
        """返回实体实际允许擦除的区间，拦截「跨行多吞一个字」的误伤。

        跨行合并后 ``block.text`` 把两行直接接起来（CJK 之间不插分隔符），
        姓名检测器会把上一行末字和下一行首字连读：判决书落款
        「书记员 周九 / 二〇二四年六月十八日」被拼成「周九二〇二四年…」，
        于是识别出 3 字姓名「周九二」，redaction 跨行把日期首字「二」擦掉，
        「二〇二四年」变成「〇二四年」——日期失效，而残留审计只扫阿拉伯数字，
        不会报警。这里做 PDF 层兜底：

        * person 实体一律不允许跨行（真实文书里姓名不会断行）；
        * 其他类型若跨行后下一行首字是中文数字/金额字符，也截断到行边界，
          避免机构名把「人民币壹万元整」的「壹」吞掉。

        截断只影响擦除范围，不影响代号内容（代号仍按原实体生成）。
        """
        start, end = self._entity_span(edit)
        if start is None or end is None:
            return edit.entity.start, edit.entity.end
        locator = getattr(edit.block, "locator", None)
        text = edit.block.text
        if not isinstance(locator, PdfLocator) or not locator.is_merged or not locator.line_breaks:
            return start, end
        row_end = next((b for b in locator.line_breaks if b > start), None)
        if row_end is None or end <= row_end:
            return start, end
        crossed = text[row_end] if row_end < len(text) else ""
        if edit.entity.type is EntityType.PERSON or crossed in _CJK_NUMERALS:
            return start, row_end
        # 只多吞了 1 个字、且这个字不是机构/地名后缀 → 判定为误吞下一行正文首字
        if (end - row_end) <= 1 and crossed not in _CROSSLINE_TAIL_OK:
            return start, row_end
        return start, end

    def _entity_span(self, edit: Edit) -> tuple[int | None, int | None]:
        """返回实体在块文本中的实际擦除区间。

        案号是唯一会「一段文字被检测器拆成两半」的实体类型：真实文书里常见
        ``（2025）浙0192民初XX号`` 这类序号非纯数字的变体，检测器可能只匹配到
        前半截。按半截矩形擦除会留下另一半（比不擦更糟：剩下的字符仍可识别，
        还破坏了版面）。所以对 case_no 做一次行内完整模式回溯，能扩就扩到整段。

        其余类型原样返回字符偏移。
        """
        start, end = edit.entity.start, edit.entity.end
        if edit.entity.type is not EntityType.CASE_NO:
            return start, end
        span = _case_no_span(edit.block.text, start, end)
        if span is None:
            return start, end
        return span

    def _resolve_segments(
        self, page: Any, edit: Edit
    ) -> list[tuple[tuple[float, float, float, float], int, int, float]]:
        """把实体拆成「逐行矩形」：[(rect, 字符起, 字符止, 该行基线)]。

        跨行合并块里，一个机构名可能横跨两行。redaction 矩形若用整体并集，
        会把两行之间的整片区域（含中间行文字）一起擦掉；按行拆开则只擦实体
        真正占据的字符。返回的每段都带自己的 baseline，写代号时不会用错行。
        """
        locator = getattr(edit.block, "locator", None)
        if not isinstance(locator, PdfLocator):
            return []
        text = edit.block.text
        raw_start, raw_end = self._entity_span(edit)
        if raw_start is None or raw_end is None:
            raw_start, raw_end = edit.entity.start, edit.entity.end
        start, end = self._effective_span(edit)
        if (end - start) < (raw_end - raw_start):
            self._last_findings.append(
                Finding(
                    "warn",
                    "pdf.crossline_truncated",
                    f"第 {locator.page_no + 1} 页跨行实体「{edit.entity.type.value}」被截断到行边界，"
                    "避免吞掉下一行的中文数字日期/金额",
                    f"page={locator.page_no + 1}",
                )
            )
        boxes = locator.char_boxes

        if not (0 <= start < end <= len(boxes)) or len(boxes) != len(text):
            rect = self._resolve_rect(page, edit)
            return [(rect, start, end, locator.baseline)] if rect else []

        if not locator.is_merged or not locator.line_breaks:
            return [(_union(boxes[start:end]), start, end, locator.baseline)]

        bounds = (0,) + tuple(locator.line_breaks)
        segments: list[tuple[tuple[float, float, float, float], int, int, float]] = []
        for index in range(len(bounds) - 1):
            lo, hi = bounds[index], bounds[index + 1]
            seg_start, seg_end = max(start, lo), min(end, hi)
            if seg_end <= seg_start:
                continue
            baseline = (
                locator.line_baselines[index]
                if index < len(locator.line_baselines)
                else locator.baseline
            )
            segments.append((_union(boxes[seg_start:seg_end]), seg_start, seg_end, baseline))
        if not segments:
            return [(_union(boxes[start:end]), start, end, locator.baseline)]
        return segments

    def _resolve_rect(self, page: Any, edit: Edit) -> tuple[float, float, float, float] | None:
        """把「块内字符偏移」翻译成页面矩形。

        三级策略：字符坐标（最准）→ search_for（退化）→ 行内均分（兜底）。
        """
        locator: PdfLocator = edit.block.locator
        text = edit.block.text
        start, end = self._effective_span(edit)

        if 0 <= start < end <= len(locator.char_boxes) and len(locator.char_boxes) == len(text):
            return _union(locator.char_boxes[start:end])

        # 字符框缺失或与文本长度不匹配：退回字符串搜索（同一行内用 clip 限位）
        try:
            clip = pymupdf.Rect(locator.bbox)
            clip.x0 -= 1.0
            clip.y0 -= 1.0
            clip.x1 += 1.0
            clip.y1 += 1.0
            hits = page.search_for(edit.entity.text, clip=clip)
            if hits:
                return tuple(hits[0])
        except Exception:
            pass

        # 最后兜底：行内按字符权重均分
        if len(text) > 0:
            boxes = _split_rect_by_chars(text, locator.bbox)
            if 0 <= start < end <= len(boxes):
                return _union(boxes[start:end])
        return None

    def _append_placeholder(
        self,
        page: Any,
        rect: tuple[float, float, float, float],
        replacement: str,
        font_size: float,
        baseline: float,
        safe_right: float | None = None,
    ) -> None:
        """在擦除位置写入带框代号，严格限制在「自己替换掉的那段原文宽度」内。

        代号通常比原文长（张三 → 【熊猫】是 2 字变 4 字）。旧版允许向右吃掉
        后面的标点甚至汉字，文本层虽然没丢字，但渲染出来「原告：【熊猫】男」
        会被看成「原告是【熊猫】男」——这是脱敏工具制造的语义污染。现在的顺序：

        1. 先按原字号画；放不下就缩字号，下限 6pt；
        2. 6pt 仍放不下时，按可用宽度做水平压缩（morph），最多压到 0.75 倍宽；
        3. 压缩后仍超出时才占用右侧空白，最后一格才轻微压字并记
           ``pdf.placeholder_tight``。

        绝不因为放不下就漏写代号：漏写会让下游模型把这块空白当成「原文本来
        就没有」。
        """
        text = (replacement or "").strip()
        if not text:
            text = "XXX"
        font = _font()

        page_left = float(page.rect.x0) + 1.0
        page_right = float(page.rect.x1) - 1.0
        x0 = max(float(rect[0]), page_left)
        rect_width = max(0.1, float(rect[2]) - float(rect[0]))
        hard_right = float(safe_right) if safe_right is not None else page_right
        hard_right = min(hard_right, page_right)
        blank = max(0.0, hard_right - float(rect[2]))

        base = float(font_size or 10.0)
        floor = min(base, 6.0)
        size = base
        width = font.text_length(text, fontsize=size)
        if width > rect_width and width > 0:
            size = max(floor, size * rect_width / width)
            width = font.text_length(text, fontsize=size)

        scale = 1.0
        if width > rect_width and width > 0:
            scale = max(0.75, rect_width / width)
            if width * scale > rect_width + blank:
                self._last_findings.append(
                    Finding(
                        "warn",
                        "pdf.placeholder_tight",
                        f"代号「{text}」缩到 {size:.1f}pt、压缩到 {scale:.2f} 倍宽仍超出，"
                        f"可用 {rect_width:.1f}pt、右侧空白 {blank:.1f}pt，已按最小压缩比例绘制",
                        f"bbox=({float(rect[0]):.1f},{float(rect[1]):.1f})",
                    )
                )

        y = float(baseline)
        if not (0.0 < y <= float(page.rect.y1)):
            y = float(rect[3]) - max(1.0, size * 0.18)

        morph = None
        if scale < 0.999:
            # 围绕插入点做水平缩放：起点不动，只把字形压窄
            morph = (pymupdf.Point(x0, y), pymupdf.Matrix(scale, 0, 0, 1, 0, 0))
        page.insert_text(
            pymupdf.Point(x0, y),
            text,
            fontsize=size,
            fontname=_PAGE_FONT_NAME,
            color=(0, 0, 0),
            overlay=True,
            morph=morph,
        )

    # ------------------------------------------------------------------ 元数据

    def _scrub_metadata(self, doc: Any, edits: Sequence[Edit]) -> None:
        """把元数据里出现的「本次已脱敏原文」替换成代号。

        只替换已知实体，不整段清空：title 里的案由、subject 里的保密级别
        对下游分析有用，清空反而损失信息。未被检测到的敏感字段由 audit 报告。
        """
        mapping: dict[str, str] = {}
        for edit in edits:
            if edit.entity.text:
                mapping.setdefault(edit.entity.text, edit.replacement)
        if not mapping:
            return

        try:
            metadata = dict(doc.metadata or {})
        except Exception:
            return

        changed: list[str] = []
        for key in ("author", "title", "subject", "creator", "producer", "keywords"):
            value = metadata.get(key) or ""
            if not value:
                continue
            new_value = value
            for original, replacement in mapping.items():
                if original and original in new_value:
                    new_value = new_value.replace(original, replacement or "XXX")
            if new_value != value:
                metadata[key] = new_value
                changed.append(key)

        if not changed:
            return
        try:
            doc.set_metadata(metadata)
        except Exception as exc:
            self._last_findings.append(
                Finding("warn", "pdf.metadata_scrub_failed", f"元数据清理失败：{exc}", "metadata")
            )
            return
        self._last_findings.append(
            Finding("info", "pdf.metadata_scrubbed", f"已清理元数据字段：{', '.join(changed)}", "metadata")
        )

    # ------------------------------------------------------------------ 保存

    def _snapshot_text(self, doc: Any) -> list[str]:
        snapshot: list[str] = []
        for page in doc:
            try:
                snapshot.append(page.get_text())
            except Exception:
                snapshot.append("")
        return snapshot

    def _save(self, doc: Any, out_path: str) -> None:
        """先子集化字体再原子落盘。

        字体子集化把内置 CJK 字体从 3.5MB 压到几十 KB，是扫描件能保持 65KB 量级的关键。
        ``subset_fonts()`` 在个别字体上会失败（MuPDF 只打 warning、返回 None），
        所以保留一份未子集化的副本：子集化版若丢字，回退到完整体。
        """
        target = pathlib.Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        suffix = ".pdf"

        # 基线文本取自「已擦除、已写代号」的内存文档；子集化只能拿它作比较，
        # 不能拿 apply 前的快照——擦除本身就会让字符数减少。
        baseline_text = self._snapshot_text(doc)

        fd_full, full_path = tempfile.mkstemp(prefix="mask-full-", suffix=suffix, dir=str(target.parent))
        os.close(fd_full)
        fd_sub, sub_path = tempfile.mkstemp(prefix="mask-sub-", suffix=suffix, dir=str(target.parent))
        os.close(fd_sub)

        try:
            doc.save(full_path, garbage=4, deflate=True)

            try:
                doc.subset_fonts(verbose=False)
            except Exception as exc:
                self._last_findings.append(
                    Finding("warn", "pdf.subset_failed", f"字体子集化失败，已保留完整字体：{exc}", "font")
                )

            doc.save(sub_path, garbage=4, deflate=True)

            chosen = sub_path
            if not _text_intact(sub_path, baseline_text):
                chosen = full_path
                self._last_findings.append(
                    Finding("warn", "pdf.subset_text_lost", "字体子集化后文本层不完整，已回退到完整字体版本", "font")
                )
            os.replace(chosen, target)
        finally:
            for leftover in (full_path, sub_path):
                if os.path.exists(leftover):
                    try:
                        os.unlink(leftover)
                    except OSError:
                        pass

    # ------------------------------------------------------------------ 自检

    def _verify_output(self, out_path: str, edits: Sequence[Edit]) -> None:
        """重新打开输出文件，检查原文是否真的消失。

        这是「真实擦除」的最后一道闸，但**必须按位置判断**：
        同一页常出现多个相同原文（化名「明某」在一份判决里出现 5 次、「杨林」既是
        地名又被人名规则命中）。如果只做「页文本里还有没有这个词」，那么检测器
        漏检了 A 处、我们正确擦掉了 B 处，也会被报成「擦除未生效」——归因错了，
        还会让真正的检测漏检淹没在噪声里。

        所以逐条编辑在输出页上 ``search_for`` 原文：
        * 命中位置与本次擦除矩形相交 → 擦除确实没生效（critical）；
        * 只有远处命中 → 同页别处还有该文本，属检测漏检（warn，交人工复核）；
        * 完全命中不到 → 擦除成功。
        """
        try:
            doc = pymupdf.open(out_path)
        except Exception as exc:
            self._last_findings.append(
                Finding("critical", "pdf.output_unreadable", f"输出文件无法重新打开：{exc}", out_path)
            )
            return

        try:
            page_texts: dict[int, str] = {}
            for index in range(doc.page_count):
                try:
                    page_texts[index] = doc[index].get_text()
                except Exception:
                    page_texts[index] = ""

            seen: set[tuple[int, str, bool]] = set()
            for edit in edits:
                locator = getattr(edit.block, "locator", None)
                if not isinstance(locator, PdfLocator):
                    continue
                original = edit.entity.text
                if not original:
                    continue
                page_no = locator.page_no
                if page_no >= doc.page_count:
                    continue
                if original not in page_texts.get(page_no, ""):
                    continue

                expected = self._expected_rect(edit)
                hits: list[Any] = []
                try:
                    hits = doc[page_no].search_for(original)
                except Exception:
                    hits = []

                same_place = False
                if expected is not None:
                    same_place = any(_rects_near(hit, expected) for hit in hits)
                elif hits:
                    # 没有字符坐标可比对（理论上极少）：保守按命中处的数量判断
                    same_place = True

                key = (page_no, original, same_place)
                if key in seen:
                    continue
                seen.add(key)
                if same_place:
                    self._last_findings.append(
                        Finding(
                            "critical",
                            "pdf.residual_text",
                            f"第 {page_no + 1} 页原位置仍能读出原文（{edit.entity.type.value}），"
                            "擦除未生效或定位偏差",
                            f"page={page_no + 1}",
                        )
                    )
                else:
                    self._last_findings.append(
                        Finding(
                            "warn",
                            "pdf.residual_elsewhere",
                            f"第 {page_no + 1} 页其他位置仍出现同一文本（{edit.entity.type.value}），"
                            "不在本次擦除范围内，属检测漏检，请人工复核",
                            f"page={page_no + 1}",
                        )
                    )
        finally:
            doc.close()

    def _expected_rect(self, edit: Edit) -> pymupdf.Rect | None:
        """本次编辑预期的擦除矩形（用原始字符坐标，不依赖输出页的字符串搜索）。"""
        locator = getattr(edit.block, "locator", None)
        if not isinstance(locator, PdfLocator):
            return None
        start, end = self._effective_span(edit)
        boxes = locator.char_boxes
        if not (0 <= start < end <= len(boxes)) or len(boxes) != len(edit.block.text):
            return None
        return pymupdf.Rect(_union(boxes[start:end]))

    def _verify_ocr_page(
        self,
        page: Any,
        edits: Sequence[Edit],
        redactions: Sequence[tuple[Any, str, str]],
    ) -> None:
        """扫描页擦除后的二次 OCR 复核。

        图像像素是否擦干净，get_text 看不见。只能把擦完的页面再渲染一次送 OCR：
        若在擦除框内仍识别出原文，说明像素没擦净或定位偏了，报 warn 让人复核。
        """
        if not self.ocr_verify:
            return
        page_no = int(edits[0].block.locator.page_no) if edits else 0
        try:
            pix = page.get_pixmap(dpi=RENDER_DPI, alpha=False)
            try:
                png = pix.tobytes("png")
            finally:
                del pix
            lines = recognize(png)
        except Exception as exc:
            self._last_findings.append(
                Finding("warn", "pdf.ocr_verify_failed", f"第 {page_no + 1} 页擦除复核失败：{exc}", f"page={page_no + 1}")
            )
            return

        for rect, original, replacement in redactions:
            if not original:
                continue
            target = pymupdf.Rect(rect)
            target.x0 -= 3.0
            target.x1 += 3.0
            # 整词比对抓不住「18 位号码只残留 1 位」这种局部失败，所以退一档：
            # 原文任意 4 个连续字符出现在相交行里就要求人工复核。
            # probes 必须剔除代号沿用的后缀（「律师事务所」「公司」），否则代号本身
            # 就会命中 probes，造成 100% 误报。
            probes = _residual_probes(original, replacement or "")
            for line in lines:
                haystack = line.text
                if replacement:
                    haystack = haystack.replace(replacement, "")
                if not any(probe in haystack for probe in probes):
                    continue
                x0, y0, x1, y1 = (v * _PT_PER_PX for v in line.box)
                if pymupdf.Rect(x0, y0, x1, y1).intersects(target):
                    self._last_findings.append(
                        Finding(
                            "warn",
                            "pdf.scan_residual",
                            f"第 {page_no + 1} 页擦除区域 OCR 复核仍识别到疑似原文，建议人工核对",
                            f"page={page_no + 1},bbox=({target.x0:.1f},{target.y0:.1f},{target.x1:.1f},{target.y1:.1f})",
                        )
                    )
                    break

    # ------------------------------------------------------------------ 审计

    def _audit_metadata(self, doc: Any) -> list[Finding]:
        findings: list[Finding] = []
        try:
            metadata = dict(doc.metadata or {})
        except Exception:
            return findings
        levels = {
            "author": "warn",
            "title": "warn",
            "subject": "warn",
            "keywords": "warn",
            "creator": "info",
            "producer": "info",
        }
        for key, level in levels.items():
            value = (metadata.get(key) or "").strip()
            if value:
                findings.append(
                    Finding(level, f"pdf.metadata_{key}", f"元数据 {key}：{_clip(value)}", f"metadata.{key}")
                )
        return findings

    def _audit_document(self, doc: Any) -> list[Finding]:
        findings: list[Finding] = []

        try:
            toc = doc.get_toc() or []
        except Exception:
            toc = []
        if toc:
            sample = "; ".join(f"{item[1]}→p{item[2]}" for item in toc[:3])
            masked = any(
                ("【" in str(item[1] or "") or "】" in str(item[1] or "")) for item in toc
            )
            if masked:
                # 标题里已经出现带框代号：说明书签已按正文实体同步改写，不再重复告警
                findings.append(
                    Finding("info", "pdf.toc", f"书签/大纲 {len(toc)} 条，标题已改写为代号：{_clip(sample)}", "toc")
                )
            else:
                findings.append(
                    Finding("warn", "pdf.toc", f"书签/大纲 {len(toc)} 条，可能含当事人名称：{_clip(sample)}", "toc")
                )

        try:
            names = doc.embfile_names() or []
        except Exception:
            names = []
        if names:
            findings.append(
                Finding(
                    "critical",
                    "pdf.embedded_files",
                    f"内嵌文件 {len(names)} 个（{_clip(', '.join(names[:5]))}），内嵌内容不会被本处理器脱敏，需单独处理或删除",
                    "embfiles",
                )
            )
        return findings

    def _audit_page(self, page: Any, page_no: int) -> list[Finding]:
        findings: list[Finding] = []
        location = f"page={page_no + 1}"

        rotation = int(getattr(page, "rotation", 0) or 0)
        if rotation % 360:
            findings.append(
                Finding(
                    "warn",
                    "pdf.rotation",
                    f"第 {page_no + 1} 页旋转 {rotation}°，redaction 定位可能偏差，建议先另存为无旋转版本",
                    location,
                )
            )

        try:
            annots = list(page.annots() or [])
        except Exception:
            annots = []
        for annot in annots:
            try:
                info = annot.info or {}
                content = (info.get("content") or "").strip()
                kind = annot.type[1] if isinstance(annot.type, tuple) else str(annot.type)
            except Exception:
                content, kind = "", "unknown"
            findings.append(
                Finding(
                    "warn",
                    "pdf.annot",
                    f"第 {page_no + 1} 页批注（{kind}）：{_clip(content) if content else '无文本内容'}",
                    location,
                )
            )

        try:
            widgets = list(page.widgets() or [])
        except Exception:
            widgets = []
        if widgets:
            findings.append(
                Finding(
                    "warn",
                    "pdf.form_field",
                    f"第 {page_no + 1} 页表单域 {len(widgets)} 个，域值可能含个人信息，脱敏后请核对",
                    location,
                )
            )

        try:
            text = page.get_text().strip()
        except Exception:
            text = ""
        if not text:
            findings.append(
                Finding("info", "pdf.no_text_layer", f"第 {page_no + 1} 页无文本层，将按扫描件 OCR 处理", location)
            )
        return findings

    @staticmethod
    def _dedupe(findings: Iterable[Finding]) -> list[Finding]:
        out: list[Finding] = []
        seen: set[tuple[str, str, str]] = set()
        for finding in findings:
            key = (finding.code, finding.message, finding.location)
            if key in seen:
                continue
            seen.add(key)
            out.append(finding)
        return out


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _is_ascii_alnum(ch: str) -> bool:
    """ASCII 字母或数字。合并两行时，任一侧是它就插一个空格，避免英文粘连。"""
    return bool(ch) and ch.isascii() and ch.isalnum()


def _can_merge_lines(current: TextBlock, last_bbox: Sequence[float], block: TextBlock) -> bool:
    """判断下一行能否并入当前检测块。

    条件（来自 det-layer 的实测判据）：
    1. 水平投影有交集（同一栏、不跨栏）；
    2. 垂直间隙 < 1.6 × 行高（同一段落的行距）；
    3. 上一行不以句末标点收尾（否则是段落边界）；
    4. 字号相同、角色相同、文本来源相同。
    """
    la = getattr(current, "locator", None)
    lb = getattr(block, "locator", None)
    if not isinstance(la, PdfLocator) or not isinstance(lb, PdfLocator):
        return False
    if current.role != block.role or current.role != "body":
        return False
    if not current.text.strip() or not block.text.strip():
        return False
    if current.text.rstrip()[-1:] in "。！？；：":
        return False
    if la.source != lb.source:
        return False
    if abs(la.font_size - lb.font_size) > 0.6:
        return False

    last_height = float(last_bbox[3]) - float(last_bbox[1])
    height = float(lb.bbox[3]) - float(lb.bbox[1])
    gap = float(lb.bbox[1]) - float(last_bbox[3])
    if gap < -1.0 or gap >= 1.6 * max(last_height, height):
        return False
    if min(float(last_bbox[2]), float(lb.bbox[2])) - max(float(last_bbox[0]), float(lb.bbox[0])) <= 0:
        return False
    return True


def _merge_two_blocks(current: TextBlock, block: TextBlock) -> TextBlock:
    """把相邻行拼成一个检测块，保持 text 与 char_boxes 逐字符对齐。"""
    la: PdfLocator = current.locator
    lb: PdfLocator = block.locator

    separator = ""
    if current.text and block.text and (
        _is_ascii_alnum(current.text[-1]) or _is_ascii_alnum(block.text[0])
    ):
        separator = " "

    separator_box: tuple[float, float, float, float] | None = None
    if separator and la.char_boxes:
        x = float(la.char_boxes[-1][2])
        separator_box = (x, float(la.bbox[1]), x, float(la.bbox[3]))

    text = current.text + separator + block.text
    boxes = la.char_boxes + ((separator_box,) if separator_box else ()) + lb.char_boxes

    if la.is_merged and la.line_breaks:
        breaks = la.line_breaks + (len(text),)
        baselines = la.line_baselines + (lb.baseline,)
    else:
        breaks = (len(current.text) + len(separator), len(text))
        baselines = (la.baseline, lb.baseline)

    locator = PdfLocator(
        page_no=la.page_no,
        bbox=la.bbox,
        char_boxes=boxes,
        baseline=la.baseline,
        font_size=la.font_size,
        source=la.source,
        ocr_score=la.ocr_score,
        merged_lines=len(baselines),
        line_breaks=breaks,
        line_baselines=baselines,
    )
    return TextBlock(text=text, locator=locator, role=current.role)


def _union(boxes: Iterable[Sequence[float]]) -> tuple[float, float, float, float]:
    """字符框并集。空输入返回零矩形，由调用方判断宽度是否为 0。"""
    x0 = y0 = float("inf")
    x1 = y1 = float("-inf")
    found = False
    for box in boxes:
        found = True
        x0 = min(x0, float(box[0]))
        y0 = min(y0, float(box[1]))
        x1 = max(x1, float(box[2]))
        y1 = max(y1, float(box[3]))
    if not found:
        return (0.0, 0.0, 0.0, 0.0)
    return (x0, y0, x1, y1)


def _text_intact(path: str, expected: Sequence[str]) -> bool:
    """校验子集化后的文本层没有丢字（比较每页非空白字符数）。"""
    try:
        doc = pymupdf.open(path)
    except Exception:
        return False
    try:
        if doc.page_count != len(expected):
            return False
        for index, want in enumerate(expected):
            try:
                got = doc[index].get_text()
            except Exception:
                return False
            if _visible_len(got) < _visible_len(want) * 0.9:
                return False
        return True
    finally:
        doc.close()


#: 判定「这一列有墨迹」的灰度阈值。扫描件白底黑字，160 足够宽松；
#: 若背景是灰底扫描噪点导致投影全为正，吸附会退回估算坐标，不会出错。
_INK_THRESHOLD = 160


def _snap_left(proj: Any, estimate: int) -> int:
    """从估算点向左吸附到最近的字符间隙，返回墨迹起点列。

    估算点落在字符内部时，向左找到的空白段就是该字符与前一个字符的间隙，
    返回其右端即该字符的真实起点——这正是「实体首字符被漏擦」的修复点。
    """
    size = len(proj)
    if size == 0:
        return estimate
    index = min(size - 1, max(0, int(estimate)))
    if proj[index] > 0:
        cursor = index
        while cursor >= 0 and proj[cursor] > 0:
            cursor -= 1
        if cursor < 0:
            return 0 if index == size - 1 else estimate
        index = cursor
    # index 现在是空白列，向右扩展到空白段末尾
    while index + 1 < size and proj[index + 1] == 0:
        index += 1
    return index + 1


def _snap_right(proj: Any, estimate: int) -> int:
    """从估算点向右吸附到最近的字符间隙，返回墨迹结束列（开区间）。"""
    size = len(proj)
    if size == 0:
        return estimate
    index = min(size - 1, max(0, int(estimate)))
    if proj[index] > 0:
        cursor = index
        while cursor < size and proj[cursor] > 0:
            cursor += 1
        if cursor >= size:
            return size
        index = cursor
    # index 现在是空白列，向左扩展到空白段起点
    while index - 1 >= 0 and proj[index - 1] == 0:
        index -= 1
    return index


def _refine_ocr_rect(
    gray: Any, rect: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """用像素垂直投影把扫描件的估算矩形吸附到真实字符边界。

    OCR 只给行框，字符坐标靠「中文 1 字宽、数字半字宽」估算，混排行（如
    「统一社会信用代码：91330100MA2K7X8Y9R」）的起点误差可达两个字宽。
    这里在估算位置左右各两倍行高的窗口内做垂直投影，把左右边界吸附到最近的
    字符间隙：左边界取间隙右侧（墨迹起点），右边界取间隙左侧（墨迹终点）。
    """
    import numpy as np

    if gray is None:
        return rect
    x0, y0, x1, y1 = rect
    unit = max(4.0, y1 - y0)
    scale = RENDER_DPI / 72.0

    left_px = int((x0 - 2.0 * unit) * scale)
    right_px = int((x1 + 2.0 * unit) * scale)
    top_px = int((y0 - 2.0) * scale)
    bottom_px = int((y1 + 2.0) * scale)
    height, width = gray.shape[0], gray.shape[1]
    left_px = max(0, left_px)
    top_px = max(0, top_px)
    right_px = min(width, right_px)
    bottom_px = min(height, bottom_px)
    if right_px - left_px < 4 or bottom_px - top_px < 2:
        return rect

    band = gray[top_px:bottom_px, left_px:right_px]
    proj = np.count_nonzero(band < _INK_THRESHOLD, axis=0)
    estimate_left = int(x0 * scale) - left_px
    estimate_right = int(x1 * scale) - left_px
    if estimate_left >= len(proj) or estimate_right >= len(proj):
        return rect

    new_left = _snap_left(proj, estimate_left)
    new_right = _snap_right(proj, estimate_right)
    if new_right - new_left < 2:
        return rect

    refined_left = (left_px + new_left) / scale
    refined_right = (left_px + new_right) / scale
    # 吸附只允许在小范围内修正，防止把整行或相邻实体吞进来
    if abs(refined_left - x0) > 3.0 * unit or abs(refined_right - x1) > 3.0 * unit:
        return rect
    return (refined_left, y0, refined_right, y1)


def _rects_near(a: Any, b: Any, *, slack: float = 2.0) -> bool:
    """两个矩形是否「同一处」：相交，或中心距离小于较大高度。

    redaction 后 MuPDF 可能把文本对象重写成与原 bbox 略有出入的矩形，
    所以不能只判严格相等；用行高量级的容差足以区分「同一处」与「同页别处」。
    """
    try:
        ra = pymupdf.Rect(a)
        rb = pymupdf.Rect(b)
    except Exception:
        return False
    if ra.intersects(rb):
        return True
    height = max(1.0, min(ra.height, rb.height))
    dx = abs((ra.x0 + ra.x1) / 2.0 - (rb.x0 + rb.x1) / 2.0)
    dy = abs((ra.y0 + ra.y1) / 2.0 - (rb.y0 + rb.y1) / 2.0)
    return dx <= slack + height * 2.0 and dy <= height * 1.2


def _case_no_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    """找出包含 [start, end) 的完整案号区间；只扩不缩。"""
    try:
        from ..allocator import CASE_NO_RE
    except Exception:
        return None
    best: tuple[int, int] | None = None
    for match in CASE_NO_RE.finditer(text or ""):
        if match.start() <= start and match.end() >= end:
            span = (match.start(), match.end())
            if span != (start, end):
                if best is None or (span[1] - span[0]) > (best[1] - best[0]):
                    best = span
    return best


def _residual_probes(original: str, replacement: str) -> set[str]:
    """构造残留检测探针：只取「原文中代号没有沿用」的那一段。

    allocator 会保留机构后缀（浙江某某律师事务所 → 【梅】律师事务所），
    直接拿原文子串去比对必然命中代号本身。这里先削掉最长公共后缀，再切 4 字探针。
    """
    common = 0
    while (
        common < min(len(original), len(replacement))
        and original[len(original) - 1 - common] == replacement[len(replacement) - 1 - common]
    ):
        common += 1
    core = original[: len(original) - common]
    if len(core) < 3:
        core = original
    if len(core) <= 4:
        return {core}
    return {core[i : i + 4] for i in range(len(core) - 3)}


def locator_is_ocr(edit: Edit) -> bool:
    locator = getattr(edit.block, "locator", None)
    return isinstance(locator, PdfLocator) and locator.is_ocr


def _is_cjk(ch: str) -> bool:
    """是否汉字（含扩展 A 区）。用于判断空格是否夹在汉字中间。"""
    if not ch:
        return False
    code = ord(ch)
    return 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF


def _drop_cjk_inner_spaces(
    chars: Sequence[tuple[str, tuple[float, float, float, float]]],
) -> tuple[str, list[tuple[float, float, float, float]]]:
    """折叠「汉字—空格—汉字」中的空格段，返回 (text, char_boxes)。

    只折叠被汉字夹住的**全角空格**（U+3000）：它几乎只用于排版对齐
    （「人民陪审员　　张　丽」），折叠后姓名才能被连续匹配。半角空格是词间
    分隔信号（「肖芄 赖粤旭 张丽」），一旦折叠三个姓名会粘成一个无法识别的
    长串——实测会让法官署名整行漏检，所以必须保留。text 与 boxes 始终对齐。
    """
    out_text: list[str] = []
    out_boxes: list[tuple[float, float, float, float]] = []
    index = 0
    total = len(chars)
    spaces = ("\u3000",)
    while index < total:
        char, box = chars[index]
        if char in spaces:
            end = index
            while end < total and chars[end][0] in spaces:
                end += 1
            previous = chars[index - 1][0] if index > 0 else ""
            following = chars[end][0] if end < total else ""
            if _is_cjk(previous) and _is_cjk(following):
                index = end  # 整段丢弃
                continue
            for offset in range(index, end):
                out_text.append(chars[offset][0])
                out_boxes.append(chars[offset][1])
            index = end
            continue
        out_text.append(char)
        out_boxes.append(box)
        index += 1
    return "".join(out_text), out_boxes


def _is_punct(ch: str) -> bool:
    """是否属于「代号可以覆盖掉」的字符：空白与各类标点。"""
    if not ch or ch.isspace():
        return True
    if ch in "，。、；：？！（）【】《》“”‘’·—…,.\"'?!()[]{}:;<>/@#$%^&*_+=|~`-":
        return True
    return not ch.isalnum()


def _visible_len(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def _clip(text: str, limit: int = 120) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def edit_page(edits: Sequence[Edit]) -> int:
    """辅助：取这批编辑的页号（用于告警文案），失败返回 0。"""
    for edit in edits:
        locator = getattr(edit.block, "locator", None)
        if isinstance(locator, PdfLocator):
            return locator.page_no + 1
    return 0


HANDLER = PdfHandler()

__all__ = ["HANDLER", "PdfHandler", "PdfLocator"]

"""回填：把脱敏件里的代号换回真名。

两个方向
--------
- ``code_to_real``（默认）：【熊猫】 -> 张三，用于分析完成后还原。
- ``real_to_code``：张三 -> 【熊猫】，用于二次校验「这份文件是不是真的脱干净了」。

容错
----
真实的脱敏件经过 Word / AI 工作台 / 复制粘贴之后，代号常被破坏：
``【熊猫】`` 可能变成 ``【 熊 猫 】``、``[熊猫]``、全角空格分隔等。
``restore_text`` 用「逐字之间允许任意空白 + 多种括号」的正则来匹配，
不要求原文一字不差。若同一代号对应多个原文（掩码撞号），则跳过并告警，
绝不猜测——猜错会把 A 的信息安到 B 头上。

格式支持
--------
- 纯文本 / Markdown / CSV / JSON：直接读写，保留原编码（UTF-8 / GBK 自动识别）。
- docx：逐段落回填，包括表格、页眉页脚；代号被拆到多个 run 时才合并 runs
  （合并会丢失该段落的局部格式，属于必要代价）。
- xlsx：逐单元格回填，公式与数字不碰。
- pdf：尽力而为（PyMuPDF 擦除后写入），版式可能变化；失败时提示改用原件改名。
"""

from __future__ import annotations

import pathlib
import re
from typing import Any, Iterable

from .vault import Vault

#: 常见中英文括号对，回填时互认
_BRACKETS: tuple[tuple[str, str], ...] = (
    ("【", "】"),
    ("〖", "〗"),
    ("〔", "〕"),
    ("［", "］"),
    ("[", "]"),
    ("｛", "｝"),
    ("{", "}"),
)
_OPEN_CLASS = "[" + "".join(re.escape(o) for o, _ in _BRACKETS) + "]"
_CLOSE_CLASS = "[" + "".join(re.escape(c) for _, c in _BRACKETS) + "]"
#: 代号内部允许插入的空白（半角/全角）
_GAP = r"[\s\u3000]*"
#: 类型线索：撞号代号（如证号与电话同为 XXX1）按邻近标签消歧。
#: 顺序无意义，按「离代号最近的线索」判定。
_TYPE_CUES: dict[str, tuple[str, ...]] = {
    "id_card": ("身份证", "身份号码", "证件号", "公民身份"),
    "phone": ("联系电话", "电话", "手机", "联系方式", "固话", "座机"),
    "bank_card": ("银行卡", "收款账户", "账户", "账号", "卡号"),
    "uscc": ("统一社会信用代码", "信用代码", "工商注册号"),
    "email": ("电子邮箱", "邮箱", "电子邮件", "邮件地址"),
    "plate": ("车牌号", "车牌"),
    "case_no": ("案号",),
}

#: 纯文本类后缀（直接按文本处理）
TEXT_SUFFIXES: frozenset[str] = frozenset(
    {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".log", ".yaml", ".yml", ".xml", ".html", ".htm"}
)


class RestoreError(RuntimeError):
    """回填失败的可预期错误。"""


class RestoreUnsupported(RestoreError):
    """格式暂不支持回填。"""


# --------------------------------------------------------------------- 正则工具


def _char_flex(text: str) -> str:
    """逐字符插入「可选空白」，让「熊猫」也能匹配「熊 猫」。"""
    return _GAP.join(re.escape(ch) for ch in text)


def _frame_parts(placeholder: str) -> tuple[str, str, str] | None:
    """在代号中定位带框片段，返回 (前缀, 内文, 后缀)。

    地名/机构代号可能带后缀，如 ``【蓬莱】路``、``【白桦】律师事务所``，
    因此不能只看首尾字符，要在任意位置找“开括号…闭括号”片段。
    """
    best: tuple[int, int, str] | None = None
    for opening, closing in _BRACKETS:
        start = placeholder.find(opening)
        if start < 0:
            continue
        end = placeholder.find(closing, start + len(opening))
        if end < 0:
            continue
        span = (start, end + len(closing), placeholder[start + len(opening): end])
        if best is None or span[0] < best[0]:
            best = span
    if best is None:
        return None
    start, end, inner = best
    if not inner:
        return None
    return placeholder[:start], inner, placeholder[end:]


def _code_regex(placeholder: str) -> re.Pattern[str]:
    """为代号生成容错正则：括号可变、字间可插空白、ASCII 不区分大小写。"""
    parts = _frame_parts(placeholder)
    if parts is not None:
        prefix, inner, suffix = parts
        # 带框代号由括号自身定界，不再加 ASCII 边界：
        # 否则「【蓬莱】路100号」会因后面紧跟数字而匹配失败。
        body = (
            f"{re.escape(prefix)}{_OPEN_CLASS}{_GAP}{_char_flex(inner)}{_GAP}{_CLOSE_CLASS}"
            f"{re.escape(suffix)}"
        )
    else:
        # 无框代号（如掩码 XXX / XXX1）：加边界，避免吃掉相邻字母数字
        body = rf"(?<![A-Za-z0-9]){_char_flex(placeholder)}(?![A-Za-z0-9])"
    flags = re.IGNORECASE if re.search(r"[A-Za-z]", placeholder) else 0
    return re.compile(body, flags)


def _cue_from_context(left: str, right: str) -> str | None:
    """从左/右上下文找最近的类型线索；左侧优先，都没有返回 None。"""
    for segment in (left, right):
        best_type: str | None = None
        best_pos = -1
        for etype, cues in _TYPE_CUES.items():
            for cue in cues:
                pos = segment.rfind(cue)
                if pos < 0:
                    continue
                # 线索结束位置越靠近代号越优先
                score = pos + len(cue)
                if score > best_pos:
                    best_pos = score
                    best_type = etype
        if best_type is not None:
            return best_type
    return None


def _nearest_cue(text: str, start: int, end: int, window: int = 12) -> str | None:
    """找离命中最近的类型线索（在给定文本内取窗口）。"""
    return _cue_from_context(text[max(0, start - window):start], text[end:end + window])


def _literal_regex(original: str) -> re.Pattern[str]:
    """为原文生成字面正则（ASCII 场景忽略大小写），用于 real_to_code。"""
    flags = re.IGNORECASE if re.search(r"[A-Za-z]", original) else 0
    return re.compile(re.escape(original), flags)


# --------------------------------------------------------------------- 文本回填


def restore_text(
    text: str,
    vault: Vault,
    direction: str = "code_to_real",
    stats: dict[str, int] | None = None,
    warnings: list[str] | None = None,
    context_hint: str = "",
) -> str:
    """把文本里的代号与真名互转。

    参数
    ----
    text: 待处理文本。
    vault: 映射表。
    direction: ``code_to_real``（默认）或 ``real_to_code``。
    stats: 可选的可变计数器，会累加 ``replaced`` / ``skipped_ambiguous``。
    warnings: 可选的告警收集列表（撞号代号会写在这里）。

    返回替换后的文本；找不到任何代号时原样返回。
    """
    if direction not in ("code_to_real", "real_to_code"):
        raise ValueError(f"未知方向：{direction}（只支持 code_to_real / real_to_code）")
    if not text or not vault.entries:
        return text

    counter = stats if stats is not None else {}
    counter.setdefault("replaced", 0)
    counter.setdefault("skipped_ambiguous", 0)
    warn_list = warnings if warnings is not None else []

    ambiguous = vault.ambiguous_placeholders()
    for placeholder, items in ambiguous.items():
        hits = len(_code_regex(placeholder).findall(text))
        if hits:
            originals = "、".join(sorted({i.original for i in items}))
            message = (
                f"代号 {placeholder} 同时对应多个原文（{originals}）："
                f"文本中出现 {hits} 处，将按邻近类型线索尽量回填，无线索处保持原样。"
            )
            if message not in warn_list:
                warn_list.append(message)

    # 长代号优先：避免 XXX 抢在 XXX1 前面把文本改坏
    usable = [e for e in vault.entries if e.placeholder not in ambiguous]
    if direction == "code_to_real":
        pairs: list[tuple[str, str]] = sorted(
            ((e.placeholder, e.original) for e in usable), key=lambda p: len(p[0]), reverse=True
        )
        pattern_of = _code_regex
    else:
        pairs = sorted(((e.original, e.placeholder) for e in usable), key=lambda p: len(p[0]), reverse=True)
        pattern_of = _literal_regex

    result = text
    for needle, replacement in pairs:
        if not needle:
            continue
        pattern = pattern_of(needle)

        def _sub(match: re.Match[str], _rep: str = replacement) -> str:
            counter["replaced"] = counter.get("replaced", 0) + 1
            return _rep

        result = pattern.sub(_sub, result)

    if direction == "code_to_real":
        result = _restore_ambiguous(result, ambiguous, counter, warn_list, context_hint)
    return result


def _restore_ambiguous(
    text: str,
    ambiguous: dict[str, list],
    counter: dict[str, int],
    warn_list: list[str],
    context_hint: str = "",
) -> str:
    """撞号代号按上下文线索逐处回填；判断不了就保持原样。

    例：``身份证号XXX1`` 与 ``联系电话XXX1`` 共用 XXX1 时，
    前者回填身份证、后者回填手机号；孤立出现的 ``XXX1`` 保持原样并计入跳过。
    """
    counter.setdefault("resolved_ambiguous", 0)
    for placeholder, items in sorted(ambiguous.items(), key=lambda kv: -len(kv[0])):
        by_type: dict[str, list] = {}
        for item in items:
            by_type.setdefault(item.type, []).append(item)
        pattern = _code_regex(placeholder)
        # 注意：不能用 re.sub + match.string 取上下文——回调里 match.string
        # 是替换前的原串，位置会随前面替换而错位。这里手工扫描当前文本。
        pieces: list[str] = []
        cursor = 0
        for match in pattern.finditer(text):
            pieces.append(text[cursor:match.start()])
            # 左上下文用「已生成的输出」：前面已回填的代号不能再作为线索；
            # 右上下文用原文：右侧通常还没处理，线索仍然有效。
            left = context_hint + "".join(pieces[-3:])[-12:]
            right = text[match.end():match.end() + 12]
            cue = _cue_from_context(left, right)
            candidates = by_type.get(cue, []) if cue else []
            if len(candidates) == 1:
                counter["replaced"] = counter.get("replaced", 0) + 1
                counter["resolved_ambiguous"] = counter.get("resolved_ambiguous", 0) + 1
                pieces.append(candidates[0].original)
            else:
                counter["skipped_ambiguous"] = counter.get("skipped_ambiguous", 0) + 1
                pieces.append(match.group(0))
            cursor = match.end()
        pieces.append(text[cursor:])
        text = "".join(pieces)
    unresolved = counter.get("skipped_ambiguous", 0)
    if unresolved and warn_list is not None:
        warn_list.append(
            f"有 {unresolved} 处撞号代号无法根据上下文判断（同代号对应多个原文），已保持原样，请人工核对。"
        )
    return text


# --------------------------------------------------------------------- 文件回填


def _decode_bytes(data: bytes) -> tuple[str, str]:
    """自动识别编码，返回 (文本, 编码名)。优先 UTF-8（含 BOM），再试 GB18030。"""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8"


def _restore_text_file(src: pathlib.Path, out: pathlib.Path, vault: Vault) -> tuple[pathlib.Path, dict[str, int]]:
    text, encoding = _decode_bytes(src.read_bytes())
    stats: dict[str, int] = {}
    restored = restore_text(text, vault, stats=stats)
    out.parent.mkdir(parents=True, exist_ok=True)
    # UTF-8 统一回写（避免 GBK 无法表示生僻字）；原文编码只在读取时使用
    out.write_text(restored, encoding="utf-8")
    return out, stats


def _iter_docx_paragraphs(document: Any) -> Iterable[Any]:
    """遍历 docx 全部段落：正文、表格（含嵌套）、页眉页脚。"""
    for paragraph in document.paragraphs:
        yield paragraph
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    yield paragraph
                for nested in cell.tables:
                    for row2 in nested.rows:
                        for cell2 in row2.cells:
                            for paragraph in cell2.paragraphs:
                                yield paragraph
    for section in document.sections:
        for container in (section.header, section.footer, section.first_page_header, section.first_page_footer):
            if container is None:
                continue
            for paragraph in container.paragraphs:
                yield paragraph
            for table in container.tables:
                for row in table.rows:
                    for cell in row.cells:
                        for paragraph in cell.paragraphs:
                            yield paragraph


def _splice_runs(runs: list, start: int, end: int, replacement: str) -> bool:
    """把段落全文中的 [start, end) 替换成 replacement，并写回对应 run。

    只改动命中区间覆盖的 run：前一个 run 保留前缀、后一个 run 保留后缀，
    中间的 run 清空文本。这样不会像「整段合并到首个 run」那样丢失
    段内换行（``<w:br/>``）、制表符与局部格式。
    """
    texts = [run.text for run in runs]
    total = sum(len(text) for text in texts)
    if start < 0 or end > total or start >= end:
        return False

    start_run = end_run = -1
    start_offset = end_offset = 0
    pos = 0
    for index, text in enumerate(texts):
        if start_run < 0 and pos <= start < pos + len(text):
            start_run, start_offset = index, start - pos
        if end_run < 0 and pos < end <= pos + len(text):
            end_run, end_offset = index, end - pos
        pos += len(text)
    if start_run < 0 or end_run < 0:
        return False

    if start_run == end_run:
        runs[start_run].text = texts[start_run][:start_offset] + replacement + texts[start_run][end_offset:]
    else:
        runs[start_run].text = texts[start_run][:start_offset] + replacement
        for index in range(start_run + 1, end_run):
            runs[index].text = ""
        runs[end_run].text = texts[end_run][end_offset:]
    return True


def _collect_paragraph_ops(
    full_text: str,
    vault: Vault,
    stats: dict[str, int],
) -> list[tuple[int, int, str]]:
    """在段落全文上收集替换区间 [(start, end, replacement)]。"""
    ambiguous = vault.ambiguous_placeholders()
    ops: list[tuple[int, int, str]] = []
    taken: list[tuple[int, int]] = []

    def _overlaps(start: int, end: int) -> bool:
        return any(start < k_end and end > k_start for k_start, k_end in taken)

    # 长代号优先：避免 XXX 抢在 XXX1 前
    usable = [e for e in vault.entries if e.placeholder not in ambiguous]
    for entry in sorted(usable, key=lambda e: -len(e.placeholder)):
        pattern = _code_regex(entry.placeholder)
        for match in pattern.finditer(full_text):
            if _overlaps(match.start(), match.end()):
                continue
            taken.append(match.span())
            ops.append((match.start(), match.end(), entry.original))

    for placeholder, items in sorted(ambiguous.items(), key=lambda kv: -len(kv[0])):
        by_type: dict[str, list] = {}
        for item in items:
            by_type.setdefault(item.type, []).append(item)
        pattern = _code_regex(placeholder)
        for match in pattern.finditer(full_text):
            if _overlaps(match.start(), match.end()):
                continue
            cue = _cue_from_context(
                full_text[max(0, match.start() - 12):match.start()],
                full_text[match.end():match.end() + 12],
            )
            candidates = by_type.get(cue, []) if cue else []
            if len(candidates) == 1:
                stats["resolved_ambiguous"] = stats.get("resolved_ambiguous", 0) + 1
                ops.append((match.start(), match.end(), candidates[0].original))
            else:
                stats["skipped_ambiguous"] = stats.get("skipped_ambiguous", 0) + 1
    return ops


def _restore_paragraph(paragraph: Any, vault: Vault, stats: dict[str, int]) -> None:
    """回填一个段落：按 run 精确拼接，保留换行与局部格式。"""
    runs = list(paragraph.runs)
    if not runs:
        old_text = paragraph.text
        if old_text:
            restored = restore_text(old_text, vault, stats=stats)
            if restored != old_text:
                paragraph.add_run(restored)
        return

    full_text = "".join(run.text for run in runs)
    if not full_text:
        return
    ops = _collect_paragraph_ops(full_text, vault, stats)
    if not ops:
        return
    # 从右往左改，避免前面的替换影响后面区间的偏移
    for start, end, replacement in sorted(ops, key=lambda item: item[0], reverse=True):
        if _splice_runs(runs, start, end, replacement):
            stats["replaced"] = stats.get("replaced", 0) + 1


def _restore_docx(src: pathlib.Path, out: pathlib.Path, vault: Vault) -> tuple[pathlib.Path, dict[str, int]]:
    from docx import Document  # 延迟导入，未安装时错误更清晰

    document = Document(str(src))
    stats: dict[str, int] = {}
    for paragraph in _iter_docx_paragraphs(document):
        _restore_paragraph(paragraph, vault, stats)
    out.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(out))
    return out, stats


def _restore_xlsx(src: pathlib.Path, out: pathlib.Path, vault: Vault) -> tuple[pathlib.Path, dict[str, int]]:
    from openpyxl import load_workbook

    workbook = load_workbook(str(src))
    stats: dict[str, int] = {}
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                value = cell.value
                if not isinstance(value, str) or value.startswith("="):
                    continue
                # 表格里类型信息常写在表头（「身份证号」「联系电话」），
                # 单元格内只有 XXX1 时靠它消歧。
                hint = ""
                if cell.row > 1 and cell.column > 1:
                    header = sheet.cell(row=1, column=cell.column).value
                    hint = str(header) if header is not None else ""
                restored = restore_text(value, vault, stats=stats, context_hint=hint)
                if restored != value:
                    cell.value = restored
    out.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(str(out))
    return out, stats


def _restore_pdf(src: pathlib.Path, out: pathlib.Path, vault: Vault) -> tuple[pathlib.Path, dict[str, int]]:
    """PDF 回填：尽力而为。

    对每一页按代号原文查找矩形，用 redaction 擦除并写回真名。
    中文字体用 PyMuPDF 内置 ``china-s``；写入失败时退化为「仅擦除」。
    版式与换行可能与原件不同，正式回填应优先使用原件改名。
    """
    try:
        import pymupdf as fitz  # PyMuPDF 1.24+ 的新模块名
    except ImportError:  # pragma: no cover - 旧版本回退
        try:
            import fitz
        except ImportError as exc:  # pragma: no cover - 环境已装
            raise RestoreUnsupported("未安装 PyMuPDF，无法回填 PDF；请把原件改名后使用。") from exc

    ambiguous = vault.ambiguous_placeholders()
    pairs = [(e.placeholder, e.original) for e in vault.entries if e.placeholder not in ambiguous]
    stats: dict[str, int] = {}
    document = fitz.open(str(src))
    try:
        for page in document:
            for placeholder, original in pairs:
                for rect in page.search_for(placeholder):
                    fontsize = max(6.0, min(rect.height * 0.9, 24.0))
                    try:
                        page.add_redact_annot(rect, text=original, fontname="china-s", fontsize=fontsize)
                    except Exception:
                        page.add_redact_annot(rect)  # 退化为擦除，至少不泄露代号
                    stats["replaced"] = stats.get("replaced", 0) + 1
            page.apply_redactions()
        out.parent.mkdir(parents=True, exist_ok=True)
        document.save(str(out), garbage=3, deflate=True)
    finally:
        document.close()
    return out, stats


def _dispatch(src: pathlib.Path, out: pathlib.Path, vault: Vault) -> tuple[pathlib.Path, dict[str, int]]:
    suffix = src.suffix.lower()
    if suffix in TEXT_SUFFIXES or suffix == "":
        return _restore_text_file(src, out, vault)
    if suffix == ".docx":
        return _restore_docx(src, out, vault)
    if suffix in (".xlsx", ".xlsm"):
        return _restore_xlsx(src, out, vault)
    if suffix == ".pdf":
        return _restore_pdf(src, out, vault)
    raise RestoreUnsupported(
        f"暂不支持回填 {suffix or '(无后缀)'} 格式。\n"
        "已支持：txt/md/csv/json 等纯文本、docx、xlsx、pdf（尽力而为）。\n"
        f"其它格式请用原文件改名方式处理：{src.name}"
    )


def restore_file(
    src: str | pathlib.Path,
    out: str | pathlib.Path,
    vault: Vault,
    *,
    direction: str = "code_to_real",
    stats: dict[str, int] | None = None,
    warnings: list[str] | None = None,
) -> pathlib.Path:
    """把 ``src`` 回填后写到 ``out``，返回输出路径。

    绝不覆盖原件：``src`` 与 ``out`` 解析为同一路径时直接拒绝。
    回填方向固定为 ``code_to_real``（``direction`` 参数保留给二次校验场景）。
    """
    source = pathlib.Path(src).expanduser().resolve()
    target = pathlib.Path(out).expanduser().resolve()
    if not source.exists():
        raise RestoreError(f"待回填文件不存在：{source}")
    if source == target:
        raise RestoreError(
            "输出路径与原件相同，拒绝覆盖原件。\n"
            f"请换一个输出名，例如：{source.with_name(source.stem + '_已回填' + source.suffix)}"
        )
    if direction != "code_to_real":
        # real_to_code 用于校验，允许调用方直接对文本做
        raise RestoreError("restore_file 只做 code_to_real 回填；real_to_code 请用 restore_text。")

    result, count = _dispatch(source, target, vault)
    if stats is not None:
        stats.update(count)
    if warnings is not None and count.get("replaced", 0) == 0:
        if count.get("skipped_ambiguous", 0):
            warnings.append(
                f"{source.name} 中的代号全部因撞号被跳过（{count['skipped_ambiguous']} 处），"
                "回填件仍是代号；请按映射表人工核对。"
            )
        else:
            warnings.append(
                f"{source.name} 中未找到任何可回填的代号，输出与原文一致；请确认映射表是否对应该案件。"
            )
    return result


__all__ = [
    "RestoreError",
    "RestoreUnsupported",
    "TEXT_SUFFIXES",
    "restore_file",
    "restore_text",
]

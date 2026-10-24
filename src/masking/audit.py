"""残留审计：对脱敏件做二次扫描，并生成中文审计报告。

两层扫描
--------
1. ``handler.audit(path)``：格式特有的泄露点（文档属性、批注、修订、隐藏表等），
   由各格式处理器负责；本模块只负责汇总。
2. ``scan_text``：用 ``config/rules.yaml`` 的 ``residual_patterns`` 逐条正则扫描
   全部文本块，命中即说明有漏网。

报告口径
--------
报告含真名与命中片段，属机密文件：落在 vault 目录（0o600），禁止外传。
``write_report`` 固定输出三张清单（已处理 / 需人工复核 / 无法处理）
以及一条「日期与金额默认保留」的风险提示。
"""

from __future__ import annotations

import pathlib
import re
from datetime import datetime
from typing import Iterable, Sequence

from .allocator import MappingEntry
from .config import Rules
from .engine import RunResult
from .entity import EntityType, Finding, TextBlock
from .vault import Vault

#: 命中这些关键词的残留按 critical 处理（可直接定位到个人）
CRITICAL_KEYWORDS: tuple[str, ...] = ("身份证", "手机", "银行卡", "邮箱", "信用代码", "案号")
#: 命中这些关键词按 warn 处理（主体名称，仍属敏感但非直接标识）
WARN_KEYWORDS: tuple[str, ...] = ("机构", "法院", "检察院", "公司")

#: 实体类型的中文名（报告用）
TYPE_LABELS: dict[str, str] = {
    "person": "自然人姓名",
    "org": "组织/机构",
    "place": "地名",
    "address": "住址",
    "id_card": "身份证号",
    "phone": "电话",
    "bank_card": "银行卡号",
    "uscc": "统一社会信用代码",
    "plate": "车牌号",
    "email": "邮箱",
    "case_no": "案号",
    "date": "日期（保留）",
    "amount": "金额（保留）",
    "custom": "自定义词",
}

#: 可作为正文直接读取的纯文本后缀
_PLAIN_SUFFIXES: frozenset[str] = frozenset(
    {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".log", ".yaml", ".yml", ".xml", ".html", ".htm"}
)


def _level_for(rule_name: str) -> str:
    """按规则名判定严重级别。"""
    if any(key in rule_name for key in CRITICAL_KEYWORDS):
        return "critical"
    if any(key in rule_name for key in WARN_KEYWORDS):
        return "warn"
    return "warn"


def _ascii_boundary_variant(pattern: str) -> str | None:
    """为 ``\\b`` 规则生成「中文紧邻」变体。

    ``rules.yaml`` 里的规则用 ``\\b`` 界定，但中文也是 ``\\w``：
    「身份证号330102…」里汉字与数字之间没有 ``\\b``，原规则会漏扫。
    这里把首尾 ``\\b`` 换成 ASCII 边界环视（``(?<![A-Za-z0-9])``），
    两边都跑，命中结果按区间去重。
    """
    variant = pattern
    if variant.startswith(r"\b"):
        variant = r"(?<![A-Za-z0-9])" + variant[2:]
    if variant.endswith(r"\b"):
        variant = variant[:-2] + r"(?![A-Za-z0-9])"
    return variant if variant != pattern else None


def _mask_token(token: str) -> str:
    """把命中片段中间打码，报告里只留可核对的头尾。"""
    if len(token) <= 6:
        return token[0] + "*" * (len(token) - 1) if len(token) > 1 else "*"
    return f"{token[:3]}{'*' * (len(token) - 5)}{token[-2:]}"


def _context(text: str, start: int, end: int, radius: int = 12) -> str:
    """取命中前后的上下文，压缩换行，便于人工定位。"""
    left = text[max(0, start - radius): start]
    right = text[end: end + radius]
    return (left + "〔命中〕" + right).replace("\n", " ").replace("\r", " ").replace("|", "／")


def _is_own_value(token: str, ignored: Sequence[str]) -> bool:
    """命中串是否是本工具自己写进文件的产物（代号 / 自编案号等）。

    自编案号 ``(2024)甲法民初1号``、掩码 ``XXX1``、代号 ``【熊猫】``
    不可能同时是泄露；不排除它们会导致「自己扫自己」的误判。
    """
    return any(token == value or token in value for value in ignored)


def scan_text(
    text: str,
    rules: Rules,
    location: str = "",
    ignore_values: Sequence[str] = (),
) -> list[Finding]:
    """用 ``rules.residual`` 的规则扫描一段文本，返回全部命中。

    ``ignore_values`` 传本案件映射表里的代号 / 自编值，命中这些串时不计入残留。
    同一段文本里区间重叠的命中只保留第一个（规则顺序即优先级：
    身份证号在银行卡号之前，避免 18 位身份证被当成银行卡重复报）。
    """
    findings: list[Finding] = []
    if not text:
        return findings
    ignored = tuple(value for value in ignore_values if value)
    kept_spans: list[tuple[int, int]] = []

    for rule_name, patterns in rules.residual.items():
        level = _level_for(rule_name)
        if not isinstance(patterns, (list, tuple)):
            patterns = [patterns]
        for raw in patterns:
            if not isinstance(raw, str) or not raw:
                continue
            variants = [raw]
            extra = _ascii_boundary_variant(raw)
            if extra:
                variants.append(extra)
            for source in variants:
                try:
                    compiled = re.compile(source)
                except re.error as exc:
                    findings.append(
                        Finding(
                            level="warn",
                            code=f"residual_config:{rule_name}",
                            message=f"残留规则无法编译（{source!r}）：{exc}。请检查 config/rules.yaml 的 residual_patterns。",
                            location=location,
                        )
                    )
                    continue
                for match in compiled.finditer(text):
                    start, end = match.span()
                    if start == end:
                        continue
                    if any(start < k_end and end > k_start for k_start, k_end in kept_spans):
                        continue
                    kept_spans.append((start, end))
                    token = match.group(0)
                    if ignored and _is_own_value(token, ignored):
                        continue
                    shown = _mask_token(token) if level == "critical" else token
                    where = f"{location} 第{start}字" if location else f"第{start}字"
                    findings.append(
                        Finding(
                            level=level,
                            code=f"residual:{rule_name}",
                            message=(
                                f"残留{rule_name}：{shown}；上下文「{_context(text, start, end)}」"
                            ),
                            location=where,
                        )
                    )
    return findings


# --------------------------------------------------------------------- 无 handler 时的文本提取


def _fallback_blocks(path: pathlib.Path) -> list[TextBlock]:
    """handler 缺失时自行提取文本（用于对已脱敏目录做复扫）。

    提取不到内容时返回空列表；不支持的格式抛 ValueError，由调用方转成 Finding。
    """
    suffix = path.suffix.lower()
    if suffix in _PLAIN_SUFFIXES or suffix == "":
        data = path.read_bytes()
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return [TextBlock(text=data.decode(encoding), locator=None, role="body")]
            except UnicodeDecodeError:
                continue
        return [TextBlock(text=data.decode("utf-8", errors="replace"), locator=None, role="body")]

    if suffix == ".docx":
        from docx import Document

        document = Document(str(path))
        blocks: list[TextBlock] = [TextBlock(text=p.text, locator=("p", i), role="body") for i, p in enumerate(document.paragraphs)]
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    blocks.append(TextBlock(text=cell.text, locator=None, role="cell"))
        for section in document.sections:
            for role, container in (("header", section.header), ("footer", section.footer)):
                if container is not None:
                    blocks.extend(TextBlock(text=p.text, locator=None, role=role) for p in container.paragraphs)
        return blocks

    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        workbook = load_workbook(str(path), read_only=True, data_only=True)
        blocks = []
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.strip():
                        blocks.append(TextBlock(text=cell.value, locator=(sheet.title, cell.coordinate), role="cell"))
        workbook.close()
        return blocks

    if suffix == ".pdf":
        try:
            import pymupdf as fitz
        except ImportError:  # pragma: no cover - 旧版本回退
            import fitz

        blocks = []
        document = fitz.open(str(path))
        try:
            for index, page in enumerate(document):
                text = page.get_text("text") or ""
                if text.strip():
                    blocks.append(TextBlock(text=text, locator=index, role="body"))
        finally:
            document.close()
        return blocks

    raise ValueError(f"暂不支持扫描 {suffix or '(无后缀)'} 格式的正文")


def audit_file(
    path: str | pathlib.Path,
    rules: Rules,
    handler=None,
    ignore_values: Sequence[str] = (),
) -> list[Finding]:
    """审计单个文件：格式特有泄露点 + 全文本残留扫描。

    ``handler`` 可为空；为空时按后缀自行提取文本（docx/xlsx/pdf/纯文本）。
    ``ignore_values`` 传本案件自己的代号/自编值，避免「自己扫自己」。
    """
    target = pathlib.Path(path).expanduser().resolve()
    findings: list[Finding] = []
    if not target.exists():
        return [Finding("critical", "file_missing", f"文件不存在，无法审计：{target}", str(target))]

    blocks: list[TextBlock] = []
    if handler is not None:
        try:
            findings.extend(handler.audit(str(target)))
        except Exception as exc:  # 单个 handler 失败不阻断文本扫描
            findings.append(
                Finding("warn", "handler_audit_error", f"格式处理器 {getattr(handler, 'name', '?')} 审计失败：{exc}", str(target))
            )
        try:
            blocks = list(handler.extract(str(target)))
        except Exception as exc:
            findings.append(
                Finding("warn", "handler_extract_error", f"格式处理器 {getattr(handler, 'name', '?')} 读取失败：{exc}", str(target))
            )
    else:
        try:
            blocks = _fallback_blocks(target)
        except Exception as exc:
            findings.append(Finding("warn", "audit_unsupported", f"无法提取正文，需人工检查：{exc}", str(target)))

    for block in blocks:
        role = f"/{block.role}" if block.role else ""
        findings.extend(
            scan_text(block.text, rules, location=f"{target.name}{role}", ignore_values=ignore_values)
        )
    return findings


# --------------------------------------------------------------------- 报告


def _cell(value: object) -> str:
    """Markdown 表格单元格转义。"""
    return str(value).replace("|", "\\|").replace("\n", " ").strip() or "—"


def _type_table(entries: Sequence[MappingEntry]) -> list[str]:
    """按类型统计表格。"""
    stats: dict[str, list[int]] = {}
    for entry in entries:
        bucket = stats.setdefault(entry.type, [0, 0])
        bucket[0] += 1
        bucket[1] += int(entry.count)
    lines = ["| 实体类型 | 不同实体数 | 出现次数 |", "| --- | ---: | ---: |"]
    for etype, (distinct, occurrences) in sorted(stats.items(), key=lambda kv: (-kv[1][1], kv[0])):
        label = TYPE_LABELS.get(etype, etype)
        lines.append(f"| {_cell(label)}（`{etype}`） | {distinct} | {occurrences} |")
    if len(lines) == 2:
        lines.append("| （无） | 0 | 0 |")
    return lines


def _display_original(entity_type, text: str) -> str:
    """报告展示用：直接标识符打码，姓名/机构/地名保留（否则无法复核）。"""
    direct = {"id_card", "phone", "bank_card", "uscc", "email", "plate", "case_no"}
    if getattr(entity_type, "value", entity_type) in direct:
        if len(text) <= 4:
            return "*" * len(text)
        return f"{text[:2]}{'*' * max(1, len(text) - 4)}{text[-2:]}"
    return text


def _safe_filename(text: str) -> str:
    """把作用域名转成安全文件名。"""
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "_", text).strip("._")
    return cleaned or "case"


def _redact_known_values(text: str, vault: Vault) -> str:
    """把可交付报告里出现的已知真名/证件号全部替换成「（已隐去）」。

    映射表是唯一真名来源：凡是本次脱敏登记过的原值，都不允许出现在可交付报告里。
    再兜底掩掉 11 位以上的数字串（可能是未登记的新泄露），双重保险。
    """
    if not text:
        return text
    for entry in sorted(vault.entries, key=lambda e: -len(e.original)):
        if entry.original and entry.original in text:
            text = text.replace(entry.original, "（已隐去）")
    return re.sub(r"(?<!\d)\d{11,}(?!\d)", "（已隐去）", text)


def _public_message(finding: Finding, vault: Vault) -> str:
    """可交付报告用的命中说明：残留扫描不显示片段/上下文，其余消息隐去真名。"""
    if finding.code.startswith("residual:"):
        rule = finding.code.split(":", 1)[1]
        return f"残留「{rule}」（命中片段已隐去，完整内容见 `vault/复核清单.md`）"
    return _redact_known_values(finding.message, vault)


def is_image_finding(finding: Finding) -> bool:
    """图片类告警（证件照/印章/手写签名）：必须强制进人工复核。"""
    return finding.code.endswith(".image") or (
        finding.level in ("warn", "critical") and "图片" in (finding.message or "")
    )


_IMAGE_HINT = "本文件含 {n} 处图片（可能是证件照/印章/手写签名），图像内容无法逐字脱敏，请人工确认是否需要遮盖。"


def write_report(
    run_result: RunResult,
    vault: Vault,
    out_dir: str | pathlib.Path,
    *,
    masked_root: str | pathlib.Path | None = None,
    vault_path: str | pathlib.Path | None = None,
    dry_run: bool = False,
    notes: Iterable[str] = (),
    replacements: dict[str, int] | None = None,
    report_name: str | None = None,
    reveal_names: bool = False,
    review_details: dict[str, list[str]] | None = None,
    mode: int = 0o600,
) -> pathlib.Path:
    """生成 Markdown 报告，返回路径。

    ``reveal_names=False``（默认）：**可交付报告**，任何真名/证件号一律显示为
    「（已隐去）」，可随脱敏件一起交给工作台。
    ``reveal_names=True``：**复核清单**，保留真名、命中片段与判定理由，
    必须落在 vault 目录并保持 0o600，文件头带「切勿上传」警告。
    """
    target_dir = pathlib.Path(out_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    if report_name:
        report_path = target_dir / report_name
    elif reveal_names:
        report_path = target_dir / "复核清单.md"
    else:
        report_path = target_dir / "脱敏报告.md"

    total = len(run_result.files)
    ok = run_result.ok_count
    failed = run_result.failed
    all_findings = [f for fr in run_result.files for f in fr.findings]
    critical = [f for f in all_findings if f.level == "critical"]
    warnings = [f for f in all_findings if f.level == "warn"]
    image_files = [
        (fr.source, sum(1 for f in fr.findings if is_image_finding(f)))
        for fr in run_result.files
        if any(is_image_finding(f) for f in fr.findings)
    ]
    details = review_details or {}

    def show(message: str) -> str:
        return message if reveal_names else _public_message_for_text(message, vault)

    def show_finding(finding: Finding) -> str:
        return finding.message if reveal_names else _public_message(finding, vault)

    lines: list[str] = []
    if reveal_names:
        lines.append("> **本文件含真实姓名，仅供本地复核，切勿随交付包上传。**")
        lines.append("")
        lines.append(f"# 复核清单（含真名）· {run_result.scope or '(未命名案件)'}")
    else:
        lines.append(f"# 脱敏审计报告 · {run_result.scope or '(未命名案件)'}")
        lines.append("")
        lines.append(
            "> 本报告已隐去真实姓名，可随脱敏件一并交付；"
            "完整复核信息见 `vault/复核清单.md`（含真名，请勿上传）。"
        )
    lines.append("")
    lines.append("## 概要")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append(f"- 案件作用域：`{run_result.scope}`")
    lines.append(f"- 模式：{'dry-run（仅检测，未写任何文件）' if dry_run else '正式脱敏'}")
    lines.append(f"- 工具版本：{vault.tool_version}")
    if masked_root is not None:
        lines.append(f"- 脱敏件目录：`{pathlib.Path(masked_root)}`")
    if vault_path is not None:
        lines.append(f"- 加密映射表：`{pathlib.Path(vault_path)}`（权限 0o600，配套密钥为同目录 `.key`）")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | ---: |")
    lines.append(f"| 输入文件 | {total} |")
    lines.append(f"| 成功 | {ok} |")
    lines.append(f"| 失败 | {len(failed)} |")
    lines.append(f"| 不同实体（代号/掩码） | {len(vault.entries)} |")
    lines.append(f"| 累计替换处数 | {vault.total_occurrences()} |")
    lines.append(f"| 残留 critical | {len(critical)} |")
    lines.append(f"| 残留 warn | {len(warnings)} |")
    lines.append("")

    # ---------------------------------------------------------------- 清单 1
    lines.append("## 一、已处理")
    lines.append("")
    if run_result.files:
        lines.append("| 文件 | 文本块 | 替换处数 | 保留未替换 | 残留告警 | 状态 |")
        lines.append("| --- | ---: | ---: | ---: | ---: | --- |")
        for fr in run_result.files:
            replaced = (replacements or {}).get(fr.source, fr.entities)
            kept = max(0, fr.entities - replaced)
            status = "✅ 已脱敏" if fr.ok else "❌ 失败"
            if dry_run and fr.ok:
                status = "🔍 仅检测"
            lines.append(
                f"| {_cell(pathlib.Path(fr.source).name)} | {fr.blocks} | {replaced} | {kept} | "
                f"{len(fr.findings)} | {status} |"
            )
        if replacements is not None:
            lines.append("")
            lines.append("> 「替换处数」只统计实际改写的命中；「保留未替换」多为按 `policy: keep` 保留的日期与金额。")
        lines.append("")
        lines.append("### 按实体类型统计")
        lines.append("")
        lines.extend(_type_table(vault.entries))
        lines.append("")
        lines.append(
            "> 掩码编号 `XXX1`、`XXX2`… 在本案件内全局唯一：同一编号只对应一个原文，"
            "`restore` 可直接精确回填，不需要猜测。"
        )
    else:
        lines.append("（没有可处理的文件）")
    lines.append("")
    if dry_run:
        lines.append("> dry-run：未写出脱敏件、映射表与报告之外的文件；本次报告本身也未落盘。")
        lines.append("")

    # ---------------------------------------------------------------- 清单 2
    lines.append("## 二、需人工复核")
    lines.append("")
    if image_files:
        lines.append("### 2.0 图片内容（强制复核）")
        lines.append("")
        for source, count in image_files:
            lines.append(f"- `{pathlib.Path(source).name}`：{_IMAGE_HINT.format(n=count)}")
        lines.append("")

    lines.append("### 2.1 低置信度命中 / 姓名类命中")
    lines.append("")
    person_entries = [e for e in vault.entries if e.type == "person"]
    if person_entries:
        lines.append("姓名类实体（检测器召回为主，建议逐条核对是否误伤或漏检）：")
        lines.append("")
        lines.append("| 原文 | 代号 | 出现次数 |")
        lines.append("| --- | --- | ---: |")
        for entry in person_entries:
            original = entry.original if reveal_names else "（已隐去）"
            lines.append(f"| {_cell(original)} | {_cell(entry.placeholder)} | {entry.count} |")
        lines.append("")
    if reveal_names and vault.entries:
        lines.append("全部实体对照（含真名，仅本地复核使用）：")
        lines.append("")
        lines.append("| 类型 | 原文 | 代号 | 出现次数 |")
        lines.append("| --- | --- | --- | ---: |")
        for entry in sorted(vault.entries, key=lambda e: (e.type, -e.count)):
            lines.append(
                f"| {_cell(TYPE_LABELS.get(entry.type, entry.type))} | {_cell(entry.original)} | "
                f"{_cell(entry.placeholder)} | {entry.count} |"
            )
        lines.append("")

    ambiguous = vault.ambiguous_placeholders()
    if ambiguous:
        lines.append("以下代号同时对应多个原文，**回填时无法自动区分**，只能人工核对：")
        lines.append("")
        lines.append("| 代号 | 候选原文 | 类型 |")
        lines.append("| --- | --- | --- |")
        for placeholder, items in sorted(ambiguous.items()):
            for item in items:
                try:
                    shown = _display_original(EntityType(item.type), item.original)
                except ValueError:
                    shown = item.original
                if not reveal_names:
                    shown = "（已隐去）"
                lines.append(f"| {_cell(placeholder)} | {_cell(shown)} | {_cell(TYPE_LABELS.get(item.type, item.type))} |")
        lines.append("")
        lines.append("> 原因：掩码类在同一类型内只有一个不同值时只用 `XXX`；")
        lines.append("> 缓解：`restore` 会按邻近的「身份证号 / 联系电话 / 账户 / 邮箱」等标签逐处判定，表格场景借用列名。")
        lines.append("")

    if any(details.values()) or any(fr.needs_review for fr in run_result.files):
        lines.append("运行期标记的低置信度/疑似命中：")
        lines.append("")
        for fr in run_result.files:
            items = details.get(fr.source, fr.needs_review)
            for item in items:
                lines.append(f"- `{pathlib.Path(fr.source).name}`：{show(item)}")
        lines.append("")

    lines.append("### 2.2 残留扫描命中（脱敏件 + 格式特有泄露点）")
    lines.append("")
    if critical or warnings:
        lines.append("| 级别 | 文件 | 位置 | 说明 |")
        lines.append("| --- | --- | --- | --- |")
        for finding in sorted(all_findings, key=lambda f: (f.level != "critical", f.location)):
            if finding.level not in ("critical", "warn"):
                continue
            file_hint = finding.location.split(" ")[0] if finding.location else "—"
            lines.append(
                f"| {'🔴 critical' if finding.level == 'critical' else '🟡 warn'} | {_cell(file_hint)} | "
                f"{_cell(finding.location)} | {_cell(show_finding(finding))} |"
            )
        lines.append("")
        lines.append("处理建议：critical 命中说明仍有可直接识别个人的信息，**不要**把该文件交给外部工作台；"
                     "请补充检测规则或手工修改后重新运行 mask，再复扫一遍。")
    else:
        lines.append("（未发现残留命中）")
    lines.append("")

    # ---------------------------------------------------------------- 清单 3
    lines.append("## 三、无法处理")
    lines.append("")
    if failed:
        lines.append("| 文件 | 原因 | 下一步 |")
        lines.append("| --- | --- | --- |")
        for fr in failed:
            reason = fr.error or "未知错误"
            hint = _failure_hint(reason)
            lines.append(f"| {_cell(pathlib.Path(fr.source).name)} | {_cell(show(reason))} | {_cell(hint)} |")
        lines.append("")
    else:
        lines.append("（本次运行没有失败文件）")
        lines.append("")
    lines.append("以下情形即使运行成功也不在本工具能力内，请人工确认：")
    lines.append("")
    lines.append("- **扫描件 / 图片型 PDF**：正文是图像，没有文本层，未做 OCR 时无法脱敏；请先 OCR 或人工处理。")
    lines.append("- **加密 / 有打开口令的 PDF、Office 文件**：无法读取正文，需先解除密码并确认授权。")
    lines.append("- **图片、截图、页眉图片、印章**：图片内文字不在文本层，本工具不处理。")
    lines.append("- **批注、修订记录、隐藏工作表、文档属性**：取决于各格式处理器的覆盖范围，见上方 warn 命中。")
    if notes:
        lines.append("")
        for note in notes:
            lines.append(f"- {show(note)}")
    lines.append("")

    # ---------------------------------------------------------------- 风险提示
    lines.append("## 四、风险提示")
    lines.append("")
    lines.append("> **日期与金额默认保留。** 这是为了让法律 AI 工作台能分析时间线与标的额；"
                 "但极端特定的「日期 + 金额 + 程序阶段」事实组合，理论上仍可能被反推到具体个案。"
                 "涉及高度敏感案件时，请在 `config/rules.yaml` 的 `policy` 中把 `date`、`amount` 改为 `mask` 后重跑。")
    lines.append("")
    lines.append("> **PDF 文本层顺序。** PDF 的文本层因脱敏重写，默认提取顺序不可靠；下游分析请使用同名 `.txt`，"
                 "或对 PDF 指定 `sort=True`。本包已为每个 PDF 导出同名 `.txt`。")
    lines.append("")
    lines.append("> **映射表与密钥是最高机密。** 映射表已用 Fernet 加密，`.key` 与映射表同目录、权限 0o600；"
                 "两者缺一不可。密钥丢失 = 映射不可恢复，只能重新脱敏。")
    lines.append("")
    lines.append("> **回填前先确认分析完成。** 回填件含真名，不要在联网环境中打开或上传。")
    lines.append("")
    lines.append("> **文件名与目录名未做脱敏。** 若原文件名/案件文件夹名含当事人姓名，请在交付前重命名。")
    lines.append("")

    lines.append("## 五、回填命令")
    lines.append("")
    vault_ref = pathlib.Path(vault_path) if vault_path is not None else pathlib.Path("<案件>/<scope>.json")
    lines.append("```bash")
    lines.append("PYTHONPATH=src .venv/bin/python -m masking.cli restore <脱敏文件> \\")
    lines.append(f"  --vault '{vault_ref}' --out <回填输出路径>")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("本报告由本地脱敏工具生成；脱敏结果仍需人工抽查，工具不替代法律判断。")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        import os

        os.chmod(report_path, mode)
    except OSError:
        pass
    return report_path


def _public_message_for_text(message: str, vault: Vault) -> str:
    """非 Finding 的自由文本（notes / error）在可交付报告里隐去真名。"""
    return _redact_known_values(message, vault)


def _failure_hint(reason: str) -> str:
    """根据失败原因给出下一步建议。"""
    if "词池" in reason and "用尽" in reason:
        return "扩充 config/lexicon.yaml 的 pools 后重跑"
    if "不支持" in reason or "格式" in reason:
        return "转成 docx/txt 后重跑，或人工处理"
    if "密码" in reason or "加密" in reason:
        return "PDF 已加密，需密码：解除密码并确认授权后重跑（本工具不破解密码）"
    if "不存在" in reason:
        return "检查路径后重跑"
    return "查看错误详情，手工处理该文件"


__all__ = [
    "CRITICAL_KEYWORDS",
    "TYPE_LABELS",
    "audit_file",
    "is_image_finding",
    "scan_text",
    "write_report",
]

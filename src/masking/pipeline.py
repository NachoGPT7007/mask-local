"""编排层：把一个案件（单文件或文件夹）串成完整流水线。

流程
----
1. 解析输出目录（``config/rules.yaml`` 的 ``output`` 段）。
2. 递归收集输入文件，跳过隐藏文件与输出目录自身。
3. 一个案件共用一个 ``PlaceholderAllocator``：同一个人在 docx 与 pdf 里
   必定拿到同一个代号，这是本工具的核心约束。
4. 逐文件：handler.extract -> build_edits -> handler.apply -> 残留审计。
   单个文件失败只记入 ``FileResult``，不中断整个案件。
5. 落盘加密映射表，生成审计报告。

安全底线
--------
- 永不修改输入原件：输出路径与原件相同直接判失败。
- 没有检测器时**拒绝运行**，避免产出「看起来脱敏、实际全是原文」的文件。
- dry-run 不写任何文件（含映射表与报告）。
"""

from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .allocator import MappingEntry, PlaceholderAllocator, PoolExhausted
from .audit import is_image_finding, write_report
from .config import Settings, project_home
from .detectors import build_detectors
from .engine import FileResult, RunResult, build_edits
from .entity import Edit, Entity, EntityType, Finding, MatchContext, TextBlock
from .formats import handler_for, supported_suffixes
from .vault import Vault

#: 低于该置信度的命中强制进入「需人工复核」
LOW_CONFIDENCE = 0.85
#: 需要人工复核的实体类型（姓名最容易误伤/漏检）
REVIEW_TYPES: frozenset[EntityType] = frozenset({EntityType.PERSON, EntityType.CUSTOM})
#: 残留原文核对时最多检查的条目数（避免超大案件拖慢）
MAX_ORIGINAL_CHECKS = 300


class PipelineError(RuntimeError):
    """流水线可预期错误（带用户可执行的下一步）。"""


@dataclass(frozen=True)
class OutputDirs:
    """一次运行的目录布局。

    - ``external=True``（传了 out_dir）：交付包自包含——
      ``<out_dir>/脱敏件…``、``<out_dir>/vault/<案件>.json + .key``、``<out_dir>/脱敏报告.md``，
      整个目录可打包下载，解压后映射表与报告都在里面。
    - ``external=False``（未传 out_dir）：回退到项目级 ``output/<案件>/`` 与 ``vault/<案件>/``。
    """

    scope: str
    masked: pathlib.Path
    vault: pathlib.Path
    project: pathlib.Path
    report_dir: pathlib.Path
    external: bool


@dataclass
class MaskOutcome:
    """``mask_path_detailed`` 的完整产物，供 CLI 展示路径。"""

    result: RunResult
    dirs: OutputDirs
    vault: Vault | None = None
    vault_path: pathlib.Path | None = None
    report_path: pathlib.Path | None = None
    dry_run: bool = False
    notes: list[str] = field(default_factory=list)
    #: 文件 source -> 真实替换处数（不含 date/amount 等 keep 策略）
    replacements: dict[str, int] = field(default_factory=dict)
    #: 文件 source -> 含真名的复核明细（只写 vault/复核清单.md）
    review_details: dict[str, list[str]] = field(default_factory=dict)
    review_path: pathlib.Path | None = None


# --------------------------------------------------------------------- 路径与收集


def output_dirs(
    input_path: str | pathlib.Path,
    out_dir: str | pathlib.Path | None = None,
    settings: Settings | None = None,
) -> OutputDirs:
    """计算输出目录。

    - 传 ``out_dir``：自包含交付包。脱敏件直接放 ``out_dir``（keep_tree 保留相对结构），
      映射表与报告放 ``out_dir/vault/`` 与 ``out_dir/脱敏报告.md``，便于整体打包下载。
    - 未传 ``out_dir``：回退到项目级 ``<项目>/<output.masked_dir>[/<案件>]``，
      映射表与报告在 ``<项目>/<output.vault_dir>/<案件>/``。
    """
    settings = settings or Settings.load()
    output_cfg = settings.rules.output or {}
    masked_name = str(output_cfg.get("masked_dir", "output"))
    vault_name = str(output_cfg.get("vault_dir", "vault"))
    keep_tree = bool(output_cfg.get("keep_tree", True))
    project = project_home()
    source = pathlib.Path(input_path).expanduser().resolve()
    scope = source.name if source.is_dir() else source.stem

    if out_dir:
        root = pathlib.Path(out_dir).expanduser().resolve()
        return OutputDirs(
            scope=scope,
            masked=root,
            vault=root / vault_name,
            project=project,
            report_dir=root,
            external=True,
        )

    base = project / masked_name
    masked = base / scope if keep_tree else base
    vault = project / vault_name / scope
    return OutputDirs(
        scope=scope,
        masked=masked,
        vault=vault,
        project=project,
        # 可交付报告与脱敏件放一起；含真名的复核清单在 vault 目录
        report_dir=masked,
        external=False,
    )


def _is_within(path: pathlib.Path, roots: Sequence[pathlib.Path]) -> bool:
    """path 是否落在任一 root 内（含相等）。"""
    for root in roots:
        try:
            if path == root or path.is_relative_to(root):
                return True
        except (OSError, ValueError):
            continue
    return False


def collect_files(source: pathlib.Path, dirs: OutputDirs) -> tuple[list[pathlib.Path], pathlib.Path]:
    """收集待处理文件，返回 (文件列表, 相对路径基准目录)。

    规则：递归、跳过隐藏文件/目录、跳过输出目录与映射表目录自身。
    """
    if source.is_file():
        return [source], source.parent
    if not source.is_dir():
        raise PipelineError(f"输入路径既不是文件也不是文件夹：{source}")

    roots = [dirs.masked.resolve(), dirs.vault.resolve()]
    files: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        current = pathlib.Path(dirpath)
        kept_dirs: list[str] = []
        for name in sorted(dirnames):
            candidate = (current / name).resolve()
            if name.startswith(".") or _is_within(candidate, roots):
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            candidate = current / name
            if _is_within(candidate.resolve(), roots):
                continue
            files.append(candidate)
    return files, source


# --------------------------------------------------------------------- 路由


def _make_router(
    handlers: Sequence[object] | None,
) -> tuple[Callable[[pathlib.Path], object | None], list[str]]:
    """返回 (路由函数, 支持的后缀列表)。

    未显式传入 handlers 时使用 formats 自动装配；显式传入时只认传入的处理器，
    便于测试与未来接入自定义处理器。
    """
    if handlers is not None:
        registered = list(handlers)

        def route(path: pathlib.Path):
            suffix = path.suffix.lower()
            for handler in registered:
                if suffix in getattr(handler, "suffixes", ()):
                    return handler
            return None

        suffixes = sorted({s for h in registered for s in getattr(h, "suffixes", ())})
        return route, suffixes

    def route_default(path: pathlib.Path):
        return handler_for(path)

    return route_default, supported_suffixes()


def _entity_label(entity_type: EntityType) -> str:
    from .audit import TYPE_LABELS

    return TYPE_LABELS.get(entity_type.value, entity_type.value)


def _mask_token(value: str) -> str:
    """数字类原文在报告里打码，避免二次泄露。"""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * max(1, len(value) - 4)}{value[-2:]}"


def _display_original(entity: EntityType, text: str) -> str:
    """报告展示用：直接标识符打码，姓名/机构/地名保留（否则无法复核）。"""
    if entity in (
        EntityType.ID_CARD,
        EntityType.PHONE,
        EntityType.BANK_CARD,
        EntityType.USCC,
        EntityType.EMAIL,
        EntityType.PLATE,
        EntityType.CASE_NO,
    ):
        return _mask_token(text)
    return text


# --------------------------------------------------------------------- 单文件


def _simulate(blocks: Sequence[TextBlock], edits: Sequence[Edit]) -> list[TextBlock]:
    """dry-run 用：在内存里应用替换，模拟脱敏后的文本。"""
    by_locator: dict[int, list[Edit]] = {}
    for edit in edits:
        by_locator.setdefault(id(edit.block), []).append(edit)
    simulated: list[TextBlock] = []
    for block in blocks:
        text = block.text
        for edit in sorted(by_locator.get(id(block), []), key=lambda e: e.entity.start, reverse=True):
            text = text[: edit.entity.start] + edit.replacement + text[edit.entity.end:]
        simulated.append(TextBlock(text=text, locator=block.locator, role=block.role))
    return simulated


def _check_originals(
    blocks: Sequence[TextBlock],
    entries: Sequence[MappingEntry],
    label: str,
) -> list[Finding]:
    """脱敏件里若仍能找到原文，说明漏检，直接判 critical。"""
    findings: list[Finding] = []
    checked = 0
    for entry in entries:
        if entry.placeholder == entry.original or len(entry.original) < 2:
            continue
        checked += 1
        if checked > MAX_ORIGINAL_CHECKS:
            break
        # 用 EntityType 走展示逻辑
        try:
            etype = EntityType(entry.type)
        except ValueError:
            etype = EntityType.CUSTOM
        shown = _display_original(etype, entry.original)
        for block in blocks:
            if entry.original in block.text:
                findings.append(
                    Finding(
                        level="critical",
                        code="original_left",
                        message=(
                            f"脱敏件中仍出现原文「{shown}」（对应代号 {entry.placeholder}），"
                            "说明该处检测漏检，请人工修改后重新扫描。"
                        ),
                        location=f"{label}/{block.role}",
                    )
                )
                break
    return findings


def _export_pdf_text(pdf_path: pathlib.Path, txt_path: pathlib.Path) -> pathlib.Path:
    """导出与脱敏 PDF 同名的 .txt（``sort=True``，保留分页符）。

    脱敏重写后的 PDF 内容流是追加的，默认 ``get_text()`` 会把代号排到页尾；
    下游法律工作台多数用默认提取，所以必须给一份顺序正确的纯文本。
    """
    try:
        import pymupdf as fitz
    except ImportError:  # pragma: no cover - 旧版本回退
        import fitz

    document = fitz.open(str(pdf_path))
    try:
        pages = [page.get_text(sort=True) for page in document]
    finally:
        document.close()
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    # \f 保留分页符，工作台/编辑器都能识别
    txt_path.write_text("\n\n\f\n\n".join(pages) + "\n", encoding="utf-8")
    return txt_path


def _merge_last_findings(result: FileResult, handler: object) -> None:
    """合并 handler.last_findings。

    pdf-fmt 把页级失败、定位失败、字体回退、扫描残留复核挂在 ``last_findings`` 上
    （apply 无返回值），这里补读并去重，否则报告会漏掉这些告警。
    """
    extras = getattr(handler, "last_findings", None) or []
    seen = {(f.level, f.code, f.message, f.location) for f in result.findings}
    for finding in extras:
        if not isinstance(finding, Finding):
            continue
        key = (finding.level, finding.code, finding.message, finding.location)
        if key in seen:
            continue
        seen.add(key)
        result.findings.append(finding)


def _looks_encrypted(result: FileResult, exception: Exception | None = None) -> bool:
    """判断失败是否由「文件已加密/需要密码」导致。"""
    if exception is not None:
        text = f"{type(exception).__name__} {exception}".lower()
        if any(key in text for key in ("encrypt", "password", "decrypt", "密码", "加密")):
            return True
    for finding in result.findings:
        text = f"{finding.code} {finding.message}".lower()
        if "encrypted" in text or "加密" in text or "password" in text:
            return True
    return False


def _process_file(
    path: pathlib.Path,
    base: pathlib.Path,
    dirs: OutputDirs,
    settings: Settings,
    detectors: Sequence[object],
    route: Callable[[pathlib.Path], object | None],
    suffixes: Sequence[str],
    allocator: PlaceholderAllocator,
    ctx: MatchContext,
    *,
    dry_run: bool,
    keep_tree: bool,
    audit_output: bool,
) -> tuple[FileResult, int, list[str]]:
    """处理单个文件；任何异常都收敛进 FileResult，不向调用方抛出。

    返回 (FileResult, 真实替换处数, 含真名的复核明细)。
    替换处数只有非 keep 策略的命中才计数；复核明细只写 vault/复核清单.md。
    """
    relative = path.relative_to(base) if keep_tree else pathlib.Path(path.name)
    out_path = dirs.masked / relative
    result = FileResult(source=str(path), output=str(out_path))

    handler = route(path)
    if handler is None:
        supported = "、".join(suffixes) or "（暂无已注册格式）"
        result.error = (
            f"不支持的格式：{path.suffix or '(无后缀)'}。当前支持：{supported}。"
            "可先转成 docx/txt 再处理，或人工脱敏。"
        )
        result.findings.append(Finding("warn", "unsupported_format", result.error, str(path)))
        return result, 0, []

    try:
        blocks = list(handler.extract(str(path)))
    except Exception as exc:  # 加密、损坏、扫描件等
        _merge_last_findings(result, handler)
        if _looks_encrypted(result, exc):
            result.error = "PDF 已加密，需密码：本工具不破解密码，请先解除密码并确认授权后重跑。"
        else:
            result.error = f"读取失败：{exc}。请确认文件未损坏；扫描件需先 OCR。"
        return result, 0, []
    _merge_last_findings(result, handler)
    result.blocks = len(blocks)
    if not blocks:
        if _looks_encrypted(result):
            result.error = "PDF 已加密，需密码：本工具不破解密码，请先解除密码并确认授权后重跑。"
        else:
            result.error = (
                "文件里没有可检测的文本（可能是扫描件/图片型 PDF）。"
                "请先用 OCR 生成文本层，或人工处理该文件。"
            )
        return result, 0, []

    try:
        edits, entity_count = build_edits(blocks, detectors, ctx, allocator)
    except PoolExhausted as exc:
        result.error = (
            f"{exc}（这是保护性中止：绝不复用代号，否则两个主体会撞号。）"
            "处理办法：扩充 config/lexicon.yaml 对应词池后重跑，或在 config/rules.yaml 调整 policy。"
        )
        result.findings.append(Finding("critical", "pool_exhausted", result.error, str(path)))
        return result, 0, []
    except Exception as exc:
        result.error = f"检测阶段失败：{exc}"
        return result, 0, []
    result.entities = entity_count

    review_full: list[str] = []
    for edit in edits:
        if edit.entity.type in REVIEW_TYPES and edit.entity.confidence < LOW_CONFIDENCE:
            label = _entity_label(edit.entity.type)
            display = _display_original(edit.entity.type, edit.entity.text)
            tail = f" → {edit.replacement}（置信度 {edit.entity.confidence:.2f}，来源 {edit.entity.detector or '未知'}）"
            result.needs_review.append(f"{label}「（已隐去）」{tail}")
            review_full.append(f"{label}「{display}」{tail}")

    # 本案件已生成的代号/自编值：残留扫描要排除，否则会「自己扫自己」
    own_values = [entry.placeholder for entry in allocator.entries]

    if dry_run:
        simulated = _simulate(blocks, edits)
        for block in simulated:
            result.findings.extend(
                audit_scan(
                    block.text,
                    settings.rules,
                    location=f"{path.name}(模拟脱敏)/{block.role}",
                    ignore_values=own_values,
                )
            )
        image_findings = [f for f in result.findings if is_image_finding(f)]
        if image_findings:
            hint = (
                f"本文件含 {len(image_findings)} 处图片（可能是证件照/印章/手写签名），"
                "图像内容无法逐字脱敏，请人工确认是否需要遮盖。"
            )
            result.needs_review.append(hint)
            review_full.append(hint)
        critical = [f for f in result.findings if f.level == "critical"]
        if critical:
            result.error = (
                f"预扫发现 {len(critical)} 处 critical 残留：按当前检测规则脱敏后仍会泄露，"
                "直接运行会产出不可交付的文件；请先修检测规则或人工处理。"
            )
        result.ok = not critical
        return result, len(edits), review_full

    # 绝不覆盖原件
    try:
        if out_path.resolve() == path.resolve():
            result.error = "输出路径与原件相同，已拒绝覆盖。请检查 --out 是否指向了输入目录。"
            result.findings.append(Finding("critical", "same_path", result.error, str(path)))
            return result, 0, []
    except OSError:
        pass

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        handler.apply(str(path), str(out_path), edits)
    except Exception as exc:
        result.error = f"写回失败：{exc}"
        return result, 0, []

    if not out_path.exists():
        result.error = "写回后未找到输出文件，格式处理器可能未正确保存。"
        return result, 0, []

    # 迭代脱敏：一次脱敏可能因块切分方式、上下文边界变化而漏掉少数实体。
    # 实测某份 2.3 万字的 docx 首轮漏 4 处（同一个公司在 194 次出现里漏 1 次），
    # 对输出再跑一轮即清零。用同一个 allocator，代号完全一致，不会产生新代号。
    # 最多 2 轮，避免病态文件无限循环。
    for _round in range(2):
        try:
            again = list(handler.extract(str(out_path)))
            more, _ = build_edits(again, detectors, ctx, allocator)
        except Exception:
            break
        if not more:
            break
        tmp_round = out_path.with_name(f"{out_path.stem}.round{_round + 2}{out_path.suffix}")
        try:
            handler.apply(str(out_path), str(tmp_round), more)
            tmp_round.replace(out_path)
            result.findings.append(
                Finding(
                    "info",
                    "iterative_pass",
                    f"第 {_round + 2} 轮补脱 {len(more)} 处（首轮因块切分漏检）",
                    str(out_path),
                )
            )
        except Exception:
            if tmp_round.exists():
                tmp_round.unlink(missing_ok=True)
            break

    # apply 阶段挂出的告警（页级失败 / 定位失败 / 字体回退 / 扫描残留复核）
    _merge_last_findings(result, handler)

    # PDF 伴生 .txt：脱敏重写会打乱默认提取顺序，下游必须读顺序正确的纯文本
    if out_path.suffix.lower() == ".pdf":
        txt_path = out_path.with_suffix(".txt")
        try:
            _export_pdf_text(out_path, txt_path)
            result.findings.append(
                Finding(
                    "info",
                    "pdf.text_companion",
                    f"已导出同名文本 {txt_path.name}（PDF 默认提取顺序不可靠，下游请优先使用该 .txt）",
                    str(txt_path),
                )
            )
        except Exception as exc:
            result.findings.append(
                Finding(
                    "warn",
                    "pdf.text_companion_failed",
                    f"导出同名 .txt 失败：{exc}；下游请对 PDF 使用 sort=True 提取",
                    str(out_path),
                )
            )

    # 格式特有的泄露点（源文件的元数据、批注等）
    try:
        result.findings.extend(handler.audit(str(path)))
    except Exception as exc:
        result.findings.append(Finding("warn", "handler_audit_error", f"格式审计失败：{exc}", str(path)))

    # 对输出做残留复扫 + 原文核对
    if audit_output:
        try:
            out_blocks = list(handler.extract(str(out_path)))
        except Exception as exc:
            result.findings.append(
                Finding("warn", "output_audit_skipped", f"无法复扫输出文件，请手工检查：{exc}", str(out_path))
            )
            out_blocks = []
        own_values = [entry.placeholder for entry in allocator.entries]
        for block in out_blocks:
            result.findings.extend(
                audit_scan(
                    block.text,
                    settings.rules,
                    location=f"{path.name}(脱敏后)/{block.role}",
                    ignore_values=own_values,
                )
            )
        result.findings.extend(_check_originals(out_blocks, allocator.entries, path.name))

    image_findings = [f for f in result.findings if is_image_finding(f)]
    if image_findings:
        hint = (
            f"本文件含 {len(image_findings)} 处图片（可能是证件照/印章/手写签名），"
            "图像内容无法逐字脱敏，请人工确认是否需要遮盖。"
        )
        result.needs_review.append(hint)
        review_full.append(hint)

    # 兜底：输出里若还有 critical 残留，视为「已产出但需人工处理」。
    #
    # 这里刻意不把 ok 置 False：实测真实材料时，2 万字文书里 500+ 处替换成功、
    # 只剩 1 处边角，判「失败」会让用户以为整份白做了，而脱敏件其实已经可用。
    # 正确做法是照常交付、在报告与清单里把这几处标红，由人复核——
    # 脱敏本来就是「自动处理 + 重点复核」，不是「全自动零漏检」。
    critical = [f for f in result.findings if f.level == "critical"]
    if critical:
        result.error = (
            f"已产出脱敏件，但有 {len(critical)} 处需人工处理："
            "见脱敏报告「2.2 残留扫描命中」与 vault/复核清单.md，改完再复扫一遍。"
        )
    result.ok = True
    return result, len(edits), review_full


def audit_scan(text: str, rules, location: str, ignore_values: Sequence[str] = ()) -> list[Finding]:
    """延迟导入 ``scan_text``，避免 pipeline <-> audit 的循环导入。"""
    from .audit import scan_text

    return scan_text(text, rules, location=location, ignore_values=ignore_values)


# --------------------------------------------------------------------- 主入口


def mask_path_detailed(
    input_path: str | pathlib.Path,
    out_dir: str | pathlib.Path | None = None,
    settings: Settings | None = None,
    *,
    detectors: Sequence[object] | None = None,
    handlers: Sequence[object] | None = None,
    dry_run: bool = False,
    write_report_file: bool = True,
    audit_output: bool = True,
) -> MaskOutcome:
    """完整流水线，返回带路径的 ``MaskOutcome``（CLI 用）。"""
    source = pathlib.Path(input_path).expanduser().resolve()
    if not source.exists():
        raise PipelineError(f"输入路径不存在：{source}")
    settings = settings or Settings.load()
    dirs = output_dirs(source, out_dir, settings)

    active_detectors = list(detectors) if detectors is not None else build_detectors(settings)
    if not active_detectors:
        raise PipelineError(
            "没有可用的检测器，已拒绝运行。\n"
            "原因：detectors/ 目录下还没有任何提供 make() 工厂的模块，此时「脱敏」不会替换任何内容，"
            "产出文件看似正常、实则全是原文。\n"
            "下一步：运行 `PYTHONPATH=src .venv/bin/python -m masking.cli doctor` 查看装配状态；"
            "补齐检测器后重跑。"
        )

    route, suffixes = _make_router(handlers)
    files, base = collect_files(source, dirs)
    keep_tree = bool((settings.rules.output or {}).get("keep_tree", True))

    result = RunResult(scope=dirs.scope)
    allocator = PlaceholderAllocator(settings.lexicon, settings.rules, scope=dirs.scope)
    ctx = MatchContext(scope=dirs.scope)
    outcome = MaskOutcome(result=result, dirs=dirs, dry_run=dry_run)

    if not files:
        result.files.append(
            FileResult(
                source=str(source),
                ok=False,
                error="没有找到可处理的文件（可能全部是隐藏文件，或输出目录与输入目录重合）。",
            )
        )
        outcome.notes.append("输入为空，未生成脱敏件。")
    else:
        for path in files:
            file_result, replaced, review_full = _process_file(
                path,
                base,
                dirs,
                settings,
                active_detectors,
                route,
                suffixes,
                allocator,
                ctx,
                dry_run=dry_run,
                keep_tree=keep_tree,
                audit_output=audit_output,
            )
            # engine.FileResult 没有 replacements 字段（契约不改），这里运行时附加，
            # 上层（CLI / web.server）可用 getattr(fr, "replacements", None) 容错读取。
            setattr(file_result, "replacements", replaced)
            outcome.result.files.append(file_result)
            outcome.replacements[str(path)] = replaced
            outcome.review_details[str(path)] = review_full

    result.entries = allocator.entries
    if ctx.stats:
        for key, count in ctx.stats.items():
            if key.startswith("detector_error:"):
                outcome.notes.append(f"检测器 {key.split(':', 1)[1]} 抛出异常 {count} 次，结果可能漏检，建议人工抽查。")

    vault = Vault.from_entries(dirs.scope, result.entries)
    if dry_run:
        outcome.vault = vault
        outcome.notes.append("dry-run：未写出脱敏件、映射表与报告。")
        return outcome

    if result.entries:
        dirs.vault.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(dirs.vault, 0o700)
        except OSError:
            pass
        safe_scope = re.sub(r"[\\/:*?\"<>|\s]+", "_", dirs.scope).strip("._") or "case"
        outcome.vault_path = vault.save(dirs.vault / f"{safe_scope}.json")
        outcome.vault = vault

    if write_report_file:
        common = dict(
            masked_root=dirs.masked,
            vault_path=outcome.vault_path,
            dry_run=False,
            notes=outcome.notes,
            replacements=outcome.replacements,
            review_details=outcome.review_details,
        )
        # 可交付报告：不含真名，可随脱敏件上传
        outcome.report_path = write_report(
            result,
            vault,
            dirs.report_dir,
            report_name="脱敏报告.md",
            reveal_names=False,
            mode=0o644,
            **common,
        )
        # 复核清单：含真名与片段，只在 vault 目录，0600
        outcome.review_path = write_report(
            result,
            vault,
            dirs.vault,
            report_name="复核清单.md",
            reveal_names=True,
            mode=0o600,
            **common,
        )
    return outcome


def mask_path(
    input_path: str | pathlib.Path,
    out_dir: str | pathlib.Path | None = None,
    settings: Settings | None = None,
    *,
    dry_run: bool = False,
    detectors: Sequence[object] | None = None,
    handlers: Sequence[object] | None = None,
) -> RunResult:
    """契约入口：脱敏一个文件或一个案件文件夹。

    需要路径信息（CLI 展示）时用 ``mask_path_detailed``。
    """
    outcome = mask_path_detailed(
        input_path,
        out_dir,
        settings,
        detectors=detectors,
        handlers=handlers,
        dry_run=dry_run,
    )
    return outcome.result


__all__ = [
    "LOW_CONFIDENCE",
    "MaskOutcome",
    "OutputDirs",
    "PipelineError",
    "collect_files",
    "mask_path",
    "mask_path_detailed",
    "output_dirs",
]

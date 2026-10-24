"""命令行入口：``python -m masking.cli``。

子命令
------
- ``mask <路径>``：脱敏单文件或整个案件文件夹。
- ``restore <文件> --vault <映射表> --out <输出>``：分析完成后回填真名。
- ``audit <路径> [--vault <映射表>]``：对已脱敏目录做残留复扫。
- ``doctor``：环境自检（依赖、OCR、字体、词池余量、格式/检测器装配）。

返回码：0 成功；1 部分失败（或审计发现残留）；2 全部失败 / 无法开始。
所有输出为中文，带统计数字与可执行的下一步建议。
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import os
import pathlib
import platform
import shutil
import sys
from typing import Sequence

from . import __version__
from .audit import audit_file
from .config import Settings, config_dir, project_home
from .restore import TEXT_SUFFIXES, RestoreError, restore_file
from .vault import Vault, VaultError, KEY_FILENAME

#: doctor 检查的依赖：导入名 -> 发行包名
_DEPENDENCIES: tuple[tuple[str, str], ...] = (
    ("cryptography", "cryptography"),
    ("yaml", "PyYAML"),
    ("docx", "python-docx"),
    ("openpyxl", "openpyxl"),
    ("fitz", "PyMuPDF"),
    ("pytest", "pytest"),
)


# --------------------------------------------------------------------- 输出小工具


def _line(char: str = "─", width: int = 62) -> str:
    return char * width


def _ok(text: str) -> str:
    return f"✓ {text}"


def _bad(text: str) -> str:
    return f"✗ {text}"


def _warn(text: str) -> str:
    return f"⚠ {text}"


def _print_header(title: str) -> None:
    print(_line("═"))
    print(f"本地脱敏工具 · {title}")
    print(_line("═"))


def _print_error(message: str) -> None:
    print(f"\n{_bad('操作未完成')}\n{message}", file=sys.stderr)


# --------------------------------------------------------------------- mask


def _build_settings(args: argparse.Namespace) -> Settings:
    lexicon = pathlib.Path(args.lexicon).expanduser() if args.lexicon else None
    rules = pathlib.Path(args.rules).expanduser() if args.rules else None
    return Settings.load(lexicon, rules)


def cmd_mask(args: argparse.Namespace) -> int:
    from .pipeline import PipelineError, mask_path_detailed

    _print_header("mask 脱敏")
    try:
        settings = _build_settings(args)
    except Exception as exc:
        _print_error(f"配置加载失败：{exc}\n请检查 --lexicon / --rules 指向的 YAML 是否存在且格式正确。")
        return 2

    # 续跑：复用已有映射表，同一原文拿回原代号
    resume_vault = None
    if getattr(args, "vault", None):
        from .vault import Vault, VaultError

        try:
            resume_vault = Vault.load(args.vault)
        except VaultError as exc:
            _print_error(f"映射表无法读取：{exc}")
            return 2
        print(f"续跑：已有 {len(resume_vault.entries)} 个实体，新实体接着分配代号")

    # 手动补词：逗号 / 顿号 / 分号 / 换行都能分隔
    extra_terms: list[str] = []
    raw_terms = getattr(args, "terms", None)
    if raw_terms:
        import re as _re

        extra_terms = [t.strip() for t in _re.split(r"[,，、;；\n]+", raw_terms) if t.strip()]
        if extra_terms:
            head = "、".join(extra_terms[:6]) + ("…" if len(extra_terms) > 6 else "")
            print(f"手动补词：{len(extra_terms)} 个（{head}）")

    try:
        outcome = mask_path_detailed(
            args.path,
            out_dir=args.out,
            settings=settings,
            dry_run=bool(args.dry_run),
            write_report_file=bool(args.report),
            vault=resume_vault,
            extra_terms=extra_terms,
        )
    except PipelineError as exc:
        _print_error(str(exc))
        return 2
    except Exception as exc:  # 兜底：给出文件路径方便排查
        _print_error(f"运行失败：{exc}")
        return 2

    result = outcome.result
    print(f"输入：{pathlib.Path(args.path).expanduser().resolve()}")
    print(f"案件作用域：{outcome.dirs.scope}")
    print(f"检测器：{len(_detector_names(settings))} 个")
    print()

    width = max((len(pathlib.Path(fr.source).name) for fr in result.files), default=10)
    for fr in result.files:
        name = pathlib.Path(fr.source).name.ljust(width)
        replaced = outcome.replacements.get(fr.source, fr.entities)
        if fr.ok:
            tail = f"{fr.blocks} 块 / 替换 {replaced} 处 / {len(fr.findings)} 告警"
            if fr.needs_review:
                tail += f" / {len(fr.needs_review)} 条待复核"
            print(f"  {_ok(name)}  {tail}")
        else:
            print(f"  {_bad(name)}  {fr.error}")

    critical = [f for fr in result.files for f in fr.findings if f.level == "critical"]
    warnings = [f for fr in result.files for f in fr.findings if f.level == "warn"]
    blocked = [fr for fr in result.files if not fr.ok and "不可交付" in fr.error]
    print()
    print(_line())
    print(f"文件：共 {len(result.files)}，成功 {result.ok_count}，失败 {len(result.failed)}")
    if blocked:
        print(_bad(f"其中 {len(blocked)} 个文件含 critical 残留，不可交付外部工作台"))
    print(f"代号：{len(result.entries)} 个不同实体，累计替换 {sum(int(e.count) for e in result.entries)} 处")
    stats = {}
    for entry in result.entries:
        stats[entry.type] = stats.get(entry.type, 0) + 1
    if stats:
        from .audit import TYPE_LABELS

        summary = " / ".join(f"{TYPE_LABELS.get(k, k)} {v}" for k, v in sorted(stats.items(), key=lambda kv: -kv[1]))
        print(f"类型：{summary}")
    print(f"残留：critical {len(critical)}，warn {len(warnings)}")

    if args.dry_run:
        print()
        print(_warn("dry-run：只检测，未写入任何文件（脱敏件 / 映射表 / 报告均未生成）"))
        return 0 if not result.failed else (1 if result.ok_count else 2)

    print()
    print(f"脱敏件目录：{outcome.dirs.masked}")
    if outcome.vault_path:
        print(f"映射表（加密）：{outcome.vault_path}  权限 600")
        print(f"密钥文件：{outcome.vault_path.parent / KEY_FILENAME}  权限 600 —— 与映射表一起备份，丢失即不可恢复")
    if outcome.report_path:
        print(f"脱敏报告（无真名，可交付）：{outcome.report_path}")
    if outcome.review_path:
        print(f"复核清单（含真名，勿上传）：{outcome.review_path}")
    txt_count = sum(
        1 for fr in result.files for f in fr.findings if f.code == "pdf.text_companion"
    )
    if txt_count:
        print(f"PDF 伴生文本：已为 {txt_count} 个 PDF 导出同名 .txt（下游优先读它，顺序才正确）")
    print()
    print(_warn("vault/ 目录含真名与密钥，不要上传；只需上传脱敏件与 脱敏报告.md"))
    if outcome.notes:
        print()
        for note in outcome.notes:
            print(f"  {_warn(note)}")
    print()
    print("下一步：把「脱敏件目录」交给法律 AI 工作台；分析完成后用 restore 回填。")

    if not result.files:
        return 2
    if result.ok_count == 0:
        return 2
    if result.failed:
        return 1
    return 0


def _detector_names(settings: Settings) -> list[str]:
    """尽力列出已装配检测器名称（装配失败时返回空表，不抛异常）。"""
    try:
        from .detectors import build_detectors

        return [getattr(d, "name", type(d).__name__) for d in build_detectors(settings)]
    except Exception:
        return []


# --------------------------------------------------------------------- restore


def cmd_restore(args: argparse.Namespace) -> int:
    _print_header("restore 回填")
    source = pathlib.Path(args.file).expanduser().resolve()
    vault_path = pathlib.Path(args.vault).expanduser().resolve()
    if args.out:
        target = pathlib.Path(args.out).expanduser().resolve()
    else:
        target = source.with_name(f"{source.stem}_已回填{source.suffix}")
    try:
        vault = Vault.load(vault_path)
    except VaultError as exc:
        _print_error(str(exc))
        return 2

    print(f"映射表：{vault_path}")
    print(f"摘要：{vault.describe()}")
    ambiguous = vault.ambiguous_placeholders()
    if ambiguous:
        print(f"{_warn(f'{len(ambiguous)} 个代号撞号，相关位置将跳过回填：' + '、'.join(sorted(ambiguous)[:5]))}")
    print(f"输出：{target}")
    print()

    stats: dict[str, int] = {}
    warnings: list[str] = []
    try:
        written = restore_file(source, target, vault, stats=stats, warnings=warnings)
    except (RestoreError, VaultError) as exc:
        _print_error(str(exc))
        return 2
    except Exception as exc:
        _print_error(f"回填失败：{exc}")
        return 2

    replaced = stats.get("replaced", 0)
    skipped = stats.get("skipped_ambiguous", 0)
    resolved = stats.get("resolved_ambiguous", 0)
    print(_ok(f"已回填 {replaced} 处"))
    if resolved:
        print(f"  其中 {resolved} 处是撞号代号，按「身份证号 / 联系电话」等上下文线索判定")
    if skipped:
        print(_warn(f"跳过 {skipped} 处撞号代号（同代号对应多个原文且无线索），请人工核对"))
    for item in warnings:
        print(_warn(item))
    print(f"输出文件：{written}")
    print()
    if replaced == 0:
        print(_bad("没有回填任何内容：请确认映射表对应本案、或该文件确实已含代号。"))
        return 1
    print("提醒：回填件含真名，不要上传到联网环境；确认分析已完成后再打开。")
    return 0


# --------------------------------------------------------------------- audit


def cmd_audit(args: argparse.Namespace) -> int:
    _print_header("audit 残留复扫")
    root = pathlib.Path(args.path).expanduser().resolve()
    if not root.exists():
        _print_error(f"路径不存在：{root}")
        return 2
    try:
        settings = _build_settings(args)
    except Exception as exc:
        _print_error(f"配置加载失败：{exc}")
        return 2

    ignore_values: list[str] = []
    if args.vault:
        try:
            vault = Vault.load(pathlib.Path(args.vault).expanduser().resolve())
            print(f"映射表：{vault.describe()}")
            ignore_values = [entry.placeholder for entry in vault.entries]
        except VaultError as exc:
            _print_error(str(exc))
            return 2
    else:
        print("映射表：未提供。注意：无法排除自编案号/代号，可能把本工具自己的产物误报为残留。")
    print()

    from .formats import handler_for, supported_suffixes

    # 映射表目录与报告本身是机密配套文件，内含真名，不能当作「泄露」来扫
    vault_dir = pathlib.Path(args.vault).expanduser().resolve().parent if args.vault else None

    def _is_confidential(path: pathlib.Path) -> bool:
        if vault_dir is not None and (path == vault_dir or path.is_relative_to(vault_dir)):
            return True
        return path.name.startswith("脱敏报告") or path.name.startswith("审计报告")

    targets: list[pathlib.Path] = []
    if root.is_file():
        targets = [root] if not _is_confidential(root) else []
    else:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or any(part.startswith(".") for part in path.parts):
                continue
            if _is_confidential(path):
                continue
            targets.append(path)

    scan_suffixes = set(supported_suffixes()) | set(TEXT_SUFFIXES) | {".pdf"}
    findings_by_file: dict[pathlib.Path, list] = {}
    skipped: list[pathlib.Path] = []
    for path in targets:
        if path.suffix.lower() not in scan_suffixes:
            skipped.append(path)
            continue
        findings = audit_file(path, settings.rules, handler_for(path), ignore_values=ignore_values)
        if findings:
            findings_by_file[path] = findings

    total_critical = sum(1 for fs in findings_by_file.values() for f in fs if f.level == "critical")
    total_warn = sum(1 for fs in findings_by_file.values() for f in fs if f.level == "warn")
    hit_files = len(findings_by_file)

    if hit_files:
        for path, findings in sorted(findings_by_file.items()):
            print(f"\n{path}")
            for finding in sorted(findings, key=lambda f: (f.level != "critical", f.location)):
                tag = "critical" if finding.level == "critical" else "warn"
                print(f"  [{tag}] {finding.location}  {finding.message}")
    else:
        print(_ok("未发现残留命中"))

    print()
    print(_line())
    print(f"扫描文件：{len(targets)}，命中文件：{hit_files}，跳过（不支持）：{len(skipped)}")
    print(f"命中：critical {total_critical}，warn {total_warn}")
    if skipped:
        preview = "、".join(p.name for p in skipped[:5])
        print(f"  {_warn(f'以下文件不支持扫描，需人工检查：{preview}' + ('…' if len(skipped) > 5 else ''))}")
    if total_critical:
        print(_bad("存在 critical 残留：该文件不得交付外部工作台，请修复后复扫。"))
        return 1
    if total_warn:
        print(_warn("存在 warn 命中：多为未脱敏的主体名称，请人工确认。"))
        return 1

    if args.report:
        report = pathlib.Path(args.report).expanduser().resolve()
        lines = ["# 残留复扫报告", "", f"- 扫描目标：{root}", f"- 文件数：{len(targets)}", f"- 命中文件：{hit_files}", ""]
        if not findings_by_file:
            lines.append("未发现残留命中。")
        for path, findings in sorted(findings_by_file.items()):
            lines.append(f"## {path.name}")
            for finding in findings:
                lines.append(f"- [{finding.level}] {finding.location} {finding.message}")
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"报告：{report}")
    return 0


# --------------------------------------------------------------------- doctor


def _dep_version(module: str, package: str) -> tuple[str, str | None]:
    """返回 (状态, 版本号)。状态: ok / missing / error。"""
    if importlib.util.find_spec(module) is None:
        return "missing", None
    try:
        return "ok", importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "ok", "（版本未知）"


def _cjk_fonts() -> list[str]:
    """查找可用的中文字体（PDF 回填与未来 OCR 渲染用）。"""
    roots = [pathlib.Path("/usr/share/fonts"), pathlib.Path("/usr/local/share/fonts"), pathlib.Path.home() / ".fonts"]
    hits: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for pattern in ("*CJK*", "*cjk*", "*wqy*", "*NotoSansSC*", "*simsun*", "*SimSun*"):
            for font in root.rglob(pattern):
                if font.is_file():
                    hits.append(str(font))
                    if len(hits) >= 5:
                        return hits
    return hits


def cmd_doctor(args: argparse.Namespace) -> int:
    _print_header("doctor 环境自检")
    problems: list[str] = []
    print(f"Python：{platform.python_version()}（{sys.executable}）")
    if sys.version_info < (3, 10):
        problems.append("Python 版本低于 3.10，代码使用了 `X | None` 类型语法")
        print(f"  {_bad('需要 3.10 及以上')}")
    else:
        print(f"  {_ok('版本满足要求（>=3.10）')}")
    print(f"项目根目录：{project_home()}")
    print(f"配置文件目录：{config_dir()}")
    print()

    print("依赖：")
    for module, package in _DEPENDENCIES:
        state, version = _dep_version(module, package)
        if state == "missing":
            problems.append(f"缺少依赖 {package}")
            print(f"  {_bad(f'{package}: 未安装')}")
        else:
            print(f"  {_ok(f'{package}: {version}')}")
    print()

    print("OCR：")
    tesseract = shutil.which("tesseract")
    pytesseract_ok = importlib.util.find_spec("pytesseract") is not None
    if tesseract:
        print(f"  {_ok(f'tesseract 可执行文件：{tesseract}')}")
    else:
        print(f"  {_warn('未找到 tesseract：扫描件/图片型 PDF 无法自动脱敏，会进入「无法处理」清单')}")
    print(f"  {'✓' if pytesseract_ok else '⚠'} pytesseract 模块：{'已安装' if pytesseract_ok else '未安装'}")
    print()

    print("中文字体（PDF 回填 / 渲染）：")
    fonts = _cjk_fonts()
    if fonts:
        for font in fonts:
            print(f"  {_ok(font)}")
    else:
        print(f"  {_warn('未找到中文字体；PDF 回填将退化为仅擦除。PyMuPDF 内置 china-s 字体通常仍可用。')}")
    print()

    print("词池余量（config/lexicon.yaml）：")
    try:
        settings = _build_settings(args)
        for name in ("person", "place", "org"):
            size = len(settings.lexicon.pools.get(name) or [])
            mark = "✓" if size >= 10 else "⚠"
            print(f"  {mark} {name}: {size} 个（单个案件最多 {size} 个不同实体）")
        print(f"  代号框：{settings.lexicon.open}…{settings.lexicon.close}，掩码：{settings.lexicon.mask_token}")
    except Exception as exc:
        problems.append(f"词库加载失败：{exc}")
        print(f"  {_bad(f'词库加载失败：{exc}')}")
        settings = None
    print()

    print("支持格式（formats 装配）：")
    try:
        from .formats import all_handlers, supported_suffixes

        handlers = all_handlers()
        if handlers:
            for handler in handlers:
                handler_name = getattr(handler, "name", type(handler).__name__)
                suffix_text = ", ".join(handler.suffixes)
                print(f"  {_ok(f'{handler_name}: {suffix_text}')}")
        else:
            problems.append("formats/ 下没有任何 HANDLER，mask 无法处理文件")
            print(f"  {_bad('尚无已注册格式处理器')}")
        print(f"  合计：{', '.join(supported_suffixes()) or '（无）'}")
    except Exception as exc:
        problems.append(f"格式装配失败：{exc}")
        print(f"  {_bad(f'格式装配失败：{exc}')}")
    print()

    print("检测器（detectors 装配）：")
    names = _detector_names(settings) if settings else []
    if names:
        for name in names:
            print(f"  {_ok(name)}")
    else:
        problems.append("detectors/ 下没有检测器，mask 会拒绝运行")
        print(f"  {_bad('尚无已装配检测器（mask 会拒绝运行，避免产出假脱敏件）')}")
    print()

    print("输出与密钥：")
    if settings:
        output_cfg = settings.rules.output or {}
        masked_root = project_home() / str(output_cfg.get("masked_dir", "output"))
        vault_root = project_home() / str(output_cfg.get("vault_dir", "vault"))
        print(f"  脱敏件默认落点：{masked_root}/<案件名>/")
        print(f"  映射表目录：{vault_root}/<案件名>/")
        print(f"  {'✓' if os.access(project_home(), os.W_OK) else '✗'} 项目目录可写：{project_home()}")
    key = project_home() / "vault" / KEY_FILENAME
    print(f"  {'✓' if key.exists() else '⚠'} 已有密钥：{key if key.exists() else '（首次 mask 时生成）'}")
    print()

    print("入口：")
    package_dir = pathlib.Path(__file__).resolve().parent
    if "site-packages" in package_dir.parts or "dist-packages" in package_dir.parts:
        print(f"  {_ok('已安装，可直接运行 python -m masking.cli')}")
    else:
        print(f"  {_ok('当前以源码目录加载（未安装），命令前需加 PYTHONPATH=src')}")
        print("      例：PYTHONPATH=src .venv/bin/python -m masking.cli mask <路径>")
    print()

    print(_line())
    if problems:
        print(_bad(f"发现 {len(problems)} 个问题，当前环境还不能完整运行 mask："))
        for problem in problems:
            print(f"  - {problem}")
        print("\n提示：补齐依赖或检测器/格式处理器后重新运行 doctor。")
        return 1
    print(_ok(f"环境就绪，工具版本 {__version__}"))
    return 0


# --------------------------------------------------------------------- 解析器


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="masking.cli",
        description="本地脱敏工具：把 Word/Excel/PDF 里的敏感实体替换成带框代号，分析后可回填。",
        epilog="示例：PYTHONPATH=src .venv/bin/python -m masking.cli mask samples/起诉状.docx",
    )
    parser.add_argument("--version", action="version", version=f"本地脱敏工具 {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    mask = sub.add_parser("mask", help="脱敏单文件或整个案件文件夹")
    mask.add_argument("path", help="待脱敏文件或案件文件夹")
    mask.add_argument("--out", help="输出目录：自包含交付包（脱敏件 + vault/ + 脱敏报告.md）")
    mask.add_argument("--lexicon", help="词库 YAML 路径（默认 config/lexicon.yaml）")
    mask.add_argument("--rules", help="规则 YAML 路径（默认 config/rules.yaml）")
    mask.add_argument("--dry-run", action="store_true", help="只检测不写文件，不生成脱敏件/映射表/报告")
    mask.add_argument("--report", dest="report", action="store_true", default=True, help="生成审计报告（默认开启）")
    mask.add_argument("--no-report", dest="report", action="store_false", help="不生成审计报告")
    mask.add_argument(
        "--vault",
        help="续跑：传入已有加密映射表（vault/案件.json），复用其中的代号，"
        "只给新实体分配新词。多次脱密时用它，否则会产生第二套映射让旧表失效。",
    )
    mask.add_argument(
        "--terms",
        help="手动补词：逗号 / 顿号 / 换行分隔的必脱词，逐字命中即脱，优先级最高。"
        "用于「阿某古某」这类规则抓不到、但你能认出来的词。",
    )
    mask.set_defaults(func=cmd_mask)

    restore = sub.add_parser("restore", help="把脱敏件里的代号回填成真名")
    restore.add_argument("file", help="待回填的脱敏文件")
    restore.add_argument("--vault", required=True, help="加密映射表路径（输出包 vault/<案件>.json）")
    restore.add_argument("--out", help="回填输出路径（默认 <原名>_已回填.<后缀>）")
    restore.set_defaults(func=cmd_restore)

    audit = sub.add_parser("audit", help="对已脱敏目录/文件做残留复扫")
    audit.add_argument("path", help="待扫描文件或目录")
    audit.add_argument("--vault", help="加密映射表路径（可选，用于对照代号）")
    audit.add_argument("--lexicon", help="词库 YAML 路径（默认 config/lexicon.yaml）")
    audit.add_argument("--rules", help="规则 YAML 路径（默认 config/rules.yaml）")
    audit.add_argument("--report", help="把复扫结果另存为 Markdown 报告")
    audit.set_defaults(func=cmd_audit)

    doctor = sub.add_parser("doctor", help="环境自检")
    doctor.add_argument("--lexicon", help="词库 YAML 路径（默认 config/lexicon.yaml）")
    doctor.add_argument("--rules", help="规则 YAML 路径（默认 config/rules.yaml）")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已中断。未完成的文件不会写入输出目录。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

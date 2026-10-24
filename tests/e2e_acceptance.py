#!/usr/bin/env python3
"""端到端验收：用 samples/case-001 跑完整链路，逐项断言并给出可读结论。

这不是单元测试，是「交付前判卷」——它回答一个问题：
把这套东西交给法院实习生，她拿真实的四份材料去跑，会不会漏。

用法：
    .venv/bin/python tests/e2e_acceptance.py            # 跑完整链路
    .venv/bin/python tests/e2e_acceptance.py --keep     # 保留中间产物便于人工检查

退出码：0 全通过；1 有断言失败。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SAMPLES = ROOT / "samples" / "case-001"

#: 这些字符串脱敏后必须一个都找不到
MUST_VANISH = [
    "张三",
    "李四",
    "王五",
    "赵六",
    "钱七",
    "孙八",
    "周九",
    "33010619900101123X",
    "91330100MA2K7X8Y9R",
    "6222021234567890003",
    "13800138000",
    "13900139000",
    "13700137000",
    "杭州某某科技有限公司",
    "浙江某某律师事务所",
    "(2024)甲法民初1234号",
]

#: 脱敏后应当出现的东西（证明替换真的发生了，而不是文件被清空）
MUST_APPEAR = ["【", "】"]

#: 允许保留的（法律分析必需）
SHOULD_SURVIVE = ["本院认为", "民法典", "二〇二四年"]

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((ok, label))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)


def collect_text(path: pathlib.Path) -> str:
    """尽力把一份文件的可读文本全抽出来，用于残留检查。"""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        from docx import Document

        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    parts.append(cell.text)
        for section in doc.sections:
            parts.extend(p.text for p in section.header.paragraphs)
            parts.extend(p.text for p in section.footer.paragraphs)
        return "\n".join(parts)

    if suffix == ".xlsx":
        from openpyxl import load_workbook

        wb = load_workbook(str(path))
        parts = []
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    if cell.value is not None:
                        parts.append(str(cell.value))
                    if cell.comment:
                        parts.append(cell.comment.text)
        return "\n".join(parts)

    if suffix == ".pdf":
        import pymupdf

        doc = pymupdf.open(str(path))
        try:
            return "\n".join(page.get_text() for page in doc)
        finally:
            doc.close()

    if suffix == ".txt":
        return path.read_text(encoding="utf-8", errors="replace")

    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留中间产物")
    args = parser.parse_args()

    if not SAMPLES.exists():
        print(f"缺少样本目录 {SAMPLES}，先跑 samples/make_samples.py", file=sys.stderr)
        return 1

    from masking.config import Settings
    from masking.pipeline import mask_path

    settings = Settings.load()

    work = pathlib.Path(tempfile.mkdtemp(prefix="mask-e2e-"))
    case_dir = work / "case-input"
    shutil.copytree(SAMPLES, case_dir)
    out_dir = work / "case-out"

    print(f"\n{'='*66}\n端到端验收：{case_dir}\n{'='*66}")

    try:
        run = mask_path(str(case_dir), out_dir=str(out_dir), settings=settings)
    except Exception:
        traceback.print_exc()
        print("\n[FAIL] 流水线整体抛出异常，验收中止")
        return 1

    print(f"\n流水线返回：{len(run.files)} 个文件，成功 {run.ok_count} 个\n")

    # ---------------------------------------------------------------- 逐文件

    masked_files = sorted(p for p in out_dir.rglob("*") if p.is_file()
                          and p.suffix.lower() in {".docx", ".xlsx", ".pdf", ".txt"})
    check(bool(masked_files), "产出脱敏文件", f"{len(masked_files)} 个")

    original_files = {p.name for p in case_dir.iterdir() if p.is_file()}
    produced_names = {p.name for p in masked_files}
    check(original_files.issubset(produced_names),
          "每个原件都有对应的脱敏件",
          f"缺 {sorted(original_files - produced_names)}" if not original_files.issubset(produced_names) else "")

    for f in masked_files:
        text = collect_text(f)

        missing = [s for s in MUST_VANISH if s in text]
        check(not missing, f"{f.name}：敏感串全部清除",
              f"仍残留 {missing}" if missing else "")

        appeared = [s for s in MUST_APPEAR if s in text]
        check(bool(appeared), f"{f.name}：出现脱敏代号",
              "" if appeared else "没找到【】，可能替换没写回")

        # 只对「原件里本来就含这些词」的文件做保留检查，否则扫描件样本会被误判
        src = case_dir / f.name
        if src.exists():
            src_text = collect_text(src)
            expected = [s for s in SHOULD_SURVIVE if s in src_text]
            if expected:
                survived = [s for s in expected if s in text]
                lost = sorted(set(expected) - set(survived))
                check(not lost, f"{f.name}：法律分析要素保留",
                      f"丢了 {lost}" if lost else f"保留 {survived}")

    # ---------------------------------------------------------------- 残留复扫

    print("\n-- 残留复扫（用 rules.yaml 的 residual_patterns）--")
    residual_hits: list[str] = []
    for label, patterns in settings.rules.residual.items():
        for pat in patterns:
            try:
                rx = re.compile(pat)
            except re.error:
                continue
            for f in masked_files:
                text = collect_text(f)
                for m in rx.finditer(text):
                    residual_hits.append(f"{f.name} · {label} · {m.group(0)[:40]}")
    check(not residual_hits, "残留扫描零命中",
          f"{len(residual_hits)} 处：{residual_hits[:5]}" if residual_hits else "")

    # ---------------------------------------------------------------- 映射表

    print("\n-- 映射表 --")
    vaults = list(out_dir.rglob("*.json")) + list((out_dir.parent).rglob("vault/*"))
    check(bool(vaults), "映射表已落盘", f"{[v.name for v in vaults][:5]}")
    key_file = next((p for p in out_dir.rglob(".key")), None)
    if key_file:
        mode = oct(key_file.stat().st_mode)[-3:]
        check(mode == "600", "密钥文件权限 600", f"实际 {mode}")

    # ---------------------------------------------------------------- 报告

    reports = list(out_dir.rglob("*.md"))
    check(bool(reports), "复核报告已生成", f"{[r.name for r in reports][:3]}")

    # ---------------------------------------------------------------- 原件

    print("\n-- 原件完整性 --")
    import hashlib

    changed = []
    for p in case_dir.iterdir():
        if not p.is_file():
            continue
        before = hashlib.sha256(p.read_bytes()).hexdigest()
        src = SAMPLES / p.name
        if src.exists():
            after = hashlib.sha256(src.read_bytes()).hexdigest()
            if before != after:
                changed.append(p.name)
    check(not changed, "原件未被修改", f"被改动：{changed}" if changed else "")

    # ---------------------------------------------------------------- 汇总

    passed = sum(1 for ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'='*66}")
    print(f"验收结果：{passed}/{total} 通过")
    if passed < total:
        print("未通过项：")
        for ok, label in RESULTS:
            if not ok:
                print(f"  - {label}")
    print(f"{'='*66}\n")

    if args.keep:
        print(f"中间产物保留在：{work}")
    else:
        shutil.rmtree(work, ignore_errors=True)

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())

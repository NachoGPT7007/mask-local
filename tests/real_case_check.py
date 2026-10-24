#!/usr/bin/env python3
"""真实案例回归检查。

与 e2e_acceptance.py 的区别：那个用虚构样本，这个用一份真实的北大法宝案例
（梁某诉某人工智能公司网络侵权责任纠纷案，34 页），专查真实材料上暴露的问题：

1. 地名残留（云南/昆明/杭州/浙江等无后缀省名、市名）
2. 误脱敏（「纠结校区」被当成「XX区」地名）
3. 专门法院（互联网法院/知识产权法院…）
4. 案号一致性（同一案号全篇只能有一个代号）
5. 书签/大纲泄露
6. 二次 OCR 复核的 critical 残留

用法：
    .venv/bin/python tests/real_case_check.py \
        --src /tmp/real-case/梁某诉某人工智能公司网络侵权责任纠纷案.pdf \
        --out /tmp/real-out
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

#: 这份材料里真实出现、必须消失的词
MUST_VANISH = [
    "云南", "昆明", "昭通", "曲靖", "呈贡", "杨林",
    "杭州", "浙江",
    "梁某", "肖芄", "赖粤旭",
    "云南国防工业职业技术学院", "云南开放大学",
    "浙0192", "18143",
]

#: 应当保留的法律要素
MUST_SURVIVE = ["本院认为", "民法典", "判决如下", "网络侵权责任"]

#: 误脱敏模式：正常词被替换成「【XX】」+ 后缀
FALSE_POSITIVE_PATTERNS = [
    (r"【[^】]+】区(?:即使|位|别|域)", "「XX区」误判：校区/区位被当地名"),
    (r"反复\s*【[^】]+】区", "「纠结校区」被误判为地名"),
    (r"直\s*【[^】]+】区\s*位", "「推荐区位」被误判为地名"),
]

results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


def pdf_text(path: pathlib.Path) -> str:
    import pymupdf

    doc = pymupdf.open(str(path))
    try:
        return "\n".join(p.get_text() for p in doc)
    finally:
        doc.close()


def masked_text(out_dir: pathlib.Path) -> tuple[str, pathlib.Path | None]:
    txts = sorted(out_dir.glob("*.txt"))
    if txts:
        return txts[0].read_text(encoding="utf-8", errors="replace"), txts[0]
    pdfs = sorted(out_dir.glob("*.pdf"))
    if pdfs:
        return pdf_text(pdfs[0]), pdfs[0]
    return "", None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="原件 PDF")
    ap.add_argument("--out", required=True, help="脱敏输出目录")
    args = ap.parse_args()

    src = pathlib.Path(args.src)
    out_dir = pathlib.Path(args.out)
    if not src.exists():
        print(f"原件不存在: {src}", file=sys.stderr)
        return 1

    orig = pdf_text(src)
    masked, masked_path = masked_text(out_dir)

    print(f"\n{'='*66}\n真实案例回归\n  原件: {src}\n  脱敏: {masked_path}\n{'='*66}\n")

    # 1. 地名与专名残留
    print("-- 必须消失的词 --")
    for w in MUST_VANISH:
        if w not in orig:
            continue
        check(w not in masked, f"「{w}」已清除",
              f"仍残留 {masked.count(w)} 次" if w in masked else "")

    # 2. 法律要素保留
    print("\n-- 应保留的法律要素 --")
    for w in MUST_SURVIVE:
        if w not in orig:
            continue
        check(w in masked, f"「{w}」保留")

    # 3. 误脱敏
    print("\n-- 误脱敏检查 --")
    for pat, label in FALSE_POSITIVE_PATTERNS:
        hits = re.findall(pat, masked)
        check(not hits, label, f"命中 {hits[:3]}" if hits else "")

    # 4. 案号一致性
    print("\n-- 案号一致性 --")
    case_nos = set(re.findall(r"[\(（]\d{4}[\)）][\u4e00-\u9fa5]{1,4}\d{0,4}[\u4e00-\u9fa5]{1,3}\d+号", masked))
    check(len(case_nos) <= 1, "全篇只有一个案号代号",
          f"出现 {len(case_nos)} 个不同案号: {sorted(case_nos)[:4]}")

    # 5. 书签
    print("\n-- PDF 书签/大纲 --")
    pdfs = sorted(out_dir.glob("*.pdf"))
    if pdfs:
        import pymupdf

        doc = pymupdf.open(str(pdfs[0]))
        try:
            toc_titles = " ".join(str(t[1]) for t in doc.get_toc())
        finally:
            doc.close()
        leaked = [w for w in MUST_VANISH if w in toc_titles]
        check(not leaked, "书签不含当事人/地名", f"泄露 {leaked}" if leaked else "")

    # 6. critical 残留
    print("\n-- 审计报告 --")
    report = out_dir / "脱敏报告.md"
    if report.exists():
        text = report.read_text(encoding="utf-8")
        m = re.search(r"残留 critical \| (\d+)", text)
        n = int(m.group(1)) if m else -1
        check(n == 0, "critical 残留为 0", f"实际 {n}")

    passed = sum(1 for ok, _, _ in results if ok)
    total = len(results)
    print(f"\n{'='*66}\n结果：{passed}/{total} 通过")
    if passed < total:
        print("未通过：")
        for ok, label, detail in results:
            if not ok:
                print(f"  - {label} {detail}")
    print(f"{'='*66}\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""NER 复核：用中文 NER 模型对材料做独立扫描，找出规则检测漏掉的实体。

定位
----
**复核员，不是主力。** 它只产出清单，不自动替换任何文本。原因是 NER 是概率
模型，会把「权益」「安全」这类普通词误判成人名；自动替换会把误报直接写进
交付件，那比漏检更糟。

工作方式
--------
1. 对**原件**跑 NER，得到「模型认为这里有个实体」的清单；
2. 每条命中去脱敏件里查它的原文是否还在；
3. 按「规则已覆盖 / 规则漏检 / 疑似误报」三类输出。

为什么对原件跑而不是对脱敏件跑：
  脱敏件里原文已被换成【熊猫】这类代号，模型看到的是代号，认不出；
  只有对原件跑，才能得到「本该被脱掉的东西」的完整清单。

模型
----
默认 uer/roberta-base-finetuned-cluener2020-chinese（CLUENER 数据集）：
标签为人名、公司、政府机构、组织、地址、职位、场景等，正好对上法院材料。
首次运行会从 hf-mirror 下载约 400MB 权重，之后离线可用。

用法
----
    export HF_ENDPOINT=https://hf-mirror.com
    .venv/bin/python tools/ner_review.py \
        --src /tmp/real-case/xxx.pdf \
        --report /tmp/real-v8-out/脱敏报告.md \
        --masked-text /tmp/real-v8-out/xxx.txt \
        --out /tmp/ner-review.md
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from dataclasses import dataclass, field

#: CLUENER 的标签里，与脱敏相关的这几类。
#: position（职位）刻意排除：NER 会把「专家」「学生」「法官」「律师」标成 position，
#: 这些是角色不是身份，脱了反而破坏语义。
RELEVANT = {
    "name": "人名",
    "company": "公司",
    "government": "政府机构",
    "organization": "组织",
    "address": "地址",
    "scene": "场景",
}

#: 明显不是敏感实体的标签（书名、游戏、电影、职位），直接忽略
IGNORE = {"book", "game", "movie", "position"}

#: 通用的、脱了没有意义的机构/称谓。NER 会把它们标成 organization/government，
#: 但「人民法院」「公司」「学校」这类词不指向任何具体主体。
COMMON_TERMS = {
    "人民法院", "最高人民法院", "人民检察院", "法院", "检察院", "政府", "省纪委",
    "教育部", "国家发展和改革委员会", "经合组织", "谷歌", "北大法宝", "北大法",
    "公司", "股子公司", "子公司", "学校的招生办", "学校招生办", "派出所", "公安局",
    "中华人民共和", "中华人民共和国", "联合国", "世界贸易组织", "欧盟",
    "人工智能", "人工智", "工智能", "专家", "学生", "首席", "审员", "法官", "律师",
    "助理", "主校区", "训基地", "培训基地", "生公寓", "学生公寓", "安宁", "校区",
}

#: 代号：【熊猫】、【昆仑】市。NER 若把代号识别成实体，属正常现象，要排除
CODE_RE = re.compile(r"【[^】]*】")


@dataclass
class Hit:
    text: str
    label: str
    score: float
    chunk_index: int
    covered: bool = False
    residue: bool = False

    @property
    def label_zh(self) -> str:
        return RELEVANT.get(self.label, self.label)


@dataclass
class Report:
    hits: list[Hit] = field(default_factory=list)
    chunks: int = 0
    text_len: int = 0

    def by_status(self) -> dict[str, list[Hit]]:
        out: dict[str, list[Hit]] = {"covered": [], "missed": [], "suspect": []}
        for h in self.hits:
            if h.covered:
                out["covered"].append(h)
            elif self._looks_like_word(h.text):
                out["suspect"].append(h)
            else:
                out["missed"].append(h)
        return out

    @staticmethod
    def _looks_like_word(text: str) -> bool:
        """疑似误报的粗筛：法律文书里的高频普通词与通用机构名。"""
        if text in COMMON_TERMS:
            return True
        if len(text) <= 1:
            return True
        # 纯称谓/角色
        if text.endswith(("专家", "学生", "法官", "律师", "助理", "首席", "人员")):
            return True
        # 通用机构后缀但不带专名（如「公司」「法院」）
        if len(text) <= 3 and text.endswith(("公司", "法院", "学院", "学校", "医院", "银行")):
            return True
        return False


def read_pdf_text(path: pathlib.Path) -> str:
    import pymupdf

    doc = pymupdf.open(str(path))
    try:
        # sort=True 才是阅读顺序
        return "\n".join(page.get_text(sort=True) for page in doc)
    finally:
        doc.close()


def read_any_text(path: pathlib.Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return read_pdf_text(path)
    if suffix == ".docx":
        from docx import Document

        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    if suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace")
    raise SystemExit(f"不支持的输入格式: {suffix}")


def chunk_text(text: str, max_chars: int = 450) -> list[str]:
    """按行聚合成不超过 max_chars 的块。

    BERT 上限 512 token，中文大致 1 字 = 1 token，留出余量。
    按行切而不是硬切，避免把姓名/机构名从中间劈开。
    """
    chunks: list[str] = []
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > max_chars and buf:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return chunks


_PIPELINE_CACHE: dict[str, object] = {}


def load_pipeline(model_name: str):
    """加载模型并缓存。Web 服务里同一个进程要处理多次请求，
    每次重新加载 69 秒不可接受，所以按模型名缓存。
    """
    cached = _PIPELINE_CACHE.get(model_name)
    if cached is not None:
        return cached

    import threading

    from transformers import pipeline

    global _LOAD_LOCK
    try:
        _LOAD_LOCK
    except NameError:
        _LOAD_LOCK = threading.Lock()

    with _LOAD_LOCK:
        cached = _PIPELINE_CACHE.get(model_name)
        if cached is not None:
            return cached
        pipe = pipeline(
            "ner",
            model=model_name,
            aggregation_strategy="simple",
            device=-1,  # CPU
        )
        _PIPELINE_CACHE[model_name] = pipe
        return pipe


def run(model_name: str, src: pathlib.Path, masked_text: str,
        min_score: float = 0.6) -> Report:
    text = read_any_text(src)
    chunks = chunk_text(text)
    ner = load_pipeline(model_name)

    report = Report(chunks=len(chunks), text_len=len(text))
    seen: set[tuple[str, str]] = set()

    for idx, chunk in enumerate(chunks, 1):
        if not chunk.strip():
            continue
        try:
            ents = ner(chunk)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 第 {idx} 块推理失败: {exc}", file=sys.stderr)
            continue
        for ent in ents:
            label = str(ent.get("entity_group") or ent.get("entity") or "").lower()
            if label.startswith(("b-", "i-")):
                label = label[2:]
            if label in IGNORE or label not in RELEVANT:
                continue
            word = str(ent.get("word") or "").replace(" ", "").strip()
            score = float(ent.get("score") or 0.0)
            # 置信度阈值：实测 0.6 以下全是「考院」「人陪」「育厅」这类切片碎片，
            # 留着只会淹没真正的漏检。用户可用 --min-score 调整。
            if score < min_score:
                continue
            if not word or CODE_RE.search(word):
                continue
            if (word, label) in seen:
                continue
            seen.add((word, label))

            hit = Hit(text=word, label=label, score=score, chunk_index=idx)
            # 「规则已覆盖」= 原件里的这个实体，在脱敏件里已经找不到了
            hit.covered = word not in masked_text
            hit.residue = not hit.covered
            report.hits.append(hit)

    return report


def write_report(report: Report, out: pathlib.Path, src: pathlib.Path) -> None:
    groups = report.by_status()
    lines: list[str] = [
        "# NER 复核清单",
        "",
        "> 本清单由中文 NER 模型独立扫描产生，**只供人工判断，不自动修改任何文件**。",
        "> 模型是概率模型，误报与漏报都会存在；请把它当作「多一双眼睛」，不是「第二份答案」。",
        "",
        "## 概要",
        "",
        f"- 源文件：`{src}`",
        f"- 文本长度：{report.text_len} 字，切分为 {report.chunks} 块",
        f"- NER 命中实体：{len(report.hits)} 个（去重后）",
        f"- 规则已覆盖：{len(groups['covered'])}",
        f"- **规则可能漏检：{len(groups['missed'])}**",
        f"- 疑似误报（常见词）：{len(groups['suspect'])}",
        "",
        "## 一、规则可能漏检（重点看这里）",
        "",
    ]
    if not groups["missed"]:
        lines.append("无。NER 认得出来的实体，规则都已覆盖。")
    else:
        lines.append("| 实体 | 类型 | 置信度 | 所在块 |")
        lines.append("| --- | --- | ---: | ---: |")
        for h in sorted(groups["missed"], key=lambda x: -x.score):
            lines.append(f"| {h.text} | {h.label_zh} | {h.score:.2f} | {h.chunk_index} |")

    lines += ["", "## 二、疑似误报（NER 认成实体，但可能是普通词）", ""]
    if not groups["suspect"]:
        lines.append("无。")
    else:
        lines.append("| 实体 | NER 类型 | 置信度 |")
        lines.append("| --- | --- | ---: |")
        for h in sorted(groups["suspect"], key=lambda x: -x.score):
            lines.append(f"| {h.text} | {h.label_zh} | {h.score:.2f} |")

    lines += ["", "## 三、规则已覆盖（证明两套机制在重叠工作）", ""]
    lines.append(f"共 {len(groups['covered'])} 个，不再逐条列出。")
    lines += [
        "",
        "## 怎么用这份清单",
        "",
        "1. 先看「规则可能漏检」：如果里面的东西确实是真名/真机构，说明规则有缺口，",
        "   把它写进 `config/custom_terms.txt` 或对应的词池，重跑 `mask`。",
        "2. 再看「疑似误报」：如果模型把普通词认成实体，说明模型不可靠——不要因为它",
        "   报了就去脱，那会把正常语义改掉。",
        "3. 两栏都看完再做决定。清单本身不是结论。",
    ]

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="NER 复核：独立扫描，产出漏检清单")
    ap.add_argument("--src", required=True, help="原件（pdf/docx/txt）")
    ap.add_argument("--masked-text", required=True, help="脱敏件导出的纯文本（.txt）")
    ap.add_argument("--out", default="", help="清单输出路径，默认与 masked-text 同目录")
    ap.add_argument(
        "--model",
        default="uer/roberta-base-finetuned-cluener2020-chinese",
        help="HF 模型名",
    )
    ap.add_argument(
        "--min-score",
        type=float,
        default=0.6,
        help="置信度阈值，默认 0.6；调低会增加召回与噪音",
    )
    args = ap.parse_args()

    src = pathlib.Path(args.src)
    masked_text = pathlib.Path(args.masked_text).read_text(encoding="utf-8", errors="replace")
    out = pathlib.Path(args.out) if args.out else (
        pathlib.Path(args.masked_text).parent / "NER复核清单.md"
    )

    print(f"加载模型 {args.model} …")
    report = run(args.model, src, masked_text, min_score=args.min_score)
    write_report(report, out, src)

    groups = report.by_status()
    print(
        f"完成：命中 {len(report.hits)} 个（覆盖 {len(groups['covered'])} / "
        f"漏检 {len(groups['missed'])} / 疑似误报 {len(groups['suspect'])}）"
    )
    print(f"清单：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

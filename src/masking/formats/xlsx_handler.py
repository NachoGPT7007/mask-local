"""Excel（.xlsx / .xlsm）格式处理器。

为什么必须处理「隐藏行列」与「隐藏工作表」
--------------------------------------------
法院材料里最常见的藏法就是：把身份证号、手机号、内部备注放进隐藏列
（有时还配合隐藏 sheet）。隐藏只是视图属性，值仍在 ``sheetN.xml`` 里，
任何按「可见区域」遍历的工具都会跳过它。本模块的 extract 不区分可见性，
所有行、所有列、所有工作表一律遍历；audit 则单独把隐藏项列出来提醒。

为什么要处理 sheet 名、定义名称与文档属性
------------------------------------------
这三处都在单元格文本流之外：

* ``sheet.title``：证据清单常直接命名成「张三证据清单」，随文件到处走；
* ``definedNames``：工作簿级命名的范围或常量里可能写着当事人名字；
* ``docProps/core.xml`` 的 creator / lastModifiedBy / title，
  ``docProps/app.xml`` 的 Company / Manager：openpyxl 只暴露前者，
  后者要直接改 zip 里的 XML，否则「李四」会原样留在文件属性里。

为什么必须处理公式里的字符串常量
--------------------------------
``="张三"`` 这种公式在单元格里显示为张三，但值不是普通字符串而是公式
文本，直接按单元格值替换会把公式结构破坏。本模块把公式里的每个字符串
字面量单独取出检测，回写时只替换引号内的内容，公式结构保持不动。

实现取用 openpyxl（非 read_only、非 data_only）
-----------------------------------------------
``read_only=True`` 的单元格是只读代理，无法写回；``data_only=True`` 会
把公式替换成缓存值，保存时公式整体丢失——对脱敏工具来说这是不可接受
的数据破坏。因此统一用可写模式加载，公式按原样保留。
"""

from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from lxml import etree
from openpyxl import load_workbook

from ..entity import Edit, Finding, TextBlock

# --------------------------------------------------------------------- 常量

#: docProps/app.xml 的命名空间（Company / Manager 在这里）。
_APP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"

#: core.xml 中「保留并脱敏」的字段：标题与主题含案由信息，对分析有用。
_KEEP_PROP_FIELDS = ("title", "subject")

#: core.xml 中「直接清空」的字段（openpyxl 的 DocumentProperties 属性名）。
#: 这些是文档由谁、用什么工具、改过几稿的痕迹，脱敏后没有分析价值，
#: 清空比替换更彻底。
_CLEAR_PROP_FIELDS = (
    "creator",
    "lastModifiedBy",
    "description",
    "keywords",
    "category",
    "contentStatus",
    "identifier",
    "version",
    "revision",
)

#: app.xml 中要清空的字段（openpyxl 不暴露，只能直接改 XML）。
_APP_CLEAR_FIELDS = ("Company", "Manager")

#: 公式里的字符串字面量：双引号包裹，内部的双引号用 "" 转义。
_FORMULA_LITERAL_RE = re.compile(r'"((?:[^"]|"")*)"')


def _iter_cells(ws) -> Iterator:
    """遍历工作表中的单元格。

    ``iter_rows`` 按 max_row × max_column 铺网格，某些生成器会把维度写成
    整张表（1048576 行），那样会遍历上百万个空单元格。超过阈值时改为遍历
    实际存在的单元格（``_cells``），批注挂在空单元格上的情况也能覆盖。
    """
    rows = ws.max_row or 0
    cols = ws.max_column or 0
    if rows * cols <= 2_000_000:
        for row in ws.iter_rows():
            yield from row
        return
    cells = getattr(ws, "_cells", {})
    for cell in sorted(cells.values(), key=lambda c: (c.row, c.column)):
        yield cell


def _formula_literals(formula: str) -> list[re.Match[str]]:
    """公式里所有字符串字面量的匹配对象。"""
    return list(_FORMULA_LITERAL_RE.finditer(formula))


# --------------------------------------------------------------------- 定位


@dataclass(frozen=True)
class XlsxLocator:
    """xlsx 文本块的定位信息，由本模块自己解释。

    ``kind`` 取值：

    * ``cell``           —— ``sheet_index`` 表 ``coordinate`` 单元格的文本；
    * ``formula_const``  —— 同一单元格公式中第 ``const_index`` 个字符串常量；
    * ``comment_text``   —— 该单元格批注的正文；
    * ``comment_author`` —— 该单元格批注的作者；
    * ``sheet_title``    —— 工作表名本身；
    * ``defined_name``   —— 定义名称的名字；
    * ``defined_value``  —— 定义名称的引用/常量（``field`` 为原名称）；
    * ``prop``           —— docProps/core.xml 的 ``field`` 属性；
    * ``app_prop``       —— docProps/app.xml 的 ``field``（Company / Manager）。

    为什么用 ``sheet_index`` 而不是表名定位：apply 阶段工作表可能被改名，
    按索引取表不会因为改名而错位。
    """

    kind: str
    sheet_index: int = -1
    sheet_title: str = ""
    coordinate: str = ""
    const_index: int = -1
    field: str = ""


# --------------------------------------------------------------------- 处理器


class XlsxHandler:
    """Excel 工作簿的 extract / apply / audit 实现。"""

    name = "xlsx"
    suffixes: tuple[str, ...] = (".xlsx", ".xlsm")

    def __init__(self) -> None:
        self._diagnostics: dict[str, list[str]] = {}

    # ---------------------------------------------------------------- 诊断

    def _key(self, path: str) -> str:
        try:
            return str(Path(path).resolve())
        except OSError:
            return str(path)

    def _note(self, path: str, message: str) -> None:
        self._diagnostics.setdefault(self._key(path), []).append(message)

    # ------------------------------------------------------------ 打开工作簿

    def _load(self, path: str):
        """可写模式加载。.xlsm 必须 keep_vba，否则保存后宏丢失。"""
        keep_vba = path.lower().endswith(".xlsm")
        try:
            return load_workbook(path, data_only=False, keep_vba=keep_vba)
        except Exception:
            # 后缀是 .xlsm、包内却没有 vbaProject.bin 时 keep_vba 可能打不开。
            # 只有在确认没有宏的情况下才退回普通加载——有宏而打不开就应当报错，
            # 否则会悄悄丢掉 vbaProject.bin。
            if keep_vba and not self._has_vba(path):
                return load_workbook(path, data_only=False)
            raise

    @staticmethod
    def _has_vba(path: str) -> bool:
        try:
            with zipfile.ZipFile(path) as archive:
                return any(n.endswith("vbaProject.bin") for n in archive.namelist())
        except (zipfile.BadZipFile, OSError):
            return False

    # ------------------------------------------------------------- extract

    def extract(self, path: str) -> list[TextBlock]:
        """取出所有可检测文本块。

        覆盖：全部工作表（含隐藏 sheet）的全部单元格（含隐藏行列与合并
        单元格）、批注正文与作者、sheet 名、定义名称、文档属性、
        公式中的字符串常量。
        """
        blocks: list[TextBlock] = []

        try:
            wb = self._load(path)
        except Exception as exc:  # noqa: BLE001
            # 文件级失败必须显式抛错：静默返回空块会让用户误以为「没有敏感信息」
            raise RuntimeError(f"无法打开工作簿 {path}: {exc}") from exc

        try:
            for sheet_index, ws in enumerate(wb.worksheets):
                try:
                    self._extract_sheet(path, sheet_index, ws, blocks)
                except Exception as exc:  # noqa: BLE001 - 单表失败不中断整个工作簿
                    self._note(path, f"工作表「{ws.title}」提取失败：{exc}")

            self._extract_defined_names(path, wb, blocks)
            self._extract_properties(path, wb, blocks)
        finally:
            # 释放 openpyxl 持有的文件句柄（Windows 上不关会导致后续覆盖失败）
            try:
                wb.close()
            except Exception:  # noqa: BLE001
                pass

        return blocks

    def _extract_sheet(self, path: str, sheet_index: int, ws, blocks: list[TextBlock]) -> None:
        """单张工作表的单元格、批注与表名。"""
        title = ws.title
        if title:
            blocks.append(
                TextBlock(
                    text=title,
                    locator=XlsxLocator(
                        kind="sheet_title", sheet_index=sheet_index, sheet_title=title
                    ),
                    role="metadata",
                )
            )

        for cell in _iter_cells(ws):
            coordinate = cell.coordinate
            value = cell.value
            try:
                if isinstance(value, str) and value.startswith("="):
                    # 公式：逐个字面量单独成块，避免破坏公式结构
                    for const_index, match in enumerate(_formula_literals(value)):
                        inner = match.group(1)
                        if not inner:
                            continue
                        blocks.append(
                            TextBlock(
                                text=inner,
                                locator=XlsxLocator(
                                    kind="formula_const",
                                    sheet_index=sheet_index,
                                    sheet_title=title,
                                    coordinate=coordinate,
                                    const_index=const_index,
                                ),
                                role="cell",
                            )
                        )
                elif isinstance(value, str):
                    if value:
                        blocks.append(
                            TextBlock(
                                text=value,
                                locator=XlsxLocator(
                                    kind="cell",
                                    sheet_index=sheet_index,
                                    sheet_title=title,
                                    coordinate=coordinate,
                                ),
                                role="cell",
                            )
                        )
                elif isinstance(value, int) and not isinstance(value, bool):
                    # 手机号/身份证有时被存成数字。位数够长才纳入，避免把金额当号码
                    text = str(value)
                    if 11 <= len(text.lstrip("-")) <= 19:
                        blocks.append(
                            TextBlock(
                                text=text,
                                locator=XlsxLocator(
                                    kind="cell",
                                    sheet_index=sheet_index,
                                    sheet_title=title,
                                    coordinate=coordinate,
                                ),
                                role="cell",
                            )
                        )
            except Exception as exc:  # noqa: BLE001
                self._note(path, f"{title}!{coordinate} 提取失败：{exc}")

            comment = getattr(cell, "comment", None)
            if comment is not None:
                text = comment.text
                if not isinstance(text, str):
                    text = str(text)
                if text:
                    blocks.append(
                        TextBlock(
                            text=text,
                            locator=XlsxLocator(
                                kind="comment_text",
                                sheet_index=sheet_index,
                                sheet_title=title,
                                coordinate=coordinate,
                            ),
                            role="comment",
                        )
                    )
                author = comment.author
                if author and author.strip():
                    blocks.append(
                        TextBlock(
                            text=author,
                            locator=XlsxLocator(
                                kind="comment_author",
                                sheet_index=sheet_index,
                                sheet_title=title,
                                coordinate=coordinate,
                            ),
                            role="comment",
                        )
                    )

    def _extract_defined_names(self, path: str, wb, blocks: list[TextBlock]) -> None:
        """工作簿定义名称：名字与引用/常量都可能含敏感串。"""
        for name, defined in wb.defined_names.items():
            if name:
                blocks.append(
                    TextBlock(
                        text=name,
                        locator=XlsxLocator(kind="defined_name", field=name),
                        role="metadata",
                    )
                )
            value = getattr(defined, "attr_text", None) or getattr(defined, "value", "") or ""
            if value:
                blocks.append(
                    TextBlock(
                        text=value,
                        locator=XlsxLocator(kind="defined_value", field=name),
                        role="metadata",
                    )
                )

    def _extract_properties(self, path: str, wb, blocks: list[TextBlock]) -> None:
        """docProps/core.xml：只提取 title / subject。

        作者、最后修改人、公司、管理者在 apply 时直接清空，提取出来交给
        检测器只会白占一个代号——那个代号最终不会出现在输出文件里。
        """
        props = getattr(wb, "properties", None)
        if props is None:
            return
        for field in _KEEP_PROP_FIELDS:
            value = getattr(props, field, None)
            if isinstance(value, str) and value.strip():
                blocks.append(
                    TextBlock(
                        text=value,
                        locator=XlsxLocator(kind="prop", field=field),
                        role="metadata",
                    )
                )

    @staticmethod
    def _app_child(root: etree._Element, field: str) -> etree._Element | None:
        """在 app.xml 里找子元素，兼容命名空间缺失的极端文件。"""
        node = root.find(f"{{{_APP_NS}}}{field}")
        if node is not None:
            return node
        for child in root:
            if isinstance(child.tag, str) and etree.QName(child).localname == field:
                return child
        return None

    # --------------------------------------------------------------- apply

    def apply(self, path: str, out_path: str, edits: Sequence[Edit]) -> None:
        """按 edits 回写并保存到 out_path，绝不覆盖原文件。"""
        if Path(path).resolve() == Path(out_path).resolve():
            raise ValueError(f"out_path 不能与原文件相同：{path}")

        grouped: dict[str, list[Edit]] = {}
        for edit in edits:
            locator = edit.block.locator
            if isinstance(locator, XlsxLocator):
                grouped.setdefault(locator.kind, []).append(edit)

        out_parent = Path(out_path).parent
        if out_parent and not out_parent.exists():
            out_parent.mkdir(parents=True, exist_ok=True)

        # 先落到临时文件，再做 zip 级元数据清空。
        # 没有 edit 时也要清元数据：检测器漏掉的作者名/公司名如果原样
        # 复制出去，等于泄露照旧。但此时不重建工作簿，只改 core.xml 与
        # app.xml，图表、条件格式、宏等特性一字节不动。
        fd, tmp_name = tempfile.mkstemp(
            prefix=".mask-", suffix=Path(out_path).suffix or ".xlsx", dir=str(out_parent or ".")
        )
        Path(tmp_name).unlink(missing_ok=True)
        try:
            if not grouped:
                shutil.copyfile(path, tmp_name)
            else:
                try:
                    wb = self._load(path)
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(f"无法打开工作簿 {path}: {exc}") from exc
                try:
                    self._apply_cells(path, wb, grouped.get("cell", []))
                    self._apply_formulas(path, wb, grouped.get("formula_const", []))
                    self._apply_comments(path, wb, grouped.get("comment_text", []),
                                         grouped.get("comment_author", []))
                    self._apply_props(path, wb, grouped.get("prop", []))
                    self._apply_defined_values(path, wb, grouped.get("defined_value", []))
                    sheet_renames = self._apply_sheet_titles(
                        path, wb, grouped.get("sheet_title", [])
                    )
                    name_renames = self._apply_defined_names(
                        path, wb, grouped.get("defined_name", [])
                    )
                    self._fix_references(wb, sheet_renames, name_renames)
                    # 清空作者/公司等字段；title/subject 已由 _apply_props 替换
                    self._clear_workbook_properties(wb)
                finally:
                    try:
                        wb.close()
                    except Exception:  # noqa: BLE001
                        pass
                wb.save(tmp_name)

            self._patch_metadata_package(tmp_name, out_path)
        finally:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass

    def _sheet(self, wb, locator: XlsxLocator, path: str = ""):
        """按索引取表；越界时记录并返回 None。"""
        if 0 <= locator.sheet_index < len(wb.worksheets):
            return wb.worksheets[locator.sheet_index]
        if path:
            self._note(path, f"工作表索引 {locator.sheet_index} 越界，已跳过")
        return None

    def _apply_cells(self, path: str, wb, edits: Sequence[Edit]) -> None:
        """单元格文本替换。同一单元格的多个实体合并处理。"""
        buckets: dict[tuple[int, str], list[Edit]] = {}
        for edit in edits:
            locator = edit.block.locator
            buckets.setdefault((locator.sheet_index, locator.coordinate), []).append(edit)

        for (sheet_index, coordinate), items in buckets.items():
            locator = items[0].block.locator
            ws = self._sheet(wb, locator, path)
            if ws is None:
                continue
            try:
                cell = ws[coordinate]
                old = cell.value
                if old is None:
                    continue
                new = _splice(str(old), items)
                # 脱敏后一律写回文本：掩码不是数字，写成字符串才不会改变含义
                cell.value = new
            except Exception as exc:  # noqa: BLE001
                self._note(path, f"{locator.sheet_title}!{coordinate} 回写失败：{exc}")

    def _apply_formulas(self, path: str, wb, edits: Sequence[Edit]) -> None:
        """公式字符串常量替换：只改引号内的内容。"""
        buckets: dict[tuple[int, str], dict[int, list[Edit]]] = {}
        for edit in edits:
            locator = edit.block.locator
            cell_key = (locator.sheet_index, locator.coordinate)
            buckets.setdefault(cell_key, {}).setdefault(locator.const_index, []).append(edit)

        for (sheet_index, coordinate), by_index in buckets.items():
            sample = next(iter(by_index.values()))[0]
            locator = sample.block.locator
            ws = self._sheet(wb, locator, path)
            if ws is None:
                continue
            try:
                cell = ws[coordinate]
                formula = cell.value
                if not isinstance(formula, str):
                    continue
                for const_index in sorted(by_index):
                    matches = _formula_literals(formula)
                    if const_index >= len(matches):
                        self._note(path, f"{coordinate} 公式常量序号 {const_index} 越界")
                        continue
                    match = matches[const_index]
                    inner = _splice(match.group(1), by_index[const_index])
                    # 公式字符串里的双引号必须写成两个双引号
                    inner = inner.replace('"', '""')
                    formula = formula[: match.start(1)] + inner + formula[match.end(1):]
                cell.value = formula
            except Exception as exc:  # noqa: BLE001
                self._note(path, f"{locator.sheet_title}!{coordinate} 公式回写失败：{exc}")

    def _apply_comments(
        self, path: str, wb, text_edits: Sequence[Edit], author_edits: Sequence[Edit]
    ) -> None:
        """批注正文与作者名。

        同一批注里可能有多个实体（「张三与李四已核对」），必须按批注分组
        一次性替换：逐条替换会把第二条的偏移当成修改后文本的偏移，位置错乱。
        """
        for edits, attr in ((text_edits, "text"), (author_edits, "author")):
            buckets: dict[tuple[int, str], list[Edit]] = {}
            for edit in edits:
                locator = edit.block.locator
                buckets.setdefault(
                    (locator.sheet_index, locator.coordinate), []
                ).append(edit)

            for items in buckets.values():
                locator = items[0].block.locator
                ws = self._sheet(wb, locator, path)
                if ws is None:
                    continue
                try:
                    cell = ws[locator.coordinate]
                    comment = getattr(cell, "comment", None)
                    if comment is None:
                        continue
                    # 以提取时的块文本为基准重组，避免多次修改后的偏移错位
                    setattr(comment, attr, _splice(items[0].block.text, items))
                except Exception as exc:  # noqa: BLE001
                    self._note(
                        path,
                        f"{locator.sheet_title}!{locator.coordinate} 批注{attr}回写失败：{exc}",
                    )

    def _apply_props(self, path: str, wb, edits: Sequence[Edit]) -> None:
        """docProps/core.xml 属性。"""
        props = getattr(wb, "properties", None)
        if props is None:
            return
        buckets: dict[str, list[Edit]] = {}
        for edit in edits:
            buckets.setdefault(edit.block.locator.field, []).append(edit)
        for field, items in buckets.items():
            if field not in _KEEP_PROP_FIELDS:
                # 作者/公司等字段走清空逻辑，不在这里写回
                continue
            try:
                # 以提取时的块文本为基准：属性部件可能被外部工具改写过
                base = items[0].block.text
                setattr(props, field, _splice(base, items))
            except Exception as exc:  # noqa: BLE001
                self._note(path, f"文档属性 {field} 回写失败：{exc}")

    @staticmethod
    def _clear_workbook_properties(wb) -> None:
        """清空 core.xml 中除 title / subject 之外的敏感字段。

        清空而不是替换：这是「文档由谁建的」痕迹，脱敏后没有分析价值，
        替换成代号还会留下「作者=【长颈鹿】」这种结构性线索。
        """
        props = getattr(wb, "properties", None)
        if props is None:
            return
        for field in _CLEAR_PROP_FIELDS:
            if hasattr(props, field):
                try:
                    setattr(props, field, "")
                except Exception:  # noqa: BLE001 - 个别字段只读时跳过
                    pass

    def _apply_defined_values(self, path: str, wb, edits: Sequence[Edit]) -> None:
        """定义名称的引用/常量文本。"""
        buckets: dict[str, list[Edit]] = {}
        for edit in edits:
            buckets.setdefault(edit.block.locator.field, []).append(edit)
        for name, items in buckets.items():
            defined = wb.defined_names.get(name)
            if defined is None:
                self._note(path, f"找不到定义名称「{name}」")
                continue
            try:
                defined.attr_text = _splice(items[0].block.text, items)
            except Exception as exc:  # noqa: BLE001
                self._note(path, f"定义名称「{name}」回写失败：{exc}")

    def _apply_sheet_titles(self, path: str, wb, edits: Sequence[Edit]) -> dict[str, str]:
        """工作表改名，返回「旧名 → 新名」映射（供引用修复）。"""
        buckets: dict[int, list[Edit]] = {}
        for edit in edits:
            buckets.setdefault(edit.block.locator.sheet_index, []).append(edit)

        renames: dict[str, str] = {}
        for sheet_index, items in buckets.items():
            if not (0 <= sheet_index < len(wb.worksheets)):
                self._note(path, f"工作表索引 {sheet_index} 越界，改名跳过")
                continue
            ws = wb.worksheets[sheet_index]
            old = ws.title
            new = _splice(old, items)
            if new == old:
                continue
            try:
                ws.title = new
                renames[old] = ws.title
            except Exception as exc:  # noqa: BLE001
                self._note(path, f"工作表「{old}」改名失败（{new!r}）：{exc}")
        return renames

    def _apply_defined_names(self, path: str, wb, edits: Sequence[Edit]) -> dict[str, str]:
        """定义名称改名，返回「旧名 → 新名」映射。"""
        buckets: dict[str, list[Edit]] = {}
        for edit in edits:
            buckets.setdefault(edit.block.locator.field, []).append(edit)

        renames: dict[str, str] = {}
        for name, items in buckets.items():
            defined = wb.defined_names.get(name)
            if defined is None:
                self._note(path, f"找不到定义名称「{name}」")
                continue
            new = _splice(name, items)
            if new == name:
                continue
            renames[name] = new
            defined.name = new

        if renames:
            # DefinedNameDict 的 key 是名字，改名后必须重建映射
            rebuilt = {}
            for key, value in wb.defined_names.items():
                rebuilt[renames.get(key, key)] = value
            wb.defined_names = rebuilt
        return renames

    def _fix_references(
        self, wb, sheet_renames: dict[str, str], name_renames: dict[str, str]
    ) -> None:
        """改名后同步公式与定义名称里的引用，避免出现 #REF!。

        公式里引用工作表可能写成 ``'旧名'!A1``（名字含空格等特殊字符时），
        也可能写成 ``旧名!A1``；两种都按「原文替换」处理，先长后短，避免
        一个名字是另一个名字前缀时替换错位。
        """
        pairs = [
            (old, new)
            for old, new in {**sheet_renames, **name_renames}.items()
            if old and new and old != new
        ]
        if not pairs:
            return
        pairs.sort(key=lambda item: len(item[0]), reverse=True)

        def rewrite(text: str) -> str:
            for old, new in pairs:
                if old in text:
                    text = text.replace(old, new)
            return text

        for ws in wb.worksheets:
            for cell in _iter_cells(ws):
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    updated = rewrite(cell.value)
                    if updated != cell.value:
                        cell.value = updated
        for defined in wb.defined_names.values():
            value = getattr(defined, "attr_text", "") or ""
            if isinstance(value, str) and value:
                updated = rewrite(value)
                if updated != value:
                    defined.attr_text = updated

    def _patch_metadata_package(self, src_path: str, out_path: str) -> None:
        """在保存后的包上做「元数据清空」。

        为什么不能只靠 openpyxl：
        * Company / Manager 在 docProps/app.xml，openpyxl 的
          DocumentProperties 只对应 core.xml，保存时还会把 app.xml 重写成
          空 Company；
        * 无 edit 时我们直接复制原文件，根本没有经过 openpyxl。

        所以统一在 zip 层再清一遍：core.xml 清空作者、最后修改人等字段
        （title / subject 保留，已由替换步骤处理好），app.xml 清空
        Company / Manager。其它部件一字节不动，图表、条件格式、宏都保留。
        """
        with zipfile.ZipFile(src_path) as archive:
            entries = [(info, archive.read(info.filename)) for info in archive.infolist()]

        patched: dict[str, bytes] = {}
        for info, blob in entries:
            try:
                if info.filename == "docProps/core.xml":
                    root = etree.fromstring(blob)
                    for node in root:
                        if not isinstance(node.tag, str):
                            continue
                        if etree.QName(node).localname in _CLEAR_PROP_FIELDS:
                            node.text = ""
                    patched[info.filename] = etree.tostring(
                        root, xml_declaration=True, encoding="UTF-8", standalone=True
                    )
                elif info.filename == "docProps/app.xml":
                    root = etree.fromstring(blob)
                    for field in _APP_CLEAR_FIELDS:
                        node = self._app_child(root, field)
                        if node is not None:
                            node.text = ""
                    patched[info.filename] = etree.tostring(
                        root, xml_declaration=True, encoding="UTF-8", standalone=True
                    )
            except Exception as exc:  # noqa: BLE001 - 修补失败保留原部件并记录
                self._note(src_path, f"{info.filename} 元数据清空失败：{exc}")

        with zipfile.ZipFile(out_path, "w") as dst:
            for info, blob in entries:
                dst.writestr(info, patched.get(info.filename, blob))

    # --------------------------------------------------------------- audit

    def audit(self, path: str) -> list[Finding]:
        """报告该文件的泄露点：隐藏行列、隐藏表、批注、属性、宏、嵌入对象。"""
        findings: list[Finding] = []
        lowered = path.lower()
        names: list[str] = []
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
        except (zipfile.BadZipFile, OSError) as exc:
            return [
                Finding(
                    level="critical",
                    code="xlsx.unreadable",
                    message=f"无法作为 zip 包读取：{exc}",
                    location=path,
                )
            ]

        # ---- 宏、嵌入对象、图片 -------------------------------------
        if lowered.endswith(".xlsm") or any(n.endswith("vbaProject.bin") for n in names):
            findings.append(
                Finding(
                    level="critical",
                    code="xlsx.macro",
                    message="文件包含 VBA 宏（.xlsm / vbaProject.bin）。宏可以读写单元格"
                    "并外传内容，脱敏后应确认宏是否仍需保留。",
                    location=path,
                )
            )
        embedded = [n for n in names if n.startswith("xl/embeddings/")]
        if embedded:
            findings.append(
                Finding(
                    level="critical",
                    code="xlsx.embedded_object",
                    message=f"含 {len(embedded)} 个嵌入对象（{', '.join(embedded[:3])}）。"
                    "嵌入文档内部保留独立原文，单元格替换覆盖不到。",
                    location=path,
                )
            )
        media = [n for n in names if n.startswith("xl/media/")]
        if media:
            findings.append(
                Finding(
                    level="info",
                    code="xlsx.media",
                    message=f"含 {len(media)} 个图片/媒体文件，图片内文字不在文本层，需人工复核。",
                    location=path,
                )
            )

        try:
            wb = self._load(path)
        except Exception as exc:  # noqa: BLE001
            findings.append(
                Finding(
                    level="critical",
                    code="xlsx.unreadable",
                    message=f"openpyxl 无法打开：{exc}",
                    location=path,
                )
            )
            return findings

        try:
            # ---- 隐藏工作表 -----------------------------------------
            hidden_sheets = [ws.title for ws in wb.worksheets if ws.sheet_state != "visible"]
            if hidden_sheets:
                findings.append(
                    Finding(
                        level="warn",
                        code="xlsx.hidden_sheet",
                        message=f"含 {len(hidden_sheets)} 张隐藏工作表："
                        f"{'、'.join(hidden_sheets[:5])}。隐藏表里的数据仍然完整存在。",
                        location=path,
                    )
                )

            # ---- 隐藏行列 -------------------------------------------
            hidden_rows: list[str] = []
            hidden_cols: list[str] = []
            for ws in wb.worksheets:
                for index, dim in getattr(ws, "row_dimensions", {}).items():
                    if getattr(dim, "hidden", False):
                        hidden_rows.append(f"{ws.title}!{index}行")
                for letter, dim in getattr(ws, "column_dimensions", {}).items():
                    if getattr(dim, "hidden", False):
                        hidden_cols.append(f"{ws.title}!{letter}列")
            if hidden_rows or hidden_cols:
                detail = "、".join((hidden_rows + hidden_cols)[:8])
                findings.append(
                    Finding(
                        level="warn",
                        code="xlsx.hidden_rows_cols",
                        message=f"含隐藏行 {len(hidden_rows)} 个、隐藏列 {len(hidden_cols)} 个"
                        f"（{detail}）。隐藏列常被用来放身份证号、手机号，"
                        "按可见区域遍历的工具会漏掉。",
                        location=path,
                    )
                )

            # ---- 批注、属性、定义名称 -------------------------------
            comment_total = 0
            authors: set[str] = set()
            formula_cells = 0
            for ws in wb.worksheets:
                for cell in _iter_cells(ws):
                    comment = getattr(cell, "comment", None)
                    if comment is not None:
                        comment_total += 1
                        if comment.author:
                            authors.add(comment.author)
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        formula_cells += 1
            if comment_total:
                findings.append(
                    Finding(
                        level="warn",
                        code="comments_present",
                        message=f"本文件含 {comment_total} 条批注，已脱敏保留；若对外发送原件，"
                        "建议人工删除批注。"
                        f"（作者：{'、'.join(sorted(authors)) if authors else '（空）'}；"
                        "批注正文与作者名独立于单元格，需单独脱敏）",
                        location=path,
                    )
                )

            # ---- 元数据清空策略 -------------------------------------
            cleared: list[str] = []
            kept: list[tuple[str, str]] = []
            props = getattr(wb, "properties", None)
            if props is not None:
                for field in _CLEAR_PROP_FIELDS:
                    value = getattr(props, field, None)
                    if isinstance(value, str) and value.strip():
                        cleared.append(f"{field}（core.xml）")
                for field in _KEEP_PROP_FIELDS:
                    value = getattr(props, field, None)
                    if isinstance(value, str) and value.strip():
                        kept.append((field, value))

            for field in _APP_CLEAR_FIELDS:
                try:
                    with zipfile.ZipFile(path) as archive:
                        blob = archive.read("docProps/app.xml")
                    root = etree.fromstring(blob)
                    node = self._app_child(root, field)
                except Exception:  # noqa: BLE001
                    node = None
                value = (node.text or "").strip() if node is not None else ""
                if value:
                    cleared.append(f"{field}（app.xml）")

            if cleared:
                findings.append(
                    Finding(
                        level="info",
                        code="xlsx.metadata_cleared",
                        message=f"已清空 {len(cleared)} 个元数据字段："
                        f"{'、'.join(cleared[:12])}。title 与 subject 保留脱敏后的值"
                        "（含案由信息，对分析有用）。",
                        location=path,
                    )
                )
            else:
                findings.append(
                    Finding(
                        level="info",
                        code="xlsx.metadata_cleared",
                        message="元数据字段均已清空（0 个待清），未发现作者/公司等残留。",
                        location=path,
                    )
                )
            for field, value in kept:
                findings.append(
                    Finding(
                        level="warn",
                        code="xlsx.metadata_kept",
                        message=f"{field} 保留为「{value}」。该字段按策略保留，交付前请确认"
                        "其中的当事人信息已被替换成代号。",
                        location=f"{path}!docProps/core.xml",
                    )
                )

            if wb.defined_names:
                findings.append(
                    Finding(
                        level="info",
                        code="xlsx.defined_names",
                        message=f"含 {len(wb.defined_names)} 个定义名称："
                        f"{'、'.join(list(wb.defined_names.keys())[:5])}。",
                        location=path,
                    )
                )
            if formula_cells:
                findings.append(
                    Finding(
                        level="info",
                        code="xlsx.formula",
                        message=f"含 {formula_cells} 个公式单元格，公式里的字符串常量"
                        "（如 =\"张三\"）不在单元格文本里。",
                        location=path,
                    )
                )

            # ---- role 统计 ------------------------------------------
            blocks = self.extract(path)
            counter = Counter(block.role for block in blocks)
            if counter:
                detail = "、".join(f"{role}={count}" for role, count in sorted(counter.items()))
                findings.append(
                    Finding(
                        level="info",
                        code="xlsx.roles",
                        message=f"提取到的文本块按 role 统计：{detail}。",
                        location=path,
                    )
                )
        finally:
            try:
                wb.close()
            except Exception:  # noqa: BLE001
                pass

        for message in self._diagnostics.get(self._key(path), [])[:20]:
            findings.append(
                Finding(level="warn", code="xlsx.error", message=message, location=path)
            )
        return findings


def _splice(text: str, edits: Sequence[Edit]) -> str:
    """把若干块内区间替换应用到一段文本（从后往前，避免偏移失效）。"""
    out = text
    for edit in sorted(edits, key=lambda e: e.entity.start, reverse=True):
        start, end = edit.entity.start, edit.entity.end
        if start < 0 or end > len(out) or end < start:
            continue
        out = out[:start] + edit.replacement + out[end:]
    return out


HANDLER = XlsxHandler()


__all__ = ["XlsxHandler", "XlsxLocator", "HANDLER"]

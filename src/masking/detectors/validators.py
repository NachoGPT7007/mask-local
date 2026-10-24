"""证件号 / 银行卡号的校验位算法，以及检测器共用的几个纯文本小工具。

难点与取舍
----------
1. 身份证、统一社会信用代码都是「格式 + 校验位」双约束。校验位能挡掉绝大多数
   随机数字串（一个 18 位随机串撞上身份证校验的概率约 1/11），是控制误报的
   第一道闸；但它只证明「这串数字形式合法」，不证明号码真实存在。
2. 正因为如此，校验失败的号码**仍要上报**（置信度降到 0.6 / 0.5），由引擎写入
   复核清单人工兜底。对脱敏工具而言，漏报是泄密，误报只是多一条待复核记录，
   两者代价不对称，一切取舍都偏向召回。
3. 15 位旧身份证没有校验位，只能做「省级区划前缀 + 出生日期合理性」判断，
   天然比 18 位更容易误报，所以只在两个条件都满足时才判为合法。
4. 本模块是纯函数集合，不依赖配置、不写文件、不抛异常（入参非法一律返回
   ``False``），既方便 ``regex_rules.py`` 调用，也方便单元测试直接覆盖。
5. ``detectors/__init__.py`` 的自动装配跳过清单里已包含 ``validators``，
   所以本模块不会被当成检测器加载；末尾的 ``is_cjk`` / ``masked_ranges``
   是两个无状态小工具，供各检测器复用，避免六份复制粘贴。
"""

from __future__ import annotations

import datetime
from typing import Iterable

# --------------------------------------------------------------------------- #
# 居民身份证 —— GB 11643-1999《公民身份号码》
# --------------------------------------------------------------------------- #

#: 18 位号码前 17 位的加权因子（GB 11643-1999 附录 B）
_ID_WEIGHTS: tuple[int, ...] = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)

#: 加权和模 11 之后对应的校验码，下标即余数 0-10
_ID_CHECK_CODES: str = "10X98765432"

#: GB/T 2260 省级行政区划代码前两位。真实身份证不会超出这个集合。
_PROVINCE_PREFIXES: frozenset[str] = frozenset(
    {
        "11", "12", "13", "14", "15",  # 华北
        "21", "22", "23",              # 东北
        "31", "32", "33", "34", "35", "36", "37",  # 华东
        "41", "42", "43", "44", "45", "46",  # 中南
        "50", "51", "52", "53", "54",  # 西南
        "61", "62", "63", "64", "65",  # 西北
        "71",                          # 台湾
        "81", "82",                    # 港澳
    }
)


def _looks_like_ascii_digits(s: str) -> bool:
    """严格判断是否全为 ASCII 数字。

    不用 ``str.isdigit()``：它会放过全角数字与「²」这类上标字符，
    导致 ``int()`` 抛异常或校验位算错。
    """
    return bool(s) and s.isascii() and s.isdigit()


def _plausible_date(year: int, month: int, day: int) -> bool:
    """判断年月日是否是真实存在的日期。"""
    if not (1900 <= year <= 2100):
        return False
    try:
        datetime.date(year, month, day)
    except ValueError:
        return False
    return True


def id_card_birth_date(s: str) -> datetime.date | None:
    """从身份证号中取出生日期，取不到或不合理时返回 ``None``。

    18 位取第 7-14 位；15 位取第 7-12 位（YYMMDD，世纪按 19xx 推断，
    因为 15 位号码 1999 年已停止发放）。
    """
    if not isinstance(s, str):
        return None
    if len(s) == 18 and _looks_like_ascii_digits(s[:17]):
        raw = s[6:14]
        if not _looks_like_ascii_digits(raw):
            return None
        y, m, d = int(raw[0:4]), int(raw[4:6]), int(raw[6:8])
    elif len(s) == 15 and _looks_like_ascii_digits(s):
        raw = s[6:12]
        y, m, d = 1900 + int(raw[0:2]), int(raw[2:4]), int(raw[4:6])
    else:
        return None
    if not _plausible_date(y, m, d):
        return None
    return datetime.date(y, m, d)


def is_valid_id_card(s: str) -> bool:
    """校验居民身份证号（18 位 GB 11643-1999 / 15 位旧号）。

    算法（18 位）
    ------------
    1. 前 17 位必须是 ASCII 数字，末位是数字或 ``X``/``x``。
    2. 前 17 位分别乘以加权因子 ``(7,9,10,5,8,4,2,1,6,3,7,9,10,5,8,4,2)``，
       求和后对 11 取余，余数 0-10 对应校验码 ``"10X98765432"``，
       与末位比对。
    3. 附加合理性检查：省级前缀在 GB/T 2260 已知集合内、出生日期真实存在、
       顺序码（第 15-17 位）不为 ``000``。这三条是 GB 11643 的必填含义，
       加上它们能挡掉「校验位碰巧算对」的随机 18 位数字。

    算法（15 位）
    ------------
    无校验位，只判断：全为数字、省级前缀合法、出生日期（19YYMMDD）真实存在、
    顺序码不为 000。因此 15 位判定比 18 位宽松，误报率也更高。

    参数
    ----
    s: 待校验字符串，允许恰好 18 位或 15 位。

    返回
    ----
    合法返回 ``True``，否则 ``False``；任何非法输入都返回 ``False``，不抛异常。
    """
    if not isinstance(s, str):
        return False
    if len(s) == 18:
        if not _looks_like_ascii_digits(s[:17]):
            return False
        if s[17] not in "0123456789Xx":
            return False
        if s[:2] not in _PROVINCE_PREFIXES:
            return False
        if s[14:17] == "000":
            return False
        if id_card_birth_date(s) is None:
            return False
        total = sum(int(ch) * w for ch, w in zip(s[:17], _ID_WEIGHTS))
        return _ID_CHECK_CODES[total % 11] == s[17].upper()

    if len(s) == 15:
        if not _looks_like_ascii_digits(s):
            return False
        if s[:2] not in _PROVINCE_PREFIXES:
            return False
        if s[12:15] == "000":
            return False
        return id_card_birth_date(s) is not None

    return False


# --------------------------------------------------------------------------- #
# 统一社会信用代码 —— GB 32100-2015
# --------------------------------------------------------------------------- #

#: 允许使用的 31 个字符。GB 32100 剔除了易混的 I、O、S、V、Z。
_USCC_CHARSET: str = "0123456789ABCDEFGHJKLMNPQRTUWXY"

#: 前 17 位的加权因子（GB 32100-2015 第 5.2 条）
_USCC_WEIGHTS: tuple[int, ...] = (
    1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28,
)

#: 第 1 位登记管理部门代码的合法取值
#: 1 机构编制 / 2 外交 / 3 司法行政 / 4 文化 / 5 民政 / 6 旅游
#: 7 宗教 / 8 工会 / 9 工商（市场监管）/ A 军委 / N 农业 / Y 其他
_USCC_FIRST_CHARS: frozenset[str] = frozenset("123456789ANY")


def is_valid_uscc(s: str) -> bool:
    """校验 18 位统一社会信用代码（GB 32100-2015）。

    算法
    ----
    1. 长度为 18，且 18 个字符全部落在字符集
       ``"0123456789ABCDEFGHJKLMNPQRTUWXY"`` 内（小写统一转大写比较）。
    2. 第 1 位是登记管理部门代码，取值必须在
       ``1-9 / A / N / Y`` 内。
    3. 前 17 位按下标查字符集得到 0-30 的数值，分别乘以加权因子
       ``(1,3,9,27,19,26,16,17,20,29,25,13,8,24,10,30,28)`` 求和。
    4. 校验值 = ``31 - (sum % 31)``；结果为 31 时取 0；再把校验值作为下标
       映射回字符集，与第 18 位比对。

    返回
    ----
    合法返回 ``True``，否则 ``False``；任何非法输入都返回 ``False``，不抛异常。
    """
    if not isinstance(s, str) or len(s) != 18:
        return False
    upper = s.upper()
    if upper[0] not in _USCC_FIRST_CHARS:
        return False
    if any(ch not in _USCC_CHARSET for ch in upper):
        return False
    total = sum(_USCC_CHARSET.index(ch) * w for ch, w in zip(upper[:17], _USCC_WEIGHTS))
    check = 31 - (total % 31)
    if check == 31:
        check = 0
    return _USCC_CHARSET[check] == upper[17]


# --------------------------------------------------------------------------- #
# 银行卡 —— ISO/IEC 7812-1 附录 B（Luhn / 模 10 算法）
# --------------------------------------------------------------------------- #


def is_valid_luhn(s: str) -> bool:
    """Luhn（模 10）校验，用于银行卡号、部分证件号。

    算法
    ----
    从右往左数，偶数位（从 0 开始计）乘以 2，结果大于 9 则减 9，
    全部相加后能被 10 整除即通过。要求入参全为 ASCII 数字，长度至少 2。

    返回
    ----
    通过返回 ``True``，否则 ``False``；非法输入返回 ``False``，不抛异常。
    """
    if not isinstance(s, str) or len(s) < 2 or not _looks_like_ascii_digits(s):
        return False
    total = 0
    for index, ch in enumerate(reversed(s)):
        digit = int(ch)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


# --------------------------------------------------------------------------- #
# 手机号
# --------------------------------------------------------------------------- #

#: 中国大陆手机号：11 位，1 开头，第二位 3-9
_MOBILE_PREFIXES: frozenset[str] = frozenset(
    {"13", "14", "15", "16", "17", "18", "19"}
)


def is_plausible_phone(s: str) -> bool:
    """判断是否像一个中国大陆手机号：11 位、``1[3-9]`` 开头、全为 ASCII 数字。

    这里只做格式判断，不做号段归属查询——归属地数据库需要联网，
    与本项目「纯本地」的硬约束冲突。
    """
    if not isinstance(s, str) or len(s) != 11:
        return False
    if not _looks_like_ascii_digits(s):
        return False
    return s[1] != "0" and s[1] != "1" and s[:2] in _MOBILE_PREFIXES


# --------------------------------------------------------------------------- #
# 共用小工具（无状态，供各检测器调用）
# --------------------------------------------------------------------------- #

#: 姓名、地名、机构名允许出现的汉字区间（基本区 + 扩展 A）
_CJK_RANGES: tuple[tuple[int, int], ...] = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF))


def is_cjk(ch: str) -> bool:
    """判断单个字符是否汉字（含扩展 A 区）。"""
    return bool(ch) and any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES)


def masked_ranges(text: str) -> list[tuple[int, int]]:
    """返回 ``【...】`` 已脱敏区间的列表（半开区间）。

    检测器一律跳过这些区间：其中的内容已经是代号或用户自己打的占位符，
    再识别一遍只会把代号替换成第二个代号，破坏一致性。
    未闭合的 ``【`` 视为普通字符，不产生区间。
    """
    out: list[tuple[int, int]] = []
    start = -1
    for index, ch in enumerate(text):
        if ch == "【":
            start = index
        elif ch == "】" and start >= 0:
            out.append((start, index + 1))
            start = -1
    return out


def within(ranges: Iterable[tuple[int, int]], start: int, end: int) -> bool:
    """判断半开区间 ``[start, end)`` 是否与任一区间相交。"""
    for lo, hi in ranges:
        if start < hi and end > lo:
            return True
    return False


__all__ = [
    "id_card_birth_date",
    "is_cjk",
    "is_plausible_phone",
    "is_valid_id_card",
    "is_valid_luhn",
    "is_valid_uscc",
    "masked_ranges",
    "within",
]

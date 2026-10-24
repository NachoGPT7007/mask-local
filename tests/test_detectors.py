"""实体检测层单元测试。

覆盖范围
--------
* ``validators``：身份证 GB 11643、统一社会信用代码 GB 32100、银行卡 Luhn、
  手机号格式——含正例、反例与「校验位被改一位」的负例。
* 六个检测器（regex / case_no / org / name / custom / place）：每个至少
  4 条正例、3 条反例，全部用 ``Settings.load()`` 加载真实 ``config/``。
* 姓名层专门覆盖需求点名的六个陷阱：原告认为、被告辩称、本院认为、合同约定，
  以及「原告张三与被告李四」一句两人。
* 跨层不变量：偏移在界内、检测器内部不重叠、不改写 ``block.text``、
  ``detect`` 不抛异常。
* 与分配器的联调：确认检测层给出的 ``EntityType`` 能拿到带框代号
  （``杭州市 → 【xx】市``），这是用户硬性要求的「方框符号」。

运行：
    cd /home/nachogpt/mask-local && .venv/bin/python -m pytest tests/test_detectors.py -q
"""

from __future__ import annotations

import pathlib
import sys

# 项目是 src 布局且未做可编辑安装，测试直接把 src 放进 sys.path
_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402

from masking.allocator import PlaceholderAllocator  # noqa: E402
from masking.config import Settings  # noqa: E402
from masking.detectors import build_detectors  # noqa: E402
from masking.detectors import validators as v  # noqa: E402
from masking.detectors.case_no import normalize_case_no  # noqa: E402
from masking.detectors.custom import CustomTermDetector  # noqa: E402
from masking.detectors.name import NameDetector  # noqa: E402
from masking.entity import EntityType, MatchContext, TextBlock  # noqa: E402


# --------------------------------------------------------------------------- #
# 夹具与工具
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def settings() -> Settings:
    """加载真实配置（lexicon.yaml + rules.yaml）。"""
    return Settings.load()


@pytest.fixture(scope="module")
def detectors(settings: Settings) -> dict[str, object]:
    """自动装配后的检测器，按 name 索引。"""
    return {d.name: d for d in build_detectors(settings)}


def detect(detector, text: str, ctx: MatchContext | None = None) -> list:
    """对单块文本跑一个检测器。"""
    block = TextBlock(text=text)
    result = list(detector.detect(block, ctx or MatchContext()))
    check_spans(result, text)
    return result


def texts(entities, etype: EntityType | None = None) -> list[str]:
    """取出实体的原文，可按类型过滤。"""
    return [e.text for e in entities if etype is None or e.type is etype]


def check_spans(entities, text: str) -> None:
    """所有偏移必须在 ``[0, len(text))`` 内，且 text 与原文一致。

    例外：案号输出的是**归一化**文本（全角→半角、括号统一、去空白），
    目的是让同一案号在所有出现位置拿到同一个代号；偏移仍指向原文，
    因此这里按归一化后比较。
    """
    for e in entities:
        assert 0 <= e.start < e.end <= len(text), f"越界: {e}"
        if e.type is EntityType.CASE_NO:
            assert normalize_case_no(text[e.start : e.end]) == e.text, f"案号归一化不符: {e}"
        else:
            assert text[e.start : e.end] == e.text, f"偏移与原文不符: {e}"
        assert 0.0 <= e.confidence <= 1.0


# --------------------------------------------------------------------------- #
# 校验位算法
# --------------------------------------------------------------------------- #


class TestValidators:
    """校验位算法。"""

    @pytest.mark.parametrize(
        "value",
        ["11010519491231002X", "110101199003078515"],
    )
    def test_id_card_18_valid(self, value: str) -> None:
        assert v.is_valid_id_card(value) is True

    def test_id_card_18_bad_checksum(self) -> None:
        """只把校验位改一位，必须判为不合法。"""
        assert v.is_valid_id_card("110105194912310021") is False

    def test_id_card_18_wrong_length_or_charset(self) -> None:
        assert v.is_valid_id_card("1101051949123100") is False
        assert v.is_valid_id_card("110105194912310021") is False
        assert v.is_valid_id_card("11010519491231002Y") is False

    def test_id_card_15_old_format(self) -> None:
        assert v.is_valid_id_card("110105491231002") is True

    def test_id_card_15_bad_birth_date(self) -> None:
        """15 位无校验位，只能靠出生日期合理性拦截：99 月不存在。"""
        assert v.is_valid_id_card("110105491331002") is False

    def test_id_card_birth_date(self) -> None:
        assert v.id_card_birth_date("11010519491231002X").year == 1949
        assert v.id_card_birth_date("110105491231002").year == 1949
        assert v.id_card_birth_date("不是身份证") is None

    @pytest.mark.parametrize("value", ["91350100M000100Y43", "91330100716105852F"])
    def test_uscc_valid(self, value: str) -> None:
        assert v.is_valid_uscc(value) is True

    def test_uscc_bad_checksum(self) -> None:
        assert v.is_valid_uscc("91350100M000100Y44") is False

    def test_uscc_illegal_charset(self) -> None:
        """I、O、S、V、Z 不在 GB 32100 字符集内。"""
        assert v.is_valid_uscc("91350100M000100Y4O") is False
        assert v.is_valid_uscc("91350100M000100Y4") is False

    def test_uscc_bad_first_char(self) -> None:
        """第 1 位是登记管理部门代码，不可能是 0。"""
        assert v.is_valid_uscc("01350100M000100Y43") is False

    @pytest.mark.parametrize("value", ["4111111111111111", "6217000010000004"])
    def test_luhn_valid(self, value: str) -> None:
        assert v.is_valid_luhn(value) is True

    def test_luhn_invalid(self) -> None:
        assert v.is_valid_luhn("4111111111111112") is False
        assert v.is_valid_luhn("6222020200000000") is False
        assert v.is_valid_luhn("abc") is False

    def test_plausible_phone(self) -> None:
        assert v.is_plausible_phone("13800138000") is True
        assert v.is_plausible_phone("19912345678") is True
        assert v.is_plausible_phone("12800138000") is False
        assert v.is_plausible_phone("1380013800") is False
        assert v.is_plausible_phone("23800138000") is False

    def test_masked_ranges(self) -> None:
        assert v.masked_ranges("原告【熊猫】与被告") == [(2, 6)]
        assert v.masked_ranges("没有脱敏标记") == []


# --------------------------------------------------------------------------- #
# 规则类检测器
# --------------------------------------------------------------------------- #


class TestRegexDetector:
    """身份证 / 手机 / 固话 / 银行卡 / USCC / 车牌 / 邮箱。"""

    def test_id_card_chinese_adjacency(self, detectors) -> None:
        """中文紧邻、无标点也必须命中（历史 bug：isalnum 把汉字当字母）。"""
        hits = detect(detectors["regex"], "身份证号330102199001011234")
        assert texts(hits, EntityType.ID_CARD) == ["330102199001011234"]
        assert hits[0].confidence == 1.0

    def test_id_card_ending_X_chinese_adjacency(self, detectors) -> None:
        """回归：18 位「17 数字 + X」曾被 USCC 分支抢走，误判成 0.6 的信用代码。

        USCC 字符集恰好也含 X，所以数字扫描必须先跑、USCC 只做补充。
        """
        hits = detect(detectors["regex"], "公民身份号码33010619900101123X，")
        assert texts(hits, EntityType.ID_CARD) == ["33010619900101123X"]
        assert hits[0].confidence == 1.0
        assert EntityType.USCC not in {e.type for e in hits}

    def test_uscc_with_letters_sample(self, detectors) -> None:
        hits = detect(detectors["regex"], "统一社会信用代码91330100MA2K7X8Y9R")
        assert texts(hits, EntityType.USCC) == ["91330100MA2K7X8Y9R"]
        assert hits[0].confidence == 1.0

    def test_19_digits_not_cut_into_18_digit_id(self, detectors) -> None:
        """19 位数字不得从中间切出 18 位身份证。"""
        hits = detect(detectors["regex"], "编号1330106199001011234")
        assert EntityType.ID_CARD not in {e.type for e in hits}

    def test_phone_chinese_adjacency(self, detectors) -> None:
        hits = detect(detectors["regex"], "电话13800138000")
        assert texts(hits, EntityType.PHONE) == ["13800138000"]
        assert hits[0].confidence == 0.95

    def test_landline(self, detectors) -> None:
        hits = detect(detectors["regex"], "座机0571-88888888")
        assert texts(hits, EntityType.PHONE) == ["0571-88888888"]
        assert hits[0].confidence == 0.7

    def test_bank_card_luhn_pass(self, detectors) -> None:
        hits = detect(detectors["regex"], "银行卡622202020000000005")
        assert texts(hits, EntityType.BANK_CARD) == ["622202020000000005"]
        assert hits[0].confidence == 1.0

    def test_bank_card_luhn_fail_kept_at_low_confidence(self, detectors) -> None:
        hits = detect(detectors["regex"], "卡号6222020200000000")
        assert texts(hits, EntityType.BANK_CARD) == ["6222020200000000"]
        assert hits[0].confidence == 0.5

    def test_uscc_with_letters(self, detectors) -> None:
        hits = detect(detectors["regex"], "统一社会信用代码91350100M000100Y43")
        assert texts(hits, EntityType.USCC) == ["91350100M000100Y43"]
        assert hits[0].confidence == 1.0

    def test_plate_普通与新能源(self, detectors) -> None:
        hits = detect(detectors["regex"], "车牌号浙A12345，另有粤B12345D。")
        assert texts(hits, EntityType.PLATE) == ["浙A12345", "粤B12345D"]

    def test_email(self, detectors) -> None:
        hits = detect(detectors["regex"], "邮箱zhang.san@lawfirm.com.cn。")
        assert texts(hits, EntityType.EMAIL) == ["zhang.san@lawfirm.com.cn"]

    def test_id_card_low_confidence_when_checksum_bad(self, detectors) -> None:
        """校验失败仍要报（0.6），不能沉默——漏报比误报危险。"""
        hits = detect(detectors["regex"], "身份证110105194912310021")
        assert texts(hits, EntityType.ID_CARD) == ["110105194912310021"]
        assert hits[0].confidence == 0.6

    # ---- 反例 -------------------------------------------------------------

    def test_date_and_amount_not_reported(self, detectors) -> None:
        """日期、金额、普通短数字不应变成证件号。"""
        hits = detect(detectors["regex"], "本院于2024年3月5日立案，标的额120000元。")
        assert hits == []

    def test_case_no_not_stolen_by_regex(self, detectors) -> None:
        """案号交给 case_no 检测器，regex 不得抢走或误判。"""
        hits = detect(detectors["regex"], "案号(2024)甲法民初1234号")
        assert hits == []

    def test_short_or_malformed_phone_ignored(self, detectors) -> None:
        assert detect(detectors["regex"], "订单号1380013800") == []
        assert detect(detectors["regex"], "编号12800138000") == []

    def test_number_never_double_classified(self, detectors) -> None:
        """同一段数字只能有一个类型：身份证不得同时报成银行卡。"""
        hits = detect(detectors["regex"], "身份证号110101199003078515")
        assert len(hits) == 1
        assert hits[0].type is EntityType.ID_CARD

    def test_already_masked_span_skipped(self, detectors) -> None:
        assert detect(detectors["regex"], "身份证号【XXX】") == []


# --------------------------------------------------------------------------- #
# 姓名检测器
# --------------------------------------------------------------------------- #


class TestNameDetector:
    """中文姓名：线索词、边界词、排除词、跨块已知姓名。"""

    def test_two_names_in_one_sentence(self, detectors) -> None:
        """需求点名：「原告张三与被告李四」→ 张三、李四。"""
        hits = detect(detectors["name"], "原告张三与被告李四")
        assert texts(hits, EntityType.PERSON) == ["张三", "李四"]
        assert all(e.confidence >= 0.9 for e in hits)

    def test_legal_roles(self, detectors) -> None:
        hits = detect(
            detectors["name"],
            "法定代表人李四，委托诉讼代理人王五，审判员赵六，书记员钱七。",
        )
        assert texts(hits, EntityType.PERSON) == ["李四", "王五", "赵六", "钱七"]

    def test_cue_with_fullwidth_colon(self, detectors) -> None:
        hits = detect(detectors["name"], "原告：张三；被告：李四")
        assert texts(hits, EntityType.PERSON) == ["张三", "李四"]

    def test_compound_surname(self, detectors) -> None:
        hits = detect(detectors["name"], "原告欧阳修文与被告司马光")
        assert texts(hits, EntityType.PERSON) == ["欧阳修文", "司马光"]

    def test_name_before_predicate_verb(self, detectors) -> None:
        """「李四拖欠」不能切成「李四拖」。"""
        hits = detect(detectors["name"], "被告李四拖欠货款")
        assert texts(hits, EntityType.PERSON) == ["李四"]

    def test_multiple_names_after_one_cue(self, detectors) -> None:
        """真实案例：一个「审理法官：」后面跟多个姓名，必须全部命中。"""
        hits = detect(detectors["name"], "审理法官：肖芄 赖粤旭 张丽")
        assert texts(hits, EntityType.PERSON) == ["肖芄", "赖粤旭", "张丽"]
        assert all(e.confidence == 0.9 for e in hits)

    def test_cue_with_spaces_between_chars(self, detectors) -> None:
        """PDF 表格里「审判员」常被拆成「审　判　员」。"""
        hits = detect(detectors["name"], "审　判　员　　赖粤旭")
        assert texts(hits, EntityType.PERSON) == ["赖粤旭"]

    def test_name_list_block(self, detectors) -> None:
        """标签与姓名被拆成两个 TextBlock 时，靠纯姓名列表块兜住。"""
        hits = detect(detectors["name"], "肖芄 赖粤旭 张丽")
        assert texts(hits, EntityType.PERSON) == ["肖芄", "赖粤旭", "张丽"]
        assert all(e.confidence == 0.75 for e in hits)

    def test_line_wrapped_block_start_not_name(self, detectors) -> None:
        """PDF 折行：「…专科专」+ 下一块「业自2021年」不能切出「业自」。"""
        ctx = MatchContext()
        detectors["name"].detect(TextBlock(text="的部分专科专"), ctx)
        hits = list(detectors["name"].detect(TextBlock(text="业自2021年起已全部迁至"), ctx))
        assert [e.text for e in hits] == []

    def test_block_start_after_punctuation_is_name(self, detectors) -> None:
        """上一块以标点收尾，本块开头就是真正的行首，姓名照常命中。"""
        ctx = MatchContext()
        detectors["name"].detect(TextBlock(text="本院判决如下："), ctx)
        hits = list(detectors["name"].detect(TextBlock(text="张三到庭参加诉讼"), ctx))
        assert [e.text for e in hits] == ["张三"]

    def test_merged_block_disables_wrap_guard(self, detectors) -> None:
        """与 pdf-fmt 的接口：merged_lines>1 时关闭折行保护。

        合并块内部已经没有跨块断词，若仍按「上一块以汉字收尾」拒绝块首，
        正常段落的第一句就会被漏掉。
        """

        class _MergedLocator:
            merged_lines = 3

        ctx = MatchContext()
        detectors["name"].detect(TextBlock(text="的部分专科专"), ctx)
        block = TextBlock(text="张三到庭参加诉讼", locator=_MergedLocator())
        hits = list(detectors["name"].detect(block, ctx))
        assert [e.text for e in hits] == ["张三"]

    def test_unmerged_block_keeps_wrap_guard(self, detectors) -> None:
        """未合并块（merged_lines 缺失或为 1）仍按折行保护拒绝块首。"""

        class _PlainLocator:
            merged_lines = 1

        ctx = MatchContext()
        detectors["name"].detect(TextBlock(text="的部分专科专"), ctx)
        block = TextBlock(text="张三到庭参加诉讼", locator=_PlainLocator())
        assert list(detectors["name"].detect(block, ctx)) == []

    def test_name_list_rejects_place_words(self, detectors) -> None:
        """「杭州 上海」是地名列表，不是人名列表。"""
        assert detect(detectors["name"], "杭州 上海 苏州") == []

    def test_known_name_from_context(self, detectors) -> None:
        """前文确认过的姓名，后文无线索词也要捞回来（跨块召回）。"""
        ctx = MatchContext()
        ctx.known["张三"] = EntityType.PERSON
        hits = detect(detectors["name"], "张三主张合同无效。", ctx)
        assert "张三" in texts(hits, EntityType.PERSON)

    def test_skips_masked_bracket(self, detectors) -> None:
        hits = detect(detectors["name"], "原告【熊猫】与被告李四")
        assert texts(hits, EntityType.PERSON) == ["李四"]

    # ---- 反例（需求点名的陷阱） -------------------------------------------

    @pytest.mark.parametrize(
        "text",
        [
            "原告认为诉讼请求成立。",
            "被告辩称合同无效。",
            "本院认为被告主张不能成立。",
            "合同约定由被告履行。",
        ],
    )
    def test_predicate_not_name(self, detectors, text: str) -> None:
        assert detect(detectors["name"], text) == []

    def test_court_and_org_not_person(self, detectors) -> None:
        assert detect(detectors["name"], "人民法院应当依法审理本案。") == []
        assert detect(detectors["name"], "公司财务部门作出说明。") == []
        assert detect(detectors["name"], "原告提交证据材料。") == []

    @pytest.mark.parametrize(
        "text",
        ["书记员家属", "证人子女", "被告配偶", "原告本人", "证人同事"],
    )
    def test_kinship_words_not_name(self, detectors, text: str) -> None:
        """亲属称谓词在证人证言里高频出现，不能当成人名。"""
        assert detect(detectors["name"], text) == []

    def test_metadata_sheet_name_relaxed(self, detectors) -> None:
        """sheet 名「张三证据清单」后继不是标点，放宽后必须命中 0.85。"""
        block = TextBlock(text="张三证据清单", role="sheet_name")
        hits = list(detectors["name"].detect(block, MatchContext()))
        assert [e.text for e in hits] == ["张三"]
        assert hits[0].confidence == 0.85

    def test_metadata_no_mid_word_fragment(self, detectors) -> None:
        """放宽后仍要有左边界，不能在一串汉字中间切出碎片。"""
        block = TextBlock(
            text="张三与杭州某某科技有限公司买卖合同纠纷", role="metadata"
        )
        hits = list(detectors["name"].detect(block, MatchContext()))
        assert [e.text for e in hits] == ["张三"]

    def test_metadata_signature_cue(self, detectors) -> None:
        """页眉「承办人：李四」——承办人不在 rules.yaml 线索表里，本地补充。"""
        block = TextBlock(text="杭州市西湖区人民法院　承办人：李四", role="header")
        hits = list(detectors["name"].detect(block, MatchContext()))
        assert "李四" in [e.text for e in hits]

    def test_common_words_not_name(self, detectors) -> None:
        """「安全 / 文明 / 完毕」这类姓 + 常用字组合必须挡住。"""
        assert detect(detectors["name"], "安全第一，文明施工。") == []


# --------------------------------------------------------------------------- #
# 机构检测器
# --------------------------------------------------------------------------- #


class TestOrgDetector:
    """公司、律所、医院、学校、银行。"""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("杭州某某科技有限公司", "杭州某某科技有限公司"),
            ("浙江某某律师事务所", "浙江某某律师事务所"),
            ("某某县人民医院", "某某县人民医院"),
        ],
    )
    def test_org_suffixes(self, detectors, text: str, expected: str) -> None:
        hits = detect(detectors["org"], text)
        assert texts(hits, EntityType.ORG) == [expected]
        assert hits[0].confidence == 0.9

    def test_bank_full_name(self, detectors) -> None:
        hits = detect(detectors["org"], "中国工商银行股份有限公司杭州分行")
        assert texts(hits, EntityType.ORG) == ["中国工商银行股份有限公司杭州分行"]

    def test_org_strips_role_prefix(self, detectors) -> None:
        """「原告张三诉杭州某某有限公司」只能报机构本身。"""
        hits = detect(detectors["org"], "原告张三诉杭州某某有限公司")
        assert texts(hits, EntityType.ORG) == ["杭州某某有限公司"]

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("判令被告杭州某某科技有限公司赔偿", "杭州某某科技有限公司"),
            ("判令杭州某某科技有限公司承担", "杭州某某科技有限公司"),
            ("判决被告浙江某某律师事务所赔偿", "浙江某某律师事务所"),
            ("请求判令被告杭州某某有限公司付款", "杭州某某有限公司"),
            ("由杭州某某科技有限公司承担", "杭州某某科技有限公司"),
        ],
    )
    def test_legal_verb_prefix_stripped(self, detectors, text: str, expected: str) -> None:
        """判决主文里的「判令 / 判决 / 请求判令」不能粘进机构名。

        这类越界会让同一家公司拿到两个代号，法律工作台会当成两个主体。
        """
        hits = detect(detectors["org"], text)
        assert texts(hits, EntityType.ORG) == [expected]

    def test_same_org_same_key(self, detectors) -> None:
        """两种写法必须切出完全相同的原文，否则分配器会给出两个代号。"""
        a = detect(detectors["org"], "被告杭州某某科技有限公司")
        b = detect(detectors["org"], "判令被告杭州某某科技有限公司赔偿")
        assert texts(a, EntityType.ORG) == texts(b, EntityType.ORG)

    # ---- 反例 -------------------------------------------------------------

    @pytest.mark.parametrize(
        "text",
        [
            "杭州市西湖区人民法院",
            "某某市人民检察院",
            "某某县人民政府",
            "全国人民代表大会",
        ],
    )
    def test_court_and_gov_excluded(self, detectors, text: str) -> None:
        """法院 / 检察院 / 政府 / 人大不归机构层，交给 place 层拆地名。"""
        assert detect(detectors["org"], text) == []

    @pytest.mark.parametrize(
        "text",
        ["该医院", "本公司", "总公司", "分公司", "有限公司", "人民医院"],
    )
    def test_generic_org_rejected(self, detectors, text: str) -> None:
        """字号不足 2 字的指代性说法不是实体。"""
        assert detect(detectors["org"], text) == []


# --------------------------------------------------------------------------- #
# 地名检测器
# --------------------------------------------------------------------------- #


class TestPlaceDetector:
    """行政区划与道路切分。"""

    def test_address_cut_into_three_parts(self, detectors) -> None:
        """核心难点：不能切成「市西湖」「区文三」。"""
        hits = detect(detectors["place"], "杭州市西湖区文三路123号")
        assert texts(hits, EntityType.PLACE) == ["杭州市", "西湖区", "文三路"]

    def test_province_city_district(self, detectors) -> None:
        hits = detect(detectors["place"], "浙江省杭州市西湖区")
        assert texts(hits, EntityType.PLACE) == ["浙江省", "杭州市", "西湖区"]

    def test_autonomous_region_and_new_district(self, detectors) -> None:
        hits = detect(detectors["place"], "内蒙古自治区呼和浩特市新城区")
        assert texts(hits, EntityType.PLACE) == ["内蒙古自治区", "呼和浩特市", "新城区"]

    def test_special_administrative_region(self, detectors) -> None:
        hits = detect(detectors["place"], "香港特别行政区")
        assert texts(hits, EntityType.PLACE) == ["香港特别行政区"]

    def test_court_address_only_places(self, detectors) -> None:
        """「杭州市西湖区人民法院」只输出两个地名，不含「人民法院」。"""
        hits = detect(detectors["place"], "杭州市西湖区人民法院")
        assert texts(hits, EntityType.PLACE) == ["杭州市", "西湖区"]
        assert all("人民法院" not in e.text for e in hits)

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("副本，上诉于浙江省杭州市中级人民法院。", ["浙江省", "杭州市"]),
            ("原告张三，住杭州市西湖区。", ["杭州市", "西湖区"]),
            ("现住杭州市余杭区", ["杭州市", "余杭区"]),
            ("位于杭州市西湖区", ["杭州市", "西湖区"]),
            ("住所地杭州市", ["杭州市"]),
            ("迁至杭州市", ["杭州市"]),
        ],
    )
    def test_verb_prefix_stripped(self, detectors, text: str, expected: list) -> None:
        """「住杭州市」不能整段变成地名——否则同一城市拿到两个代号。"""
        hits = detect(detectors["place"], text)
        assert texts(hits, EntityType.PLACE) == expected

    def test_residence_suffix_not_swallowed(self, detectors) -> None:
        """「被告住所地杭州市」不能粘成「所地杭州市」。"""
        hits = detect(detectors["place"], "被告住所地杭州市西湖区")
        assert texts(hits, EntityType.PLACE) == ["杭州市", "西湖区"]

    # ---- 真实案例回归：区划词块约束 ---------------------------------------

    @pytest.mark.parametrize(
        "text",
        [
            "用户反复纠结校区，可能隐含矛盾。",
            "既想要昆明资源又怕偏远",
            "或直接推荐区位更好的昆明工业职院",
            "呈贡校区即使有专科",
            "杨林职教园区",
        ],
    )
    def test_zone_blockers(self, detectors, text: str) -> None:
        """「校区 / 区位 / 园区」不能被当地名擦掉——这是真实案例里的最严重误脱。"""
        hits = detect(detectors["place"], text)
        assert all(not e.text.endswith("区") for e in hits), [
            (e.text, e.start, e.end) for e in hits
        ]

    def test_zone_blockers_keep_real_district(self, detectors) -> None:
        """真实的「呈贡区」必须保留。"""
        hits = detect(detectors["place"], "昆明市呈贡区启秀街318号")
        assert texts(hits, EntityType.PLACE) == ["昆明市", "呈贡区", "启秀街"]

    def test_zone_exempt_real_chengtai(self, detectors) -> None:
        """「新城区」是真实行政区，不能被 blocker 里的「城」误杀。"""
        hits = detect(detectors["place"], "呼和浩特市新城区")
        assert texts(hits, EntityType.PLACE) == ["呼和浩特市", "新城区"]

    # ---- 真实案例回归：不带后缀的省市名 -----------------------------------

    def test_bare_province_and_city(self, detectors) -> None:
        assert texts(detect(detectors["place"], "云南物理类xxx分"), EntityType.PLACE) == ["云南"]
        assert texts(detect(detectors["place"], "既想要昆明资源"), EntityType.PLACE) == ["昆明"]

    def test_bare_place_in_court_name(self, detectors) -> None:
        """「杭州互联网法院」→ 只脱「杭州」，「互联网法院」保留。"""
        hits = detect(detectors["place"], "杭州互联网法院")
        assert texts(hits, EntityType.PLACE) == ["杭州"]
        assert all("法院" not in e.text for e in hits)

    @pytest.mark.parametrize(
        "court",
        ["杭州互联网法院", "上海知识产权法院", "北京金融法院", "广州海事法院"],
    )
    def test_specialized_courts(self, detectors, court: str) -> None:
        """专门法院：地名照常脱敏，法院类型后缀不并入地名。"""
        hits = detect(detectors["place"], court)
        assert len(hits) == 1
        assert hits[0].text in ("杭州", "上海", "北京", "广州")

    def test_suffix_form_not_duplicated(self, detectors) -> None:
        """「云南省」只产出一条带后缀的实体，不额外产出「云南」。"""
        hits = detect(detectors["place"], "云南省昭通市")
        assert texts(hits, EntityType.PLACE) == ["云南省", "昭通市"]

    # ---- 反例 -------------------------------------------------------------

    @pytest.mark.parametrize(
        "text",
        ["城市管理", "本地区", "小区", "开发区", "道路", "铁路", "本市", "市场",
         "风情小镇", "故乡的云", "园区管理"],
    )
    def test_generic_place_words_rejected(self, detectors, text: str) -> None:
        assert detect(detectors["place"], text) == []

    def test_no_place_in_plain_legal_text(self, detectors) -> None:
        assert detect(detectors["place"], "本院认为被告主张不能成立。") == []


# --------------------------------------------------------------------------- #
# 案号检测器
# --------------------------------------------------------------------------- #


class TestCaseNoDetector:
    """案号：括号形态、全角半角、法院代字。"""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("(2024)甲法民初1234号", "(2024)甲法民初1234号"),
            ("（2024）浙01民终567号", "(2024)浙01民终567号"),
            ("〔2024〕京0105执123号", "(2024)京0105执123号"),
            ("[2024]沪0115民初1号", "(2024)沪0115民初1号"),
        ],
    )
    def test_bracket_variants(self, detectors, text: str, expected: str) -> None:
        """四种括号统一归一化成半角圆括号，保证全文同一个案号同一个键。"""
        hits = detect(detectors["case_no"], text)
        assert texts(hits, EntityType.CASE_NO) == [expected]
        assert hits[0].confidence == 0.95

    def test_same_case_no_one_key(self, detectors) -> None:
        """真实案例回归：全角/半角写法必须产出逐字节相同的 text。"""
        full = detect(detectors["case_no"], "（2025）浙0192民初18143号")
        half = detect(detectors["case_no"], "(2025)浙0192民初18143号")
        assert full[0].text == half[0].text == "(2025)浙0192民初18143号"

    def test_case_no_split_across_lines(self, detectors) -> None:
        """PDF 常见断行：「（2025）浙0192」+ 换行 +「民初18143号」。"""
        hits = detect(detectors["case_no"], "（2025）浙0192\n民初18143号")
        assert texts(hits, EntityType.CASE_NO) == ["(2025)浙0192民初18143号"]

    def test_case_no_spaces_inside(self, detectors) -> None:
        hits = detect(detectors["case_no"], "（2025）浙 0192 民初 18143 号")
        assert texts(hits, EntityType.CASE_NO) == ["(2025)浙0192民初18143号"]

    def test_case_no_in_sentence(self, detectors) -> None:
        hits = detect(detectors["case_no"], "本案案号为（2024）浙01民终567号，现已审结。")
        assert texts(hits, EntityType.CASE_NO) == ["(2024)浙01民终567号"]

    # ---- 反例 -------------------------------------------------------------

    @pytest.mark.parametrize(
        "text",
        [
            "(2024)年度工作报告",
            "2024年3月5日立案",
            "民初1234号",
            "（2024）甲法民初",
        ],
    )
    def test_not_case_no(self, detectors, text: str) -> None:
        assert detect(detectors["case_no"], text) == []


# --------------------------------------------------------------------------- #
# 自定义词表
# --------------------------------------------------------------------------- #


class TestCustomDetector:
    """用户词表：注释、空行、长词优先、文件缺失。"""

    def _write(self, tmp_path: pathlib.Path, body: str) -> pathlib.Path:
        path = tmp_path / "custom_terms.txt"
        path.write_text(body, encoding="utf-8")
        return path

    def test_hits_and_comments(self, settings, tmp_path) -> None:
        path = self._write(
            tmp_path,
            "# 这是注释\n\n某某项目\n华政\n张三丰\n张三\n",
        )
        det = CustomTermDetector(settings, terms_path=path)
        hits = detect(det, "张三丰参与某某项目。")
        assert texts(hits, EntityType.CUSTOM) == ["张三丰", "某某项目"]
        assert all(e.confidence == 1.0 for e in hits)

    def test_long_term_first(self, settings, tmp_path) -> None:
        """「张三丰」优先于「张三」，不能掩成「【XXX】丰」。"""
        path = self._write(tmp_path, "张三\n张三丰\n")
        det = CustomTermDetector(settings, terms_path=path)
        hits = detect(det, "张三丰")
        assert texts(hits, EntityType.CUSTOM) == ["张三丰"]

    def test_multiple_occurrences(self, settings, tmp_path) -> None:
        path = self._write(tmp_path, "华政\n")
        det = CustomTermDetector(settings, terms_path=path)
        hits = detect(det, "华政与华政。")
        assert texts(hits) == ["华政", "华政"]

    def test_missing_file_is_empty(self, settings, tmp_path) -> None:
        det = CustomTermDetector(settings, terms_path=tmp_path / "not_exist.txt")
        assert detect(det, "任意文本") == []

    def test_commented_term_not_hit(self, settings, tmp_path) -> None:
        path = self._write(tmp_path, "# 华政\n某某项目\n")
        det = CustomTermDetector(settings, terms_path=path)
        assert detect(det, "华政") == []

    def test_unrelated_text(self, settings, tmp_path) -> None:
        path = self._write(tmp_path, "某某项目\n")
        det = CustomTermDetector(settings, terms_path=path)
        assert detect(det, "本院认为被告主张不能成立。") == []

    def test_parse_terms(self) -> None:
        terms = CustomTermDetector.parse_terms("# 注释\n\n甲\n乙乙\n")
        assert terms == ["乙乙", "甲"]


# --------------------------------------------------------------------------- #
# 装配、跨层不变量与分配器联调
# --------------------------------------------------------------------------- #


class TestAssembly:
    """自动装配与前缀不变量。"""

    def test_all_detectors_assembled(self, detectors) -> None:
        assert set(detectors) == {"regex", "case_no", "org", "name", "custom", "place"}

    def test_orders_sorted(self, settings) -> None:
        orders = [d.order for d in build_detectors(settings)]
        assert orders == sorted(orders)

    def test_detector_protocol_fields(self, detectors) -> None:
        for name, det in detectors.items():
            assert det.name == name
            assert isinstance(det.order, int)

    def test_no_overlap_within_detector(self, detectors) -> None:
        """同一检测器内部不得输出重叠命中（engine 另有跨检测器消解）。"""
        text = (
            "原告张三与被告李四，身份证号110101199003078515，电话13800138000，"
            "住杭州市西湖区文三路123号，杭州某某科技有限公司，"
            "案号(2024)甲法民初1234号，邮箱a.b@c.com。"
        )
        for det in detectors.values():
            hits = detect(det, text)
            for x, y in zip(hits, hits[1:]):
                assert x.end <= y.start, f"{det.name} 重叠: {x} / {y}"

    def test_block_text_unchanged(self, detectors) -> None:
        text = "原告张三，身份证号110101199003078515。"
        block = TextBlock(text=text)
        for det in detectors.values():
            det.detect(block, MatchContext())
        assert block.text == text

    def test_detect_never_raises_on_hostile_input(self, detectors) -> None:
        """空串、超长串、只有符号、未闭合方括号都不能抛异常。"""
        for text in ["", "【", "】】】", "【未闭合", "0" * 200, "@#$%^&*()"]:
            for det in detectors.values():
                det.detect(TextBlock(text=text), MatchContext())

    def test_real_config_loaded(self, settings) -> None:
        """确认读到的是真实 rules.yaml，而不是空配置。"""
        assert "原告" in settings.rules.name_cues()
        assert "认为" in settings.rules.name_blockers()


class TestAllocatorIntegration:
    """联调：检测层给出的类型必须能换到带方框的代号（用户硬性要求）。"""

    def test_code_words_are_framed(self, settings, detectors) -> None:
        text = "原告张三与被告李四，住杭州市西湖区，杭州某某科技有限公司。"
        block = TextBlock(text=text)
        ctx = MatchContext()
        entities = []
        for det in build_detectors(settings):
            entities.extend(det.detect(block, ctx))

        allocator = PlaceholderAllocator(lexicon=settings.lexicon, rules=settings.rules)
        allocator.prepare(entities)
        replacements = {e.text: allocator.placeholder_for(e) for e in entities}

        # 人名 / 地名 / 机构名都走 code_word，必须带【】
        assert replacements["张三"].startswith("【") and replacements["张三"].endswith("】")
        assert replacements["李四"].startswith("【")
        # 地名保留后缀，层级信息不丢：杭州市 → 【xx】市
        assert replacements["杭州市"].startswith("【")
        assert replacements["杭州市"].endswith("】市")
        # 机构保留机构类型后缀
        assert replacements["杭州某某科技有限公司"].endswith("】公司")

    def test_place_split_suffix_survives(self, settings, detectors) -> None:
        """「西湖区」的代号必须以「区」结尾。"""
        block = TextBlock(text="杭州市西湖区")
        entities = list(detectors["place"].detect(block, MatchContext()))
        allocator = PlaceholderAllocator(lexicon=settings.lexicon, rules=settings.rules)
        allocator.prepare(entities)
        for e in entities:
            assert allocator.placeholder_for(e).endswith(e.text[-1])

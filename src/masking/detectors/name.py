"""中文姓名识别（纯自研，不依赖 jieba 等分词库）。

难点与取舍
----------
姓名识别是整个脱敏链路上最容易两头挨打的一层：

* **漏报**＝真名留在文书里，等于没脱敏，是最严重的失败；
* **误报**＝把「原告认为」当成名字替换掉，文书读不通，还白白消耗代号词池。

本模块的取舍是「召回优先、误报靠置信度分级兜底」：

1. **线索词是第一证据**。``config/rules.yaml`` 的 ``name_cues`` 是法律文书里
   姓名的固定锚点（原告 / 法定代表人 / 审判员 / 证人 …）。命中线索词后，
   在 0-6 字内（只允许空格、全角空格、「：」、顿号、逗号作为间隔）取
   2-4 字纯中文串，置信度 0.9。
2. **边界词决定名字在哪里结束**。线索词后面紧跟的往往是「认为 / 辩称 /
   委托 / 与 / 及 / 系 / 男 / 女 / 户籍」这类谓语或身份标记，它们把
   一串汉字切成「名字 + 后续」，取边界词之前的片段就得到姓名。
   这比「取固定 2-3 字」稳得多：``原告张三与被告李四`` 能正确切成两个人。
3. **无边界时按姓氏长度封顶**。单姓名字最多 3 字（姓 + 2 名），复姓最多 4 字。
   4 字单姓（欧阳某玺这类）会被截成 3 字，这是已知取舍：宁可少掩一个字，
   也不要把相邻的两个名字粘成一个。
4. **无线索模式只做兜底**。要求「姓氏 + 2-3 字 + 后继是标点/空格/连接词」，
   置信度仅 0.5，并且额外要求名字部分全部落在常用名字用字表里。
   这一条会带来零星的误报（如「安全」「文明」），已用停用词表压制；
   但它是「原告王某平」这类名字含连接字（和）时唯一的兜底通道。
5. **排序失败的代价**：名字里含「和 / 于 / 为 / 有」等连接字时，线索词模式
   会被边界词提前截断（``原告王某平与…`` 只切到「王」而放弃），
   改由 0.5 置信度的无线索模式捞回，仍能掩掉，但置信度低。

已知局限（交付时同步给 Lead）
------------------------------
* 无线索、无标点、无连接词的两个名字连写（``原告张三李四``）会粘成
  「张三李」，残留一个「四」；
* 少数名字用字不在常用字表内（生僻名、少数民族音译名）时，无线索模式漏报；
* 名字本身含「和 / 与 / 为」等字时只走 0.5 兜底；
* 只有姓氏没有名字的表述（「张氏」「王氏」）按 2 字处理，会掩成「张氏」。
* 无线索模式要求严格左边界：PDF 折行会把词从中间断开（「专科专|业自2021」），
  这类接续块的首字不做推断，避免切出「业自」「向声」「司提」这类碎片。
* 纯自研、无分词器，因此不识别倒装（「某某公司的张三」靠线索词命中，
  但「张三系该公司员工」这类无线索句要靠无线索模式，置信度 0.5）。
"""

from __future__ import annotations

import re
from typing import Sequence

from ..config import Settings
from ..entity import Detector, Entity, EntityType, MatchContext, TextBlock
from .validators import is_cjk, masked_ranges, within

# --------------------------------------------------------------------------- #
# 姓氏表
# --------------------------------------------------------------------------- #

#: 单姓。取自《百家姓》常见字并补充现代高频姓，共 400+ 个字符。
#: 注意：这里刻意不收「合」「同」「言」「事」等高频普通字，
#: 它们当姓极罕见，收进来只会把「合同约定」变成人名。
_SINGLE_SURNAMES: str = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚"
    "程嵇邢滑裴陆荣翁荀羊惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧"
    "隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶"
    "幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴郁胥能苍双闻"
    "莘党翟谭贡劳逄姬申扶堵冉宰郦雍却璩桑桂濮牛寿通边扈燕冀郏浦尚农温"
    "别庄晏柴阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国"
    "文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋"
    "沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公原随庆承位振殿银生业"
    "勤儒殿敬群峰洲滨洋港睿哲翰轩然照燮茂肖闫覃冼"
)

#: 复姓。前 20 个是需求点名的必收项，后面补充文书里能见到的其他复姓。
_COMPOUND_SURNAMES: tuple[str, ...] = (
    "欧阳", "司马", "上官", "诸葛", "东方", "皇甫", "尉迟", "公孙", "慕容",
    "长孙", "宇文", "司徒", "司空", "令狐", "夏侯", "轩辕", "端木", "独孤",
    "南宫", "西门",
    "太史", "申屠", "公冶", "宗政", "濮阳", "淳于", "单于", "赫连", "鲜于",
    "闾丘", "左丘", "东郭", "百里", "呼延", "钟离", "拓跋", "万俟", "闻人",
    "澹台", "公良", "漆雕", "乐正", "公西", "巫马", "梁丘", "东门", "微生",
    "羊舌", "子车", "第五",
)

#: 常用名字用字。仅用于「无线索模式」做精度过滤：
#: 真名几乎不会用到表外的字，而「完毕 / 确定 / 金额」这类普通词的尾字
#: 大多不在表内，靠它可以把兜底模式的误报压下来。
_GIVEN_CHARS: str = (
    "伟芳娜敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英华玉兰萍红玲芬燕"
    "彬斌宇浩凯健俊帆帅旭宁欣悦馨怡婷雪梦雨涵佳嘉慧惠倩颖洁琳珊莉莎"
    "薇蓉蕾蕊菲露霜冰清泉溪海江峰岩松柏梅竹菊荷莲桃杏梨樱林森木金银"
    "石田天日月星辰光辉亮阳晴云风波浪潮源流洲龙凤鹏鹤鹰鸿雁虎豹熊鹿"
    "马牛羊贝珠珍琼瑶璐珂琪琦瑾璇珑蔚蓝青绿紫黄白彩虹霓雯霏霖寒暖温"
    "鑫淼垚一二三四五六七八九十百千万亿子某阿买提古丽"
    "为建国军民兵建志学文武才修亮和小晓"
    "智仁义礼信忠孝廉勤俭诚朴谦恭敬慎思念忆怀志愿望慕恩爱德善美妙巧"
    "灵雅素淳质爽快乐欢笑恬逸自飞翔舞歌诗书画琴棋笔墨章曲音声韵鸣响"
    "阔远近迟速疾缓轻薄低短少全双单群生众客商旅游"
    "荣昌盛旺发达顺安康泰福禄寿喜庆祥瑞吉利丰富贵显耀宗祖正直方圆长"
    "永久高深厚重宏广博"
)

#: 边界词里的单字补充：连接词、谓语词、身份标记。
#: 这些字几乎不会出现在名字中间，用来确定名字结尾最可靠。
_BOUNDARY_EXTRA: tuple[str, ...] = (
    # 连接词 / 判断词
    "与", "及", "和", "或", "等", "的", "系", "为", "是", "于", "向", "在",
    "对", "因", "由", "从", "自", "至", "经", "按", "据", "依", "将", "把",
    "被", "让", "使", "未", "无", "有", "已", "曾", "均", "亦", "而", "且",
    "但", "则", "们", "住", "岁", "现", "原",
    # 谓语动词：名字后面直接跟动词时（「赵六诉某某公司」）必须在这里截断
    "诉", "涉嫌", "驾驶", "报案", "控告", "举报", "到案", "抓获", "供述",
    "辩解", "上诉", "申诉", "申请", "要求", "主张",
    "拖欠", "拒绝", "同意", "共计", "每月", "每年", "每日", "应当", "需要",
    "支付", "赔偿", "承担", "履行", "签订", "出具", "借款", "租赁", "购买",
    # 身份 / 性别 / 民族标记
    "男", "女", "汉", "族",
    # 法律文书里的高频谓语与身份名词
    "要求", "请求", "主张", "认为", "辩称", "诉称", "陈述", "表示", "承认",
    "否认", "提交", "提供", "申请", "委托", "代理", "户籍", "身份", "电话",
    "手机", "联系", "地址", "出生", "年龄", "民族", "籍贯", "职务", "单位",
    "现住", "住址", "签名", "签章", "盖章",
    # 当事人姓名后面常直接跟的法律名词：不切开就会把「李四合同纠纷」
    # 切成「李四合」，残留一个「四」——必须在这里截断
    "合同", "协议", "纠纷", "一案", "案件", "诉讼", "起诉", "上诉", "答辩",
    "判决", "裁定", "调解", "仲裁", "公司", "银行", "医院", "学校", "单位",
    "住所", "号码", "职务", "民族", "籍贯", "出生", "年龄", "现住址",
)

#: 元数据类文本块的角色。这类块通常是短标识符（sheet 名、定义名称、
#: 文档属性、批注作者），整块几乎就是实体本身，因此放宽「后继必须是标点」。
_METADATA_ROLES: frozenset[str] = frozenset(
    {"metadata", "sheet_name", "defined_name", "comment_author", "property"}
)

#: metadata 放宽模式允许的左邻字符。
#: 放宽后姓名后面不再要求标点，若左边界也不管，就会在一串汉字中间
#: 硬切出「司买」这种碎片（样例：「张三与杭州某某科技有限公司买卖合同纠纷」）。
#: 规则：左边必须是块首 / 非汉字 / 或这些分隔、连接字符。
_META_LEFT_OK: frozenset[str] = frozenset("与和及、：: ；;（()）【】《》 \u3000-—的年月日第号")

#: 无线索模式的左边界白名单：只有块首或这些分隔符之后才可能是姓名。
#: 真实案例里「内容为」「证明某」「人工智能生」「民事权利」「存在明显」
#: 都被跨词边界切成了 2 字「姓名」，根因就是没有左边界约束。
#: 刻意**不含引号、破折号、顿号、书名号**：引号里的「‘杨林’」、
#: 破折号后的「——万一」、顿号后的「、谷歌」、书名号里的「《全国高校…》」
#: 都是普通词或机构名，不是人名。
_WORD_LEFT_OK: frozenset[str] = frozenset(
    " \t\u3000。；：！？，；:!?,\n"
)

#: 「行首」判定：折行也要看上一行怎么结尾。PDF 会把句子从中间折断，
#: 折行后的第一个字不是词边界（「…专\n业自2021年」曾切出「业自」）。
_LINE_END_PUNCT: frozenset[str] = frozenset("。；：！？，、）】」』》")

#: 2 字候选后面紧跟这些字时，说明它在修饰地名/机构（「杨林校区」「杨林镇」）
_PLACE_CONT: frozenset[str] = frozenset("校镇乡村街路区县市")

#: 一个线索词后最多连续识别几个姓名（合议庭 / 代理人列表够用）
_MAX_NAMES_PER_CUE: int = 6

#: 姓名列表块：整块由空格分隔的 2-4 字纯汉字组成，且至少两段
_NAME_LIST_RE = re.compile(
    r"[\u4e00-\u9fa5]{2,4}(?:[ \t\u3000]+[\u4e00-\u9fa5]{2,4})+"
)

#: 从姓名列表块里切出每一段
_NAME_LIST_TOKEN_RE = re.compile(r"[\u4e00-\u9fa5]{2,4}")

#: 线索词与姓名之间允许出现的间隔字符（需求点名：空格、「：」、全角空格、顿号）
_SEPARATORS: str = " \t\n\r\u3000:：、,，;；"

#: 名字末字不可能是这些字：一出现就说明候选其实是地名或机构后缀
_BAD_LAST: str = "省市县区镇乡村路街巷号院局委办所厂店行社部处科系队组会团族者州"

#: 无线索模式下，候选名字后面允许紧跟的连接字（再往后才是谓语）
_CONNECTORS: str = (
    "与及和或等的系为是于向在对因由从自至经按据依将把被让使未无有已曾"
    "均亦而且但则们住岁男女汉族现原称诉"
    # 姓名后面直接跟动词是常态（张三到庭 / 李四付清），扩充右边界
    "到给跟用拿求找买卖借欠还付送交取做干说讲问答想看听走跑来去应需须"
)

#: 机构 / 地名后缀。线索词后面紧跟这些，说明这里的主体是单位或地点，不是人。
_ORG_OR_PLACE_TAIL: tuple[str, ...] = (
    "股份有限公司", "有限责任公司", "有限公司", "股份公司", "公司",
    "律师事务所", "会计师事务所", "专利代理事务所",
    "人民医院", "中医院", "卫生院", "医院",
    "大学", "学院", "中学", "小学", "学校", "幼儿园",
    "研究院", "研究所", "设计院", "银行", "支行", "分行", "工厂", "厂",
    "超市", "商店", "协会", "商会", "基金会", "合作社", "中心",
    "自治区", "自治州", "自治县", "特别行政区", "省", "市", "区", "县",
    "镇", "乡", "街道", "大道", "路", "街", "巷",
)

#: 停用词：候选串命中这些一律不是姓名。
#: 既有法律术语，也有「安全 / 文明 / 完毕」这类「姓 + 常用名用字」的普通词  ——
#: 它们是无线索模式最主要的误报来源。
_STOPWORDS: frozenset[str] = frozenset(
    """
    本院 本案 本庭 原告 被告 第三人 上诉人 被上诉人 申请 申请人 被申请人
    执行 被执行人 申请执行人 法定代表人 负责人 主要负责人 代理人 委托代理人
    委托诉讼代理人 辩护人 指定辩护人 审判长 审判员 人民陪审员 陪审员 书记员
    法官助理 证人 被害人 犯罪嫌疑人 被告人 当事人 联系人 经办人 签名 签章
    盖章 签字 公司 法院 人民 合同 协议 判决 裁定 通知 证据 材料 理由 事实
    法律 法规 规定 条例 办法 细则 解释 意见 建议 方案 计划 报告 清单 附件
    诉讼 起诉 上诉 答辩 答辩状 起诉状 判决书 裁定书 调解书 调解 仲裁 立案
    开庭 送达 公告 证明 鉴定 评估 登记 批准 审查 监督 管理 经营 服务 技术
    信息 网络 数据 软件 系统 项目 工程 建设 开发 投资 资产 债务 债权 股权
    股东 董事 监事 经理 员工 工资 劳动 税务 发票 银行 账户 转账 支付 结算
    利息 违约金 定金 押金 租金 租赁 买卖 销售 采购 供应 运输 保险 事故 损害
    赔偿 损失 责任 过错 故意 过失 违法 犯罪 刑事 民事 行政 程序 期限 时效
    管辖 回避 复议 申诉 抗诉 检察 公安 司法 公证 律师 法官 检察官 一审 二审
    再审 终审 查封 扣押 冻结 拍卖 变卖 清算 破产 重整 和解 履行 违约 解除
    撤销 无效 有效 成立 生效 终止 到期 逾期 催告 协商 达成 一致 请求 主张
    认为 辩称 诉称 陈述 承认 否认 表示 提交 提供 补充 说明 确认 承诺 保证
    担保 抵押 质押 留置 保全 以上 以下 如下 上述 综上 据此 因此 但是 如果
    因为 所以 已经 正在 尚未 没有 不能 可以 应当 必须 需要 相关 有关 如何
    家属 家属院 亲属 家人 子女 配偶 妻子 丈夫 儿子 女儿 父母 兄弟 姐妹
    权益 权力 权限 权威 安全 安心 安排 和平 和解 于是 向来 何时 成本 成本价
    方向 方式 方面 方法 方案 金额 钱包 金银 白银 黄金 关心 关闭 关于 关系
    伊利 蒙牛 淘宝 天猫 京东 拼多多 美团 饿了么 华为 小米 腾讯 阿里 百度
    字节 抖音 快手 微信 支付宝 顺丰 中通 圆通 韵达 菜鸟 苏宁 国美 沃尔玛
    权利 明显 万一 万分 内容 证明 能力 智能 职业 基于 享有 民事
    谷歌 全国 高校 高等 温和 简章 学费 分数 专业 院校
    本人 其子 其父 其母 其妻 其夫 亲戚 同事 朋友 邻居 监护人 被监护人
    完毕 确定 安全 文明 公平 和平 黄金 白天 明白 任何 全部 方向 方面 金额
    金融 金属 石油 车站 车辆 月份 颜色 石头 马上 方便 方式 方法 时间 时期
    地方 地区 单位 个人 人员 人工 双方 国家 家长 查明 空白 水平 阳光 国民
    队伍 组织 机关 部门 机构 团体 价值 利益 目的 目标 结果 原因 条件 环境
    情形 情况 内容 形式 性质 范围 标准 水平 程度 过程 阶段 方式 途径 措施
    """.split()
)


# --------------------------------------------------------------------------- #
# 检测器
# --------------------------------------------------------------------------- #


class NameDetector:
    """中文姓名检测器。``order=40``：在证件号之后、机构地名之前。"""

    name = "name"
    order = 40

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._singles = frozenset(_SINGLE_SURNAMES)
        self._compounds = frozenset(_COMPOUND_SURNAMES)
        self._given = frozenset(ch for ch in _GIVEN_CHARS if not ch.isspace())
        self._bad_last = frozenset(_BAD_LAST)
        self._connectors = frozenset(_CONNECTORS)
        self._stopwords = _STOPWORDS

        clues = list(settings.rules.name_cues())
        # 配置文件已锁定，这里只做「只增不改」的本地补充：
        # 刑事文书里这些称谓高频，而 rules.yaml 未收录。
        clues.extend(
            [
                "罪犯",
                "自诉人",
                "附带民事诉讼原告人",
                "附带民事诉讼被告人",
                "被告单位",
                "被害单位",
                # 送达 / 笔录 / 表单类文书里的署名栏，rules.yaml 未收录
                "受送达人", "承办人", "送达人", "签收人", "代收人",
                "填报人", "制表人", "审核人", "批准人", "记录人",
                # 合同类文书的主体称谓
                "借款人", "出借人", "出租人", "承租人", "出卖人", "买受人",
                "委托人", "受托人", "保证人", "担保人", "债权人", "债务人",
                "继承人", "收款人", "付款人", "投保人", "被保险人", "受益人",
            ]
        )
        blockers = list(settings.rules.name_blockers())
        self._cue_set = frozenset(clues)
        self._blocker_set = frozenset(blockers)

        # 省市词池：姓名列表块里要排除「长沙」「大同」这类本身就是地名的串
        pools = getattr(settings.lexicon, "pools", {}) or {}
        self._place_words = frozenset(
            w for w in (*pools.get("province", []), *pools.get("city", [])) if w
        )

        # 线索词正则：同一位置优先匹配最长的线索（委托诉讼代理人 优先于 代理人）；
        # 字间允许空白——PDF 表格里的「审 判 员」必须能命中。
        self._cue_re = self._build_alternation(clues, spaced=True)
        # 边界词正则：把线索词、排除词、连接/谓语字一起当作名字的右边界
        self._boundary_re = self._build_alternation(
            clues + blockers + list(_BOUNDARY_EXTRA)
        )

    # ------------------------------------------------------------------ 构造

    @staticmethod
    def _build_alternation(
        words: Sequence[str], spaced: bool = False
    ) -> re.Pattern[str] | None:
        """把词表编译成「长词优先」的交替正则；空表返回 ``None``。

        ``spaced=True`` 时在词内每个字之间允许空白，用于线索词：
        法院 PDF 的表格里「审判员」常被拆成「审　判　员」。
        """
        cleaned = sorted({w for w in words if w}, key=len, reverse=True)
        if not cleaned:
            return None
        if spaced:
            parts = [r"\s*".join(re.escape(ch) for ch in w) for w in cleaned]
        else:
            parts = [re.escape(w) for w in cleaned]
        return re.compile("|".join(parts))

    # ------------------------------------------------------------------ 主流程

    def detect(self, block: TextBlock, ctx: MatchContext) -> Sequence[Entity]:
        """返回本块内的姓名实体，不重叠、偏移在界内、绝不抛异常。"""
        hits: list[Entity] = []
        try:
            text = block.text
            if not text:
                return hits
            masked = masked_ranges(text)
            relaxed = block.role in _METADATA_ROLES
            # PDF 按行切块，会把「专科专|业自」「公司|提交」这类词从中间断开。
            # 上一块以汉字收尾说明是折行，本块开头就不是词边界。
            #
            # 与 pdf-fmt 的接口约定：块若已做过行合并，locator 上带
            # ``merged_lines``（>1 表示合并了几行）。合并块内部已经没有
            # 跨块断词，此时必须**关闭**折行保护，否则正常的块首字会被误弃。
            prev_tail = getattr(ctx, "_name_prev_tail", "")
            prev_merged = bool(getattr(ctx, "_name_prev_merged", False))
            locator = getattr(block, "locator", None)
            merged_lines = int(
                getattr(locator, "merged_lines", getattr(block, "merged_lines", 1)) or 1
            )
            safe_start = (
                merged_lines > 1
                or prev_merged
                or (not prev_tail)
                or (not is_cjk(prev_tail[-1]))
            )
            hits.extend(self._cue_hits(text, masked))
            hits.extend(self._known_hits(text, masked, ctx))
            hits.extend(
                self._word_hits(text, masked, relaxed=relaxed, safe_start=safe_start)
            )
            hits.extend(self._name_list_hits(text, masked))
            # 供下一块判断是否折行（契约里的 MatchContext 没有这个字段，
            # 用私有属性承载，不影响 known/scope/stats 的既有语义）
            setattr(ctx, "_name_prev_tail", text[-2:])
            setattr(ctx, "_name_prev_merged", merged_lines > 1)
        except Exception:
            # 检测器绝不向上抛异常：丢召回比丢一条异常更危险
            pass
        return self._dedupe(hits)

    # ------------------------------------------------------------------ 线索词模式

    def _cue_hits(self, text: str, masked: list[tuple[int, int]]) -> list[Entity]:
        """线索词 + 边界词：本检测器的主力通道（置信度 0.9）。

        一个线索词后面可能跟**多个**姓名（「审理法官：肖芄 赖粤旭 张丽」），
        所以命中第一个之后继续看：只有「姓名 + 分隔符 + 汉字」才继续，
        遇到标点或非汉字立即收手，避免把后文谓语当成第二个人名。
        """
        out: list[Entity] = []
        if self._cue_re is None:
            return out
        n = len(text)
        for match in self._cue_re.finditer(text):
            if within(masked, match.start(), match.end()):
                continue
            # 跳过线索词与姓名之间的间隔（空格、冒号、顿号…），最多 6 字
            j = match.end()
            while j < n and text[j] in _SEPARATORS:
                j += 1
            if j - match.end() > 6 or j >= n or not is_cjk(text[j]):
                continue

            for _ in range(_MAX_NAMES_PER_CUE):
                picked = self._pick_cue_name(text, j, masked)
                if picked is None:
                    break
                cand, start, end = picked
                out.append(
                    Entity(
                        type=EntityType.PERSON,
                        text=cand,
                        start=start,
                        end=end,
                        confidence=0.9,
                        detector=self.name,
                    )
                )
                # 继续找下一个姓名：必须有分隔符，否则交给无线索模式
                k = end
                while k < n and text[k] in _SEPARATORS:
                    k += 1
                if k == end or k - end > 6 or k >= n or not is_cjk(text[k]):
                    break
                j = k
        return out

    def _pick_cue_name(
        self, text: str, j: int, masked: list[tuple[int, int]]
    ) -> tuple[str, int, int] | None:
        """在线索词之后的位置 ``j`` 取一个姓名，取不到返回 ``None``。"""
        n = len(text)
        # 取一段连续的汉字作为候选池
        run_end = j
        while run_end < n and is_cjk(text[run_end]) and run_end - j < 12:
            run_end += 1
        run = text[j:run_end]

        # 找最早出现的边界词，它之前才是名字
        boundary = self._boundary_re.search(run) if self._boundary_re else None
        limit = boundary.start() if boundary is not None else len(run)
        if limit < 2:
            return None

        # 完全没找到边界、而整段以机构/地名后缀结尾：
        # 说明这里的主体是单位或地点（org/place 检测器负责），不是人名。
        if boundary is None and run.endswith(_ORG_OR_PLACE_TAIL):
            return None
        # 边界之后紧跟机构/地名后缀同理（如「张三诉杭州某某公司」，边界是「诉」）
        if boundary is not None and run[limit:].startswith(_ORG_OR_PLACE_TAIL):
            return None

        compound = run[:2] in self._compounds
        cap = 4 if compound else 3
        length = min(limit, cap, len(run))
        stem_len = 2 if compound else 1
        cand = ""
        # 逐字回退：如果截到的第 3（复姓第 4）个字不是常用名用字，
        # 说明边界词没覆盖到后面的动词（「李四拖欠」→「李四拖」），
        # 退一个字再试。两字姓名（姓+1 名）不受此限制，
        # 免得把「王燮」这类生僻名挡掉。
        while length >= 2:
            cand = run[:length]
            if length > stem_len + 1 and cand[stem_len + 1] not in self._given:
                length -= 1
                continue
            if self._valid(cand, given_gate=False):
                break
            length -= 1
        if length < 2 or not cand:
            return None
        if within(masked, j, j + length):
            return None
        return cand, j, j + length

    # ------------------------------------------------------------------ 姓名列表块

    def _name_list_hits(
        self, text: str, masked: list[tuple[int, int]]
    ) -> list[Entity]:
        """整块就是「姓名 姓名 姓名」的列表（合议庭、代理人、送达栏）。

        真实 PDF 里「审理法官：」这个标签与姓名常被拆成两个 TextBlock
        （表格单元格各占一块），只看单块里有没有线索词必然漏检。
        这类块的形态很稳定：整块只有被空格/全角空格分隔的 2-4 字纯汉字，
        且每一段都能通过姓氏、停用词与地名排除校验。命中置信度 0.75。
        """
        out: list[Entity] = []
        stripped = text.strip()
        if not stripped or len(stripped) > 80:
            return out
        if not _NAME_LIST_RE.fullmatch(stripped):
            return out
        lead = len(text) - len(text.lstrip())
        for token in _NAME_LIST_TOKEN_RE.finditer(stripped):
            word = token.group(0)
            if word in self._place_words:
                return out
            if not self._valid(word, given_gate=False):
                return out
            start = lead + token.start()
            end = lead + token.end()
            if within(masked, start, end):
                return out
            out.append(
                Entity(
                    type=EntityType.PERSON,
                    text=word,
                    start=start,
                    end=end,
                    confidence=0.75,
                    detector=self.name,
                )
            )
        return out

    # ------------------------------------------------------------------ 跨块已知姓名

    def _known_hits(
        self, text: str, masked: list[tuple[int, int]], ctx: MatchContext
    ) -> list[Entity]:
        """把前文已确认的人名在本文中再找一遍（置信度 0.85）。

        法律文书常在前文列明当事人，后文只写「张三主张…」而不再带线索词。
        复用 ``MatchContext.known`` 能显著提高跨段召回。
        """
        out: list[Entity] = []
        known = [k for k, v in ctx.known.items() if v is EntityType.PERSON and 2 <= len(k) <= 4]
        if not known or len(known) > 800:
            return out
        for name_text in known:
            pos = text.find(name_text)
            while pos >= 0:
                end = pos + len(name_text)
                if not within(masked, pos, end):
                    out.append(
                        Entity(
                            type=EntityType.PERSON,
                            text=name_text,
                            start=pos,
                            end=end,
                            confidence=0.85,
                            detector=self.name,
                        )
                    )
                pos = text.find(name_text, end)
        return out

    # ------------------------------------------------------------------ 无线索模式

    def _word_hits(
        self,
        text: str,
        masked: list[tuple[int, int]],
        relaxed: bool = False,
        safe_start: bool = True,
    ) -> list[Entity]:
        """无线索兜底：姓氏 + 2-3 字 + 后继为标点/空格/连接词（置信度 0.5）。

        额外要求名字部分全部落在常用名字用字表内，用于压制
        「安全 / 文明 / 完毕」这类普通词。

        ``relaxed=True``（metadata / sheet_name 等块）时取消「后继字符」限制：
        sheet 名往往写成「张三证据清单」，姓名后面紧跟的是普通汉字而不是标点，
        严格模式会整条漏掉。放宽后置信度提到 0.85，并仍然过常用字表——
        靠它把「张三证」这种多切一个字的候选压回「张三」。
        """
        out: list[Entity] = []
        n = len(text)
        for i in range(n):
            ch = text[i]
            if not is_cjk(ch):
                continue
            if within(masked, i, i + 1):
                continue
            # 折行接续块的首字：不是词边界，不做无线索推断。
            # 整块就是一个完整短词（单元格里只放一个姓名）时例外。
            if (
                not relaxed
                and not safe_start
                and i == 0
                and len(text.strip()) > 4
            ):
                continue
            if i > 0:
                prev = text[i - 1]
                if relaxed:
                    # 元数据块：允许「2024年张三…」这类左侧
                    if is_cjk(prev) and prev not in _META_LEFT_OK:
                        continue
                else:
                    # 严格模式：左侧必须是块首 / 空白 / 标点。
                    # 紧贴汉字说明这是从更长词语中间切出来的碎片。
                    if is_cjk(prev):
                        continue
                    if prev.isascii() and prev.isalnum():
                        continue
                    if prev in "\n\r":
                        # 折行：上一行若不是以标点收尾，说明句子被折断，
                        # 这里不是词边界
                        k = i - 2
                        while k >= 0 and text[k] in " \t\u3000":
                            k -= 1
                        if k >= 0 and text[k] not in _LINE_END_PUNCT:
                            continue
                    elif prev not in _WORD_LEFT_OK:
                        continue
            if text[i : i + 2] in self._compounds:
                stem = 2
            elif ch in self._singles:
                stem = 1
            else:
                continue

            run_end = i
            while run_end < n and is_cjk(text[run_end]) and run_end - i < 8:
                run_end += 1
            run = text[i:run_end]
            max_len = 4 if stem == 2 else 3
            min_len = stem + 1

            for length in range(min(max_len, len(run)), min_len - 1, -1):
                cand = run[:length]
                # 「杨林校区」「杨林镇」：2 字候选在修饰地名，不是人名
                if length == 2 and length < len(run) and run[length] in _PLACE_CONT:
                    continue
                # 「全国高等学校」曾切出「全国高」：候选以普通词开头时丢弃
                if not relaxed and any(
                    cand.startswith(w) and len(w) >= 2 for w in self._stopwords
                ):
                    continue
                if relaxed:
                    # 元数据块：不要求后继是标点，靠常用字表回退到正确的长度
                    if not self._valid(cand, given_gate=True):
                        continue
                    out.append(
                        Entity(
                            type=EntityType.PERSON,
                            text=cand,
                            start=i,
                            end=i + length,
                            confidence=0.85,
                            detector=self.name,
                        )
                    )
                    break
                tail = run[length] if length < len(run) else ""
                if tail and tail not in self._connectors:
                    continue
                if not self._valid(cand, given_gate=True):
                    continue
                out.append(
                    Entity(
                        type=EntityType.PERSON,
                        text=cand,
                        start=i,
                        end=i + length,
                        confidence=0.5,
                        detector=self.name,
                    )
                )
                break
        return out

    # ------------------------------------------------------------------ 校验与收尾

    def _valid(self, cand: str, given_gate: bool) -> bool:
        """候选串是否构成一个姓名。"""
        if not (2 <= len(cand) <= 4):
            return False
        if not all(is_cjk(c) for c in cand):
            return False
        if cand in self._stopwords or cand in self._cue_set or cand in self._blocker_set:
            return False
        stem = 2 if cand[:2] in self._compounds else 1
        if stem == 1 and cand[0] not in self._singles:
            return False
        given = cand[stem:]
        if not given:
            return False
        if given_gate and any(c not in self._given for c in given):
            return False
        if cand[-1] in self._bad_last:
            return False
        return True

    @staticmethod
    def _dedupe(hits: list[Entity]) -> list[Entity]:
        """内部消解重叠：置信度高者优先，再按位置输出。"""
        ordered = sorted(hits, key=lambda e: (-e.confidence, e.start, -(e.end - e.start)))
        kept: list[Entity] = []
        for cand in ordered:
            if any(cand.start < k.end and cand.end > k.start for k in kept):
                continue
            kept.append(cand)
        kept.sort(key=lambda e: e.start)
        return kept


def make(settings: Settings) -> Detector:
    """检测器工厂，供 ``detectors/__init__.py`` 自动装配。"""
    return NameDetector(settings)


__all__ = ["NameDetector", "make"]

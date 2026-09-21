"""表格描述生成。

纯数字表格的向量表示很弱，这句描述是它被语义检索命中的主要途径
（docs/05-document-pipeline.md §3.2）。

描述必须只陈述表里有什么。让小模型顺手「总结一下」，等于在检索层就
掺进了未经验证、无来源的判断——而输出中每个论断都必须可溯源。
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

#  预计/预期后面若紧跟着一个名词性短语再遇到分隔符或收尾（"预计负债，"、
#  "预计总投资额、"），它是财务报表/披露文件里的固定名词术语，不是在下论断；
#  只有后面能在碰到分隔符之前找到"将/会/有望"这类将来时态标记，或
#  "增长/下滑/…"这类方向性动词时，才是真正的前瞻性表述。见下面
#  INTERPRETATION_PATTERNS[2] 的具体解释。
_FORWARD_LOOKING_MARKER = (
    "将|会|有望|增长|下滑|下降|上升|提升|回升|改善|恶化|扩大|收窄|盈利|亏损|上涨|下跌"
)

INTERPRETATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 显示/说明/表明/反映/意味着/体现出 只有在用作谓语（后面接宾语，构成一个论断）
    # 时才是「解读」。它们裸接标点或收尾，是被用作名词——最典型的是「说明」：
    # 资产减值准备等附注表格几乎都有一列字面叫「说明」（备注列），
    # MockTableDescriber 把列名原样列进描述，"...、说明，共 N 行。" 这种句子
    # 里的「说明」后面紧跟顿号/逗号，不是在下论断。用零宽断言排除"紧跟标点或
    # 收尾"的用法，四个必测样例（显示公司业绩大幅改善 / 说明增长强劲 /
    # 表明景气度回升 / 预计将继续增长，均后接实词）不受影响，见 test_describe.py
    # 与本文件同目录测试里的 test_neutral_column_name_is_not_rejected。
    re.compile(r"(显示|说明|表明|反映|意味着|体现出)(?=[^，。！？；、\s])"),
    # 审计结论：改善/恶化/强劲/疲软/亮眼/承压/超预期/不及预期 都是纯评价性
    # 形容词，不是《企业会计准则》科目名或披露表格里的固定列名/术语
    # （相较之下"预计负债"「说明」是），裸子串匹配没有发现同类误判风险，未改。
    re.compile(r"(改善|恶化|强劲|疲软|亮眼|承压|超预期|不及预期)"),
    # 预计/预期后面接的是名词短语（财务科目/披露术语，如"预计负债"
    # "预计总投资额"「预期信用损失」）还是前瞻性论断，光看这两个字区分不了
    # ——两种用法后面都紧跟着实词，不能照搬上面「说明」模式那种「后面是否
    # 接实词」的零宽断言。用「碰到逗号/顿号/句末之前有没有出现将来时态标记
    # 或方向性动词」来区分：见 test_describe.py 的
    # test_provision_column_name_is_not_rejected /
    # test_estimated_fundraising_column_name_is_not_rejected（说明列名不误判）
    # 与 test_forecast_with_explicit_period_is_still_rejected（真前瞻表述仍拦截）。
    # 有望/将会/料将本身就是完整的将来时态标记，不是任何常见财务科目/列名的
    # 组成部分，保持裸子串匹配。
    re.compile(rf"(预计|预期)(?=[^，。！？；、\s]*({_FORWARD_LOOKING_MARKER}))|(有望|将会|料将)"),
    # 审计结论：这一条要求"大幅/显著/明显"与"增长/下滑/提升/下降"紧邻组成
    # 四字词，财务表格列名是名词（"增长率""同比增速"），不会以这种
    # 副词+动词的评价性短语整体作为列名出现，未发现同类误判风险，未改。
    re.compile(r"(大幅|显著|明显)(增长|下滑|提升|下降)"),
)

# 审计结论：这份词表同样存在"买入/卖出/增持/减持"作为真实披露表格固定列名
# 的误判风险（如股东增持/减持计划公告表里的"增持数量""减持比例"、龙虎榜
# 公告里的"买入金额""卖出营业部"），风险形态与"预计负债"一致。但这份词表
# 对应的是 CLAUDE.md §0 的合规硬约束（"禁止输出买卖建议…不可协商"），
# 其变更范围由 docs/09-compliance-security.md §1.1 单独定义——权威词表在
# `config/compliance/forbidden_terms.yaml`，"词表的变更需要两人审核
# （CODEOWNERS 规则），因为放宽一个词就是放宽一条合规边界"。放宽这份拦截
# 名单不是本次任务（表格描述解读词误判）授权范围内的判断，也不该在没有
# 第二人复核的情况下顺手做掉，留给专门的合规改动去处理。
FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(买入|卖出|增持|减持|推荐|目标价|评级|建议配置|仓位)"),
)


class DescriptionRejected(ValueError):
    """描述含解读、推断或违禁词。"""


def validate_description(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise DescriptionRejected("描述为空")
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.search(stripped):
            raise DescriptionRejected(f"描述含违禁词: {pattern.pattern}")
    for pattern in INTERPRETATION_PATTERNS:
        if pattern.search(stripped):
            raise DescriptionRejected(f"描述含解读而非陈述: {pattern.pattern}")
    return stripped


@runtime_checkable
class TableDescriber(Protocol):
    model: str
    prompt_version: str

    def describe(self, table_markdown: str, *, title: str, section_path: str) -> str: ...


class MockTableDescriber:
    """确定性描述器。从表头与行数推导，不调模型——测试与离线开发用。"""

    model = "mock"
    prompt_version = "v1"

    def describe(self, table_markdown: str, *, title: str, section_path: str) -> str:
        rows = [r for r in table_markdown.splitlines() if r.strip().startswith("|")]
        if not rows:
            raise DescriptionRejected("不是 Markdown 表格")
        headers = [c.strip() for c in rows[0].strip("| ").split("|") if c.strip()]
        body_count = max(len(rows) - 1, 0)
        leaf_section = section_path.split(">")[-1].strip() or title
        return validate_description(
            f"{leaf_section}表，列为 {'、'.join(headers)}，共 {body_count} 行。"
        )

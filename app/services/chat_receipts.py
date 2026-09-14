"""入队即回执：**不调用模型**的确定性计划与回执文案。

真实反馈：“为什么直到任务结束才响应聊天”。模型意图识别与工具执行都要时间（数秒到数十秒），
所以 HTTP 请求只负责登记消息 + 写一条确定性回执，真正的执行交给 Worker。

回执措辞复用 `parse_agent_command()` 的意图分类：界面按钮命令能被完全确定地识别；
自然语言则用同一套关键词做**轻量多步规划**（例如“先配图，然后自动审核”＝两步），
这样回执本身就能告诉用户“我理解成了哪几步”，而不是等模型跑完才知道。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.models import ConversationIntent
from app.services.agent_commands import parse_agent_command

# 步骤代号：与 dispatcher 的执行器一一对应。
STEP_COLLECT = "collect"
STEP_IMAGE = "image"
STEP_REVIEW = "review"
STEP_DELIVER = "deliver"
STEP_REWRITE = "rewrite"
STEP_CHAT = "chat"

STEP_LABELS = {
    STEP_COLLECT: "采集资讯",
    STEP_IMAGE: "生成配图",
    STEP_REVIEW: "自动审核",
    STEP_DELIVER: "投递公众号草稿",
    STEP_REWRITE: "重写文案",
    STEP_CHAT: "会话回复",
}

# 关键词 → 步骤。匹配位置决定步骤顺序（用户先说哪个就先做哪个）。
_STEP_PATTERNS: tuple[tuple[str, str], ...] = (
    (STEP_COLLECT, r"采集|抓取|获取|搜集|热点|趋势|资讯|新闻|榜单"),
    (STEP_IMAGE, r"配图|封面|封面图|插图"),
    # “待审核草稿”是名词短语，不是命令：只有不是“待审核”时才当成审核步骤。
    (STEP_REVIEW, r"(?<!待)审核|审一遍|把关"),
    (STEP_DELIVER, r"投递|公众号草稿|草稿箱|发布到公众号|发到公众号"),
    (STEP_REWRITE, r"重写|改写"),
)
_STEP_REGEXES = tuple((step, re.compile(pattern)) for step, pattern in _STEP_PATTERNS)

# 问句不是指令：命中这些词时不再声称“已识别为某个动作”，避免回执误导。
_QUESTION_HINT = re.compile(r"为什么|为何|怎么|如何|是不是|能不能|可不可以|什么原因|哪(一|个|些)|吗[？?]?$|[？?]$")
_ATTACHMENT_HINT = re.compile(r"附件|文档|这个文件|文本")

# 界面命令 → 步骤（顺序即执行顺序）。
_COMMAND_STEPS: dict[str, tuple[str, ...]] = {
    "run_auto_review": (STEP_REVIEW,),
    # 只审不改（review_draft）与“重新选择配图”是界面按钮命令：不在这里登记，
    # 回执就会退化到关键词扫描，措辞与卡片标题都可能对不上点下去的动作。
    "review_draft": (STEP_REVIEW,),
    "generate_draft_illustration": (STEP_IMAGE,),
    "publish_to_wechat_draft": (STEP_DELIVER,),
    "reselect_publication_assets": (STEP_DELIVER,),
    "rewrite_draft": (STEP_REWRITE,),
}

# 这些命令不会改动正文：回执文案要区分“审核并改稿”与“只审不改”。
_READ_ONLY_COMMANDS = {"review_draft"}

# 步骤 → 受理运行的初始意图（前端卡片标题与“生成记录”分组要用）。
_STEP_INTENTS = {
    STEP_COLLECT: ConversationIntent.COLLECT_NEWS,
    STEP_IMAGE: ConversationIntent.GENERATE_DRAFT_IMAGE,
    STEP_REVIEW: ConversationIntent.RUN_AUTO_REVIEW,
    STEP_DELIVER: ConversationIntent.PUBLISH_TO_WECHAT_DRAFT,
    STEP_REWRITE: ConversationIntent.REGENERATE_DRAFT,
    STEP_CHAT: ConversationIntent.GENERAL_CHAT,
}


@dataclass(frozen=True)
class ChatPlan:
    """一次用户消息的确定性计划：回执文案 + 有序步骤。"""

    steps: tuple[str, ...]
    text: str
    status: str
    intent: ConversationIntent
    inline: bool = False
    placement: int = 0
    multi: bool = False

    def event_detail(self) -> str:
        if self.multi:
            return "计划：" + " → ".join(STEP_LABELS[step] for step in self.steps)
        return self.text


def _scan_steps(text: str) -> tuple[str, ...]:
    """按出现位置排序的关键词扫描；同一关键词重复出现只算一次。"""
    found: list[tuple[int, str]] = []
    for step, regex in _STEP_REGEXES:
        match = regex.search(text)
        if match:
            found.append((match.start(), step))
    found.sort(key=lambda item: item[0])
    return tuple(step for _, step in found)


def _single_step_receipt(step: str, *, revise: bool = True) -> tuple[str, str]:
    """单步：回执文案 + 运行摘要（都要短，卡片标题直接显示摘要）。"""
    if step == STEP_REVIEW and not revise:
        # 只审不改必须说清“正文不会被改”，否则用户以为文章已经按意见改过了。
        return "收到：只审核当前文章，不改稿。正在处理，完成后我会把意见列给你。", "正在审核（不改稿）"
    return {
        STEP_COLLECT: ("收到：采集资讯。正在获取资料并生成待审核草稿。", "正在采集"),
        STEP_IMAGE: ("收到：生成配图。正在排队生成，完成后我会汇报。", "正在生成配图"),
        STEP_REVIEW: ("收到：自动审核当前文章。正在处理，通过后不会自动发表。", "正在审核"),
        STEP_DELIVER: ("收到：投递公众号草稿（不会发表）。正在处理，完成后我会汇报。", "正在投递"),
        STEP_REWRITE: ("收到：重写文案。正在用已保存的来源证据重写。", "正在重写"),
        STEP_CHAT: ("收到，正在理解你的要求…", "正在理解"),
    }[step]


def _multi_step_text(steps: tuple[str, ...]) -> str:
    listed = "；".join(f"{index}）{STEP_LABELS[step]}" for index, step in enumerate(steps, start=1))
    tail = "按此顺序执行"
    if STEP_IMAGE in steps and STEP_REVIEW in steps and steps.index(STEP_IMAGE) < steps.index(STEP_REVIEW):
        tail = "配图入队后立即审核"
    return f"收到，共 {len(steps)} 步：{listed}。{tail}，完成后我会汇报。"


def command_draft_id(text: str) -> str:
    """界面命令里点名的草稿 id（不是命令时返回空串）。

    回执要按标题称呼文章，但界面的按钮命令只带 id；这里把 id 取出来给调用方查标题用。
    """
    parsed = parse_agent_command(text)
    return parsed.draft_id if parsed is not None else ""


def plan_chat_message(text: str, *, has_attachment: bool = False) -> ChatPlan:
    """确定性规划：界面命令 > 附件提取 > 多步自然语言 > 单步关键词 > 普通对话。"""
    raw = (text or "").strip()
    parsed = parse_agent_command(raw)
    if parsed is not None and parsed.name in _COMMAND_STEPS:
        steps = _COMMAND_STEPS[parsed.name]
        step_text, status = _single_step_receipt(
            steps[0], revise=parsed.name not in _READ_ONLY_COMMANDS
        )
        if parsed.name == "generate_draft_illustration":
            step_text = (
                "收到：生成正文插图。正在排队生成，完成后我会汇报。"
                if parsed.purpose == "inline"
                else step_text
            )
        return ChatPlan(
            steps=steps,
            text=step_text,
            status=status,
            intent=_STEP_INTENTS[steps[0]],
            inline=parsed.purpose == "inline",
            placement=parsed.placement,
        )

    if has_attachment and _ATTACHMENT_HINT.search(raw):
        return ChatPlan(
            steps=(STEP_CHAT,),
            text="收到：正在读取附件内容并生成待审核草稿。",
            status="正在处理附件",
            intent=ConversationIntent.ATTACHMENT_DRAFT,
        )

    steps = () if _QUESTION_HINT.search(raw) else _scan_steps(raw)
    if len(steps) > 1:
        return ChatPlan(
            steps=steps,
            text=_multi_step_text(steps),
            status="正在执行：" + " → ".join(STEP_LABELS[step] for step in steps),
            intent=_STEP_INTENTS[steps[0]],
            multi=True,
        )
    if len(steps) == 1:
        step_text, status = _single_step_receipt(steps[0])
        return ChatPlan(
            steps=steps,
            text=step_text,
            status=status,
            intent=_STEP_INTENTS[steps[0]],
            inline=bool(re.search(r"插图|正文图|第\s*\d+\s*段", raw)),
        )
    return ChatPlan(
        steps=(STEP_CHAT,),
        text=_single_step_receipt(STEP_CHAT)[0],
        status=_single_step_receipt(STEP_CHAT)[1],
        intent=ConversationIntent.GENERAL_CHAT,
    )

"""把界面按钮变成给 Agent 的**明确命令**。

界面不再直连专用接口：按钮发送一条人类可读的命令消息（例如“运行自动审核｜draft=…”），
服务端先做确定性解析——解析成功就直接交给对应的 Agent 工具执行（不调用模型、稳定且省一次调用）；
解析失败才走原来的模型意图识别，于是自然语言与按钮命令共用同一条执行链路。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentCommand:
    """一条来自界面的明确命令。"""

    name: str
    label: str
    draft_id: str = ""
    purpose: str = "cover"
    placement: int = 0
    deliver: bool = False


_DRAFT_PARAM = re.compile(r"draft[=:]\s*([0-9a-fA-F-]{8,})")
_PARAGRAPH_PARAM = re.compile(r"第\s*(\d+)\s*段")
_INLINE_HINT = re.compile(r"正文|插图|inline")
_DELIVER_HINT = re.compile(r"投递|公众号草稿|deliver\s*[=:]\s*true")

# 命令前缀 → 工具名；按顺序匹配，先长后短，避免“审核通过”被“审核”吃掉。
_PREFIXES: tuple[tuple[str, str, str], ...] = (
    ("运行自动审核", "run_auto_review", "运行自动审核"),
    ("自动审核", "run_auto_review", "运行自动审核"),
    # “仅审核”= 只出意见、**不改稿**：指向 review_draft，而不是会长篇改稿的 run_auto_review
    # （真实歧义：按钮写着“仅审核”，点下去却动了正文）。
    ("仅运行审核", "review_draft", "仅运行审核"),
    ("仅审核", "review_draft", "仅运行审核"),
    ("重新审核", "run_auto_review", "重新运行审核"),
    ("审核通过", "approve_draft", "审核通过"),
    ("撤销审核", "revoke_approval", "撤销审核"),
    ("废弃文案", "discard_draft", "废弃文案"),
    ("重写文案", "rewrite_draft", "重写文案"),
    ("重写本次生成的文案", "rewrite_draft", "重写文案"),
    ("重新生成文案", "rewrite_draft", "重写文案"),
    ("重写", "rewrite_draft", "重写文案"),
    # 只换证据：秒级返回且不动正文（用户说“来源更新了，重新抓一下”）。
    ("刷新来源", "refresh_draft_source", "刷新来源"),
    ("更新来源", "refresh_draft_source", "刷新来源"),
    ("重新抓取来源", "refresh_draft_source", "刷新来源"),
    # 只用已存证据重写：不联网重抓，避免换出一份不同的正文。
    ("重新生成正文", "regenerate_draft_body", "重新生成正文"),
    ("按已保存证据重写", "regenerate_draft_body", "重新生成正文"),
    # 与“仅运行审核”配套的按钮：不重新审核，直接按最近一次审核意见改一稿。
    ("按审核意见改稿", "apply_revision_issues", "按审核意见改稿"),
    ("按审核意见修改", "apply_revision_issues", "按审核意见改稿"),
    # 只读：先看队列再决定要不要新建任务（长任务期间最容易做错的就是“又起一个”）。
    ("查任务队列", "list_active_tasks", "查看在跑的任务"),
    ("任务队列", "list_active_tasks", "查看在跑的任务"),
    ("队列状态", "list_active_tasks", "查看在跑的任务"),
    ("现在在跑什么", "list_active_tasks", "查看在跑的任务"),
    ("排版偏好", "show_publication_preferences", "查看长期排版偏好"),
    ("清理素材库", "list_wechat_materials", "盘点素材库可清理项"),
    ("素材库盘点", "list_wechat_materials", "盘点素材库可清理项"),
    ("重新选择配图", "reselect_publication_assets", "重新选择投递配图"),
    ("重选配图", "reselect_publication_assets", "重新选择投递配图"),
    # 发布页「创建 / 更新公众号草稿箱」按钮：素材准备 + 创建（或原地覆盖）远端草稿由同一个后台任务完成，
    # 因此不再需要界面上分两步、也不再直连专用接口（真实反馈：点按钮对话里什么都没发生）。
    ("创建公众号草稿箱", "publish_to_wechat_draft", "创建公众号草稿箱"),
    ("创建公众号草稿", "publish_to_wechat_draft", "创建公众号草稿箱"),
    ("更新公众号草稿箱", "publish_to_wechat_draft", "更新公众号草稿箱"),
    # 只准备/上传素材、**不**创建远端草稿：界面不放按钮，保留为对话里的隐藏能力
    # （想先看看选了哪几张图时用得上）。
    ("准备投递素材", "plan_publication_assets", "准备投递素材"),
    ("上传投递素材", "plan_publication_assets", "准备投递素材"),
    ("投递到公众号草稿", "publish_to_wechat_draft", "投递到公众号草稿箱"),
    ("更新公众号草稿", "publish_to_wechat_draft", "更新公众号草稿"),
    ("重新投递", "publish_to_wechat_draft", "重新投递到公众号草稿箱"),
    ("复用配图", "reuse_draft_assets", "复用原配图"),
    ("复用原配图", "reuse_draft_assets", "复用原配图"),
)

# 配图命令的组合式识别：以“生成/重新生成”开头并提到封面或插图即可，
# 避免为“生成正文第 3 段插图”这类自然写法逐个列举前缀。
_ILLUSTRATION_START = re.compile(r"^(重新)?生成")
_ILLUSTRATION_HINT = re.compile(r"封面|配图|插图|正文|inline")

MAX_COMMAND_CHARS = 200


def parse_agent_command(text: str) -> AgentCommand | None:
    """解析界面命令；无法确定时返回 None，交由模型意图识别处理。"""
    raw = (text or "").strip()
    if not raw or len(raw) > MAX_COMMAND_CHARS:
        return None
    for prefix, name, label in _PREFIXES:
        if not raw.startswith(prefix):
            continue
        draft_match = _DRAFT_PARAM.search(raw)
        command = AgentCommand(
            name=name,
            label=label,
            draft_id=draft_match.group(1) if draft_match else "",
        )
        if name == "run_auto_review":
            return AgentCommand(
                name=name,
                label=label,
                draft_id=command.draft_id,
                deliver=bool(_DELIVER_HINT.search(raw)),
            )
        return command
    # 前缀表优先于配图启发式：“重新生成正文”也以“生成”开头并含“正文”，
    # 先跑启发式会被误判成“生成正文插图”（真实歧义：正文重写变成了配图任务）。
    if _ILLUSTRATION_START.match(raw) and _ILLUSTRATION_HINT.search(raw):
        draft_match = _DRAFT_PARAM.search(raw)
        paragraph = _PARAGRAPH_PARAM.search(raw)
        inline = bool(_INLINE_HINT.search(raw)) or bool(paragraph)
        return AgentCommand(
            name="generate_draft_illustration",
            label=f"生成{'正文插图' if inline else '封面图'}",
            draft_id=draft_match.group(1) if draft_match else "",
            purpose="inline" if inline else "cover",
            placement=int(paragraph.group(1)) if paragraph else 0,
        )
    return None

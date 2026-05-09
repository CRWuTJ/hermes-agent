"""Natural foreground/background admission control for gateway messages."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NaturalDispatchDecision:
    should_background: bool
    reason: str = ""


def _config_enabled(config: Any) -> bool:
    if isinstance(config, dict):
        natural = config.get("natural_dispatch") or {}
    else:
        natural = getattr(config, "natural_dispatch", {}) or {}
    if not isinstance(natural, dict):
        return False
    return bool(natural.get("enabled", False))


def natural_dispatch_enabled(config: Any) -> bool:
    return _config_enabled(config)


def _norm(message: str) -> str:
    return re.sub(r"\s+", " ", str(message or "").strip().lower())


_FOREGROUND_DISCUSSION_MARKERS = (
    "继续讨论", "讨论方案", "我有个想法", "我有一个想法", "你觉得",
    "怎么推进", "如何推进", "下一步方向", "先分析", "不要执行",
    "是否合理", "合不合理", "后台数据怎么同步", "怎么同步",
    "方案是否", "只分析", "不用执行",
)

_FOREGROUND_OVERRIDE_MARKERS = ("后台帮", "后台执行", "后台跑", "放到后台", "转后台")

_BACKGROUND_MARKERS = (
    "后台帮", "后台执行", "后台跑", "放到后台", "转后台", "background",
    "继续优化", "继续检查", "继续补完", "继续实现", "继续修复", "继续改",
    "实现", "修复", "bug", "代码", "改代码", "测试", "跑测试",
    "运行测试", "pytest", "smoke test", "smoke", "维护", "repair",
    "部署", "构建", "编译",
)

_MULTI_STEP_TOKENS = ("检查", "修改", "测试", "验证", "运行", "实现", "修复", "生成", "补完", "优化")

_BARE_CONTINUATION_MESSAGES = {"继续", "继续吧", "继续一下", "continue", "go on"}

_EXECUTION_ACCEPTANCE_RE = re.compile(
    r"(?:"
    r"^同意$|"
    r"同意[，,。 ]*(?:按|执行|开始|做|推进)|"
    r"按(?:推荐|这个|上述|刚才)?方案(?:执行|来|推进|做)|"
    r"按(?:你的|你刚才的)?建议(?:做|执行|推进)|"
    r"(?:开始|继续|就这样|照这个|照此)(?:执行|做|推进)|"
    r"执行吧|"
    r"^(?:go ahead|proceed|please proceed|approved|do it)$"
    r")"
)
_EXECUTION_REJECTION_MARKERS = ("不同意", "先别", "别执行", "不要执行", "不执行", "暂时不要")

_TECHNICAL_CONTEXT_MARKERS = (
    "gateway", "telegram", "foreground", "background", "natural_dispatch",
    "systemd", "service", "worker", "pytest", "root cause", "debug",
    "代码", "测试", "验证", "修复", "排查", "实现", "优化", "编译", ".py",
)

_TECHNICAL_ACTION_CONTEXT_MARKERS = (
    "next step", "run ", "pytest", "test", "verify", "debug", "fix",
    "root cause", "检查", "测试", "验证", "修复", "排查", "实现", "优化", "运行",
)

_MEDIA_NOUN_RE = re.compile(
    r"(?:短视频|视频|动图|动画|图片|图像|海报图?|封面图?|插画|配图|出图|画图|logo|gif|mp4|image|video|photo|poster|thumbnail|animation)"
)
_MEDIA_REQUEST_RE = (
    re.compile(
        r"(?:生成|制作|做|画|设计|出|渲染|剪|剪辑|合成)(?:.{0,30})(?:短视频|视频|动图|动画|图片|图像|海报图?|封面图?|插画|配图|出图|画图|logo|gif|mp4)"
    ),
    re.compile(
        r"(?:帮我|请|给我|来|搞|弄)(?:.{0,30})(?:短视频|视频|动图|动画|图片|图像|海报图?|封面图?|插画|配图|出图|画图|logo|gif|mp4)"
    ),
    re.compile(r"(?:generate|create|make|draw|render)(?:.{0,50})(?:image|video|photo|poster|thumbnail|animation|gif|mp4)"),
)
_BARE_MEDIA_REQUEST_RE = re.compile(
    r"^(?:出图|发图|给我图|图呢|我的图呢|来张图|来一张图|生成图|生成图片|画图|draw image|make image)(?:[啊呀吧嘛吗呢？?！!。\s]*)$"
)
_MEDIA_CAPABILITY_QUESTION_RE = re.compile(
    r"(?:能不能|可不可以|是否(?:能|可以)?|能|可以|会|支持)(?:.{0,20})(?:生成|制作|做|画|设计|出图|画图)(?:.{0,20})(?:短视频|视频|动图|动画|图片|图像|海报图?|封面图?|插画|配图|图|logo|gif|mp4)(?:了)?[吗么?？]?$"
)
_FOREGROUND_WORK_QUESTION_RE = re.compile(
    r"(?:怎么|如何|为什么|为何|哪里|哪儿|什么原因|should|how should|why|what caused|what is causing)"
    r"(?:.{0,40})"
    r"(?:修复|实现|测试|验证|部署|构建|编译|优化|fix|implement|test|verify|deploy|build|compile|optimize)"
)


def _is_media_capability_question(text: str) -> bool:
    return bool(_MEDIA_CAPABILITY_QUESTION_RE.search(text))


def _is_media_generation_request(text: str) -> bool:
    if _BARE_MEDIA_REQUEST_RE.search(text):
        return True
    if not _MEDIA_NOUN_RE.search(text):
        return False
    return any(pattern.search(text) for pattern in _MEDIA_REQUEST_RE)


def is_bare_continuation_message(message: str) -> bool:
    """Return true for short standalone continuation requests only."""
    return _norm(message).rstrip(".。!！?？") in _BARE_CONTINUATION_MESSAGES


def is_execution_acceptance_message(message: str) -> bool:
    """Return true when a short reply appears to approve executing the prior plan."""
    text = _norm(message).rstrip(".。!！")
    if not text or any(marker in text for marker in _EXECUTION_REJECTION_MARKERS):
        return False
    if text.endswith(("?", "？")) or re.search(r"(?:吗|么|嘛)$", text):
        return False
    return bool(_EXECUTION_ACCEPTANCE_RE.search(text))


def _has_technical_execution_context(context: Iterable[str] | None) -> bool:
    if not context:
        return False
    text = _norm(" ".join(str(item or "") for item in context))
    if not text:
        return False
    has_technical_marker = any(marker in text for marker in _TECHNICAL_CONTEXT_MARKERS)
    has_action_marker = any(marker in text for marker in _TECHNICAL_ACTION_CONTEXT_MARKERS)
    return has_technical_marker and has_action_marker


def classify_natural_dispatch(
    message: str,
    config: Any | None = None,
    *,
    context: Iterable[str] | None = None,
) -> NaturalDispatchDecision:
    if not _config_enabled(config):
        return NaturalDispatchDecision(False, "disabled")
    text = _norm(message)
    if not text:
        return NaturalDispatchDecision(False, "empty")

    if any(marker in text for marker in _FOREGROUND_DISCUSSION_MARKERS):
        if not any(marker in text for marker in _FOREGROUND_OVERRIDE_MARKERS):
            return NaturalDispatchDecision(False, "discussion")

    if _is_media_capability_question(text):
        return NaturalDispatchDecision(False, "media_capability_question")

    if _is_media_generation_request(text):
        return NaturalDispatchDecision(False, "media_generation")

    if _FOREGROUND_WORK_QUESTION_RE.search(text):
        return NaturalDispatchDecision(False, "work_question")

    if is_execution_acceptance_message(text):
        if _has_technical_execution_context(context):
            return NaturalDispatchDecision(True, "accepted_execution_context")
        return NaturalDispatchDecision(False, "execution_acceptance_without_context")

    for marker in _BACKGROUND_MARKERS:
        if marker in text:
            return NaturalDispatchDecision(True, marker)

    hits = sum(1 for token in _MULTI_STEP_TOKENS if token in text)
    if hits >= 3 or (hits >= 2 and ("并" in text or "、" in text or "然后" in text)):
        return NaturalDispatchDecision(True, "multi_step")

    if is_bare_continuation_message(text) and _has_technical_execution_context(context):
        return NaturalDispatchDecision(True, "continuation_context")

    return NaturalDispatchDecision(False, "lightweight")

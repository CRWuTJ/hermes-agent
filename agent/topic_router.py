"""Topic routing for Telegram DM conversations.

This module implements automatic session routing based on message content,
allowing natural conversation flow without manual /resume /new /title commands.

The router analyzes each incoming message and decides:
- stay: continue in current session (default for most messages)
- resume: switch to an existing titled session that matches the topic
- new: reset to a fresh session (for topic shifts or resets)

Configuration (in config.yaml):

  topic_routing:
    enabled: true
    mode: keyword  # keyword | llm (future: semantic clustering)
    new_session_keywords:
      - "新话题"
      - "换个话题"
      - "换个主题"
      - "切换话题"
      - "换个方向"
    resume_threshold: 0.7  # confidence threshold for resume
    max_candidates: 10     # max titled sessions to consider
"""

from dataclasses import dataclass
from typing import Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class TopicRouteResult:
    """Result of topic routing decision."""
    action: str  # "stay" | "resume" | "new"
    target_session_id: Optional[str] = None
    target_title: Optional[str] = None
    confidence: float = 1.0
    reason: str = ""


def route_telegram_dm_turn(
    message: str,
    current_session_id: str,
    current_title: Optional[str],
    candidates: list[dict],
    config: dict,
) -> TopicRouteResult:
    """Route a Telegram DM message to the appropriate session.

    Args:
        message: The incoming message text
        current_session_id: Current session ID
        current_title: Current session title (if any)
        candidates: List of candidate sessions with "id" and "title" keys
        config: Routing configuration from config.yaml

    Returns:
        TopicRouteResult with action, target, confidence, and reason
    """
    if not message:
        return TopicRouteResult(action="stay", reason="empty message")

    mode = config.get("mode", "keyword")

    # 1. Check for explicit new session keywords
    new_keywords = config.get("new_session_keywords", [])
    for kw in new_keywords:
        if kw in message:
            # Try to extract a title from the message
            title = _extract_title(message, kw)
            return TopicRouteResult(
                action="new",
                target_title=title,
                confidence=1.0,
                reason=f"keyword '{kw}' triggered new session"
            )

    # 2. Keyword-based resume matching
    if mode == "keyword" and candidates:
        threshold = config.get("resume_threshold", 0.7)
        current_score = _keyword_overlap_score(message, current_title or "") if current_title else 0.0
        best_match = None
        best_score = 0.0

        for cand in candidates:
            if cand.get("id") == current_session_id:
                continue
            topic_text = _candidate_topic_text(cand)
            if not topic_text:
                continue

            # Simple keyword overlap scoring
            score = _keyword_overlap_score(message, topic_text)
            if score > best_score and score >= threshold:
                best_score = score
                best_match = cand

        if current_score >= threshold and (best_match is None or current_score >= best_score):
            return TopicRouteResult(
                action="stay",
                confidence=current_score,
                reason="current session matches topic",
            )

        if best_match:
            return TopicRouteResult(
                action="resume",
                target_session_id=best_match["id"],
                target_title=best_match.get("title"),
                confidence=best_score,
                reason=f"matched title '{best_match.get('title')}'"
            )

    # 3. Default: stay in current session
    return TopicRouteResult(action="stay", reason="no routing triggered")


def _candidate_topic_text(candidate: dict) -> str:
    """Build the searchable topic text for a candidate session."""
    parts = []
    for key in ("title", "summary", "description", "workstream"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    keywords = candidate.get("keywords")
    if isinstance(keywords, (list, tuple, set)):
        parts.extend(str(item).strip() for item in keywords if str(item).strip())
    return " ".join(parts)


def _extract_title(message: str, trigger_keyword: str) -> Optional[str]:
    """Extract a potential title from a new session message.

    E.g., "新话题：关于 AI" -> "关于 AI"
    """
    # Try common title delimiters
    for delim in ["：", ":", " - ", " – ", "—"]:
        if delim in message:
            parts = message.split(delim, 1)
            if len(parts) > 1:
                title = parts[1].strip()
                # Remove trigger keyword if present
                title = title.replace(trigger_keyword, "").strip()
                if title:
                    return title[:100]  # Cap at 100 chars

    return None


def _keyword_overlap_score(message: str, title: str) -> float:
    """Compute a keyword overlap score between a message and session title.

    The router needs to work well for mixed Chinese/English titles like
    ``MCP 服务器`` or ``Telegram DM topic routing`` where users may only mention
    the distinctive English fragment in follow-up turns.
    """
    import re

    english_stopwords = {
        "a",
        "an",
        "and",
        "for",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
    }
    chinese_stopwords = {
        "的",
        "是",
        "在",
        "了",
        "和",
        "与",
        "或",
        "有",
        "不",
        "我",
        "你",
        "他",
        "她",
        "它",
        "这",
        "那",
        "关于",
        "讨论",
        "内容",
    }

    def _normalize_english_token(token: str) -> str:
        token = token.lower()
        if len(token) > 4 and token.endswith("ies"):
            return token[:-3] + "y"
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            return token[:-1]
        return token

    def tokenize(text: str) -> tuple[set[str], set[str]]:
        tokens: set[str] = set()
        english_tokens: set[str] = set()

        for raw_word in re.findall(r"[a-zA-Z0-9]+", text):
            word = _normalize_english_token(raw_word)
            if not word or word in english_stopwords:
                continue
            tokens.add(word)
            english_tokens.add(word)

        cn_segments = re.findall(r"[\u4e00-\u9fff]+", text)
        for seg in cn_segments:
            tokens.add(seg)
            for char in seg:
                tokens.add(char)
            for i in range(len(seg) - 1):
                tokens.add(seg[i:i+2])

        return tokens, english_tokens

    msg_words, msg_en_words = tokenize(message)
    title_words, title_en_words = tokenize(title)

    if not title_words:
        return 0.0

    overlap = msg_words & title_words
    score = len(overlap) / len(title_words)

    distinctive = title_words - chinese_stopwords - english_stopwords
    if distinctive and (distinctive & msg_words):
        score = min(1.0, score * 1.5)

    english_overlap = msg_en_words & title_en_words
    if title_en_words and english_overlap == title_en_words:
        score = max(score, 0.92)
    elif len(english_overlap) >= 2 and msg_en_words and english_overlap == msg_en_words:
        score = max(score, 0.82)

    try:
        from agent.harness import classify_workstream

        message_workstream = classify_workstream(message)
        title_workstream = classify_workstream(title)
        if (
            message_workstream
            and title_workstream
            and message_workstream == title_workstream
            and message_workstream != "general"
        ):
            score = max(score, 0.86)
    except Exception:
        pass

    return score

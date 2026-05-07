#!/usr/bin/env python3
"""
LLM-Powered Emotion Detection

Uses local Ollama (qwen3.5:4b) for nuanced detection (sarcasm, understatement,
mixed signals). Falls back to keyword matching if Ollama is unavailable.

Design: lightweight — sends minimal context, expects structured JSON response.
Thinking mode disabled for fast, direct responses.
"""

import json
import logging
import os
import sys
from typing import Optional

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

logger = logging.getLogger(__name__)


# Keyword fallback (kept as safety net when Ollama unavailable)
EMOTION_KEYWORDS = {
    "frustrated": {
        "words": ["still broken", "not working", "wrong again", "i told you", "frustrat",
                  "annoying", "why does", "keeps happening", "this is broken", "wtf",
                  "come on", "seriously", "how many times"],
        "valence": -0.7, "arousal": 0.7,
    },
    "angry": {"words": ["unacceptable", "terrible", "awful", "hate", "worst", "furious"],
              "valence": -0.9, "arousal": 0.9},
    "disappointed": {"words": ["expected better", "thought you", "should have", "missed",
                               "not what i asked", "close but", "almost", "not good enough"],
                     "valence": -0.5, "arousal": 0.3},
    "urgent": {"words": ["production", "down", "asap", "urgent", "customers",
                         "broken in prod", "hotfix", "immediately", "critical"],
               "valence": -0.3, "arousal": 0.9},
    "curious": {"words": ["what if", "how about", "could we", "interesting", "wonder",
                          "explore", "brainstorm", "let's try", "what do you think"],
                "valence": 0.3, "arousal": 0.5},
    "happy": {"words": ["perfect", "exactly", "great", "love it", "amazing", "awesome",
                        "nice", "well done", "brilliant", "nailed it"],
              "valence": 0.8, "arousal": 0.6},
    "excited": {"words": ["let's go", "can't wait", "this is huge", "game changer",
                          "incredible", "revolutionary"],
                "valence": 0.8, "arousal": 0.9},
    "teaching": {"words": ["because", "the reason", "what matters is", "the point is",
                           "understand that", "let me explain"],
                 "valence": 0.4, "arousal": 0.4},
    "satisfied": {"words": ["good", "works", "fine", "ok nice", "makes sense", "agreed",
                            "solid", "ship it", "yes always", "go for it"],
                  "valence": 0.5, "arousal": 0.3},
    "directing": {"words": ["make it so", "i want you to", "now do", "in parallel",
                            "go ahead and", "next step", "start working on", "implement"],
                  "valence": 0.2, "arousal": 0.5},
    "encouraging": {"words": ["this is how you grow", "high standard", "push yourself",
                              "you can do", "level up", "keep pushing", "believe in"],
                    "valence": 0.6, "arousal": 0.5},
    "delegating": {"words": ["whatever you decide", "up to you", "your call",
                             "if you think", "you decide", "trust your judgment",
                             "i'll leave it to you"],
                   "valence": 0.5, "arousal": 0.3},
}


_NEGATION_PATTERNS = ["not ", "not that ", "no longer ", "never ", "quite the opposite"]


def _is_negated(text_lower: str, keyword: str) -> bool:
    """Check if a keyword match is negated by preceding words."""
    idx = text_lower.find(keyword)
    if idx < 0:
        return False
    prefix = text_lower[max(0, idx - 20):idx]
    return any(neg in prefix for neg in _NEGATION_PATTERNS)


def detect_keyword_fallback(text: str) -> dict:
    """Keyword-based fallback when LLM is unavailable."""
    text_lower = text.lower()

    scores = {}
    for emotion, sig in EMOTION_KEYWORDS.items():
        score = sum(1 for w in sig.get("words", []) if w in text_lower and not _is_negated(text_lower, w))
        if score > 0:
            scores[emotion] = score

    if not scores:
        return {
            "emotion": "neutral", "valence": 0.0, "arousal": 0.2,
            "confidence": 0.3, "method": "keyword_fallback",
            "reasoning": "No keyword signals detected",
        }

    best = max(scores, key=scores.get)
    sig = EMOTION_KEYWORDS[best]
    return {
        "emotion": best,
        "valence": sig["valence"],
        "arousal": sig.get("arousal", 0.5),
        "confidence": min(0.5, scores[best] * 0.2),
        "method": "keyword_fallback",
        "reasoning": f"Keywords matched: {scores[best]} for '{best}'",
    }


def detect_emotion(text: str, context: Optional[list[str]] = None) -> dict:
    """
    Detect emotion using local LLM, with keyword fallback.

    Args:
        text: The message to analyze
        context: Optional list of recent messages for context (last 2-3)

    Returns:
        {emotion, valence, arousal, confidence, method, reasoning}
    """
    if len(text.strip()) < 10:
        return {
            "emotion": "neutral", "valence": 0.0, "arousal": 0.2,
            "confidence": 0.3, "method": "too_short",
            "reasoning": "Message too short for reliable detection",
        }

    try:
        result = _detect_with_llm(text, context)
        if result:
            return result
    except Exception as e:
        logger.warning(f"LLM emotion detection failed: {e}")

    return detect_keyword_fallback(text)


def _detect_with_llm(text: str, context: Optional[list[str]] = None) -> Optional[dict]:
    """Call GPU server for emotion detection. Fast, free, no API key."""
    context_block = ""
    if context:
        recent = "\n".join(f"- {m[:150]}" for m in context[-3:])
        context_block = f"\nRecent conversation:\n{recent}\n"

    prompt = f"""You are an emotion classifier. Analyze this message from a CTO to their AI assistant.
{context_block}
Message: "{text}"

CRITICAL — distinguish DIRECTIVE tone from NEGATIVE tone:
- "Make it so X" / "I want you to X" / "Now do X" = DIRECTING (neutral-positive instructions)
- "You should X, this is how you grow" / "have high standards" = ENCOURAGING (mentorship)
- "Whatever you decide" / "if you think X then do Y" / "your call" = DELEGATING (trust)
- "Ok, now do X" / "in parallel, do Y" = DIRECTING or SATISFIED
- "Please do" / "Yes go ahead" / "always go for the best" = SATISFIED
- "Can you reflect on X?" / "what do you think?" = CURIOUS
- Negated negatives: "not frustrated" / "quite the opposite" = look at the ACTUAL sentiment

Reserve FRUSTRATED only for ACTUAL frustration signals:
- Repetition complaints: "I told you", "how many times", "again?!"
- ALL CAPS anger or explicit negative words: "wtf", "broken", "not working"
- Exasperated tone with negative judgment about past failures

Other rules:
- "Oh perfect, another broken X" = sarcastic FRUSTRATION, not happiness
- "This could be better" = DISAPPOINTMENT, not neutral
- Mixed signals: pick the DOMINANT emotion

Valid emotions: frustrated, angry, disappointed, urgent, curious, happy, excited, teaching, directing, encouraging, delegating, satisfied, neutral

Respond with ONLY a JSON object in this format (no other text):
{{"emotion": "<one from list above>", "valence": -1.0 to 1.0, "arousal": 0.0 to 1.0, "confidence": 0.0 to 1.0, "reasoning": "<brief reason>"}}

Your JSON response:"""

    try:
        from brain.platform.gpu_client import get_client
        result = get_client().generate(
            prompt=prompt, max_tokens=150,
            temperature=0.1, think=False, fallback_policy="auto",
        )

        if not result:
            return None

        data = _extract_json_from_response(result.strip())
        if data and "emotion" in data:
            data["emotion"] = data["emotion"].lower().strip()
            for field in ("valence", "arousal", "confidence"):
                if field in data:
                    try:
                        data[field] = float(data[field])
                    except (ValueError, TypeError):
                        data[field] = 0.5
            data["method"] = "llm"
            return data

    except Exception as e:
        logger.debug(f"LLM emotion detection failed: {e}")

    return None


def _extract_json_from_response(text: str) -> Optional[dict]:
    """Extract JSON object from LLM response that may contain surrounding text."""
    import re

    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r'\{[^{}]*\}', text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return None


def sense_emotion(message: str, attributed_to: str = "operator") -> dict:
    """Detect emotion and write snapshot to DB if significant.

    Shared entry point for all surfaces (CLI, Cortex, API).
    Returns the emotion dict with an optional ``snapshot_id`` key.
    """
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    emotion = detect_emotion(message)

    if emotion.get("confidence", 0) >= 0.3 and emotion.get("emotion") != "neutral":
        try:
            with UnitOfWork() as uow:
                result = uow.session.execute(text(
                    "INSERT INTO emotional_snapshots "
                    "(session_date, valence, arousal, label, trigger_summary, attributed_to) "
                    "VALUES (CURRENT_DATE, :valence, :arousal, :label, :trigger_summary, :attributed_to) "
                    "RETURNING id"
                ), {
                    "valence": emotion["valence"],
                    "arousal": emotion["arousal"],
                    "label": emotion["emotion"],
                    "trigger_summary": message[:200],
                    "attributed_to": attributed_to,
                })
                emotion["snapshot_id"] = result.mappings().first()["id"]
        except Exception as exc:
            logger.warning("Failed to write emotional snapshot: %s", exc)

    return emotion

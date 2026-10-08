"""OpenAI-backed classifier for 'How did you hear about us' free text.

The classification rules live in ``system_prompt.md`` - a human-readable file
you can edit without touching this code. Hear Source is locked to the enum in
``taxonomy.py`` by the request's JSON schema, so the model cannot return a value
your picklist does not have.
"""
import json
import os
import threading

from openai import OpenAI

from . import taxonomy

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Reasoning models spend completion tokens on thinking before they emit the
# answer, and that reasoning counts against this cap. The answer itself is ~25
# tokens; a genuinely ambiguous multi-source answer has been measured at 330,
# nearly all of it reasoning. Too low a cap burns the whole budget on reasoning
# and returns nothing. Unused headroom is not billed, so keep this generous.
MAX_OUTPUT_TOKENS = 4000

# ---------------------------------------------------------------------------
# System prompt loading (cached, reloaded when the .md file changes)
# ---------------------------------------------------------------------------
_prompt_lock = threading.Lock()
_prompt_cache: dict = {"mtime": None, "text": ""}


def get_system_prompt(extra_instructions: str = "") -> str:
    """Return the contents of system_prompt.md, plus any per-campaign additions."""
    with _prompt_lock:
        mtime = os.path.getmtime(taxonomy.SYSTEM_PROMPT_FILE)
        if _prompt_cache["mtime"] != mtime:
            _prompt_cache["text"] = taxonomy.load_system_prompt()
            _prompt_cache["mtime"] = mtime
        prompt = _prompt_cache["text"]

    if extra_instructions.strip():
        prompt += (
            "\n\n## 6. Additional campaign-specific rules\n\n"
            "These override anything above.\n\n" + extra_instructions.strip()
        )
    return prompt


def classify(
    raw_text: str,
    model: str = "gpt-5.6-luna",
    temperature: float = 0.0,
    extra_instructions: str = "",
) -> dict:
    """Classify one free-text answer.

    Always returns a dict with keys: hear_source, hear_source_detail,
    raw_model_answer, coerced, skipped.
    Raises only on a genuine API failure - the caller decides what to log.

    Every input goes to the model, including blanks and non-answers -
    system_prompt.md §4 decides what counts as Unknown, not this code. The one
    exception is a completely empty string, which has nothing to send.
    """
    raw_text = (raw_text or "").strip()

    if not raw_text:
        return {
            "hear_source": taxonomy.FALLBACK_SOURCE,
            "hear_source_detail": taxonomy.FALLBACK_DETAIL,
            "raw_model_answer": "",
            "coerced": False,
            "skipped": True,
        }

    # max_completion_tokens is the current parameter name and works on both the
    # gpt-4o generation and newer models, which reject the old max_tokens.
    kwargs = {
        "model": model,
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
        "response_format": {"type": "json_schema", "json_schema": taxonomy.json_schema()},
        "messages": [
            {"role": "system", "content": get_system_prompt(extra_instructions)},
            {"role": "user", "content": raw_text[:2000]},
        ],
    }
    # Newer models only accept the default temperature and reject the parameter
    # outright, so temperature=None means "don't send it".
    if temperature is not None:
        kwargs["temperature"] = temperature

    resp = client.chat.completions.create(**kwargs)
    message = resp.choices[0].message

    # A strict schema can still be refused by the safety layer.
    if getattr(message, "refusal", None):
        return {
            "hear_source": taxonomy.FALLBACK_SOURCE,
            "hear_source_detail": taxonomy.FALLBACK_DETAIL,
            "raw_model_answer": f"refusal: {message.refusal}",
            "coerced": True,
            "skipped": False,
        }

    # A reasoning model that runs out of budget mid-thought returns an empty
    # answer with finish_reason "length" (or a 400 on some API versions). Fail
    # loudly rather than writing a fabricated Unknown onto the lead.
    if resp.choices[0].finish_reason == "length":
        used = getattr(resp.usage, "completion_tokens", "?")
        raise RuntimeError(
            f"model hit the output limit before answering ({used} tokens used of "
            f"{MAX_OUTPUT_TOKENS}); raise MAX_OUTPUT_TOKENS in openai_functions.py")

    raw_answer = (message.content or "").strip()

    try:
        parsed = json.loads(raw_answer)
    except json.JSONDecodeError:
        return {
            "hear_source": taxonomy.FALLBACK_SOURCE,
            "hear_source_detail": taxonomy.FALLBACK_DETAIL,
            "raw_model_answer": raw_answer,
            "coerced": True,
            "skipped": False,
        }

    source, detail, was_clean = taxonomy.validate(
        parsed.get("hear_source", ""), parsed.get("hear_source_detail", "")
    )

    return {
        "hear_source": source,
        "hear_source_detail": detail,
        "raw_model_answer": raw_answer,
        "coerced": not was_clean,
        "skipped": False,
    }

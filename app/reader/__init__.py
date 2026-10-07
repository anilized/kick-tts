"""Reader factory: the LLM reader when an Anthropic key is configured, otherwise the rules cleaner."""
from __future__ import annotations

from app.interfaces import Reader


def get_reader(settings) -> Reader:
    from app.reader.rules import RulesReader

    rules = RulesReader(settings)
    key = getattr(settings, "ANTHROPIC_API_KEY", None)
    secret = key.get_secret_value() if hasattr(key, "get_secret_value") else key
    if not secret:
        return rules

    from app.reader.llm import AnthropicReader

    return AnthropicReader(settings, rules)

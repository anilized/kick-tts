"""Reader factory. TASK-102 replaces the stub with RulesReader / AnthropicReader selection."""
from __future__ import annotations

from app.interfaces import Reader


def get_reader(settings) -> Reader:
    from app.reader.fake import FakeReader

    return FakeReader()

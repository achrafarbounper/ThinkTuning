"""Copilot suggestion feedback persisted in MongoDB."""

from __future__ import annotations

from app.infrastructure.persistence.mongodb import MongoFeedbackStore

AGENT_COPILOT_PATH = "mongodb://configured/copilot_feedback"


class FeedbackStore(MongoFeedbackStore):
    def __init__(self, path: str | None = None, provider=None):
        super().__init__(provider=provider)


_feedback_store: FeedbackStore | None = None


def get_feedback_store() -> FeedbackStore:
    global _feedback_store
    if _feedback_store is None:
        _feedback_store = FeedbackStore()
    return _feedback_store


def reset_feedback_store(path: str | None = None) -> FeedbackStore:
    global _feedback_store
    _feedback_store = FeedbackStore(path)
    return _feedback_store

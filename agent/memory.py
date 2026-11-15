from dataclasses import asdict, dataclass, field
from typing import Literal

from .workflow_models import WorkflowMemory


@dataclass
class ConversationTurn:
    user: str
    assistant: str
    at: str | None = None


# Older files contain two-item arrays with no recorded time.
type StoredTurn = ConversationTurn | tuple[str, str]


def turn_data(turn: StoredTurn) -> dict:
    if isinstance(turn, ConversationTurn):
        return asdict(turn)
    return asdict(ConversationTurn(*turn))


@dataclass
class AgentMemory:
    chat_history: list[StoredTurn] = field(default_factory=list)
    chat_summary: str = ""
    unsummarized_chat: list[StoredTurn] = field(default_factory=list)
    last_route: Literal["chat", "meeting"] = "chat"
    meeting_history: list[StoredTurn] = field(default_factory=list)
    workflow: WorkflowMemory = field(default_factory=WorkflowMemory)

import os
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import TypeAdapter

from agent.memory import AgentMemory


def file_key(value: str) -> str:
    # Case-insensitive Windows filenames must still distinguish different IDs.
    label = re.sub(r"[^A-Za-z0-9_-]", "_", value)[:32]
    return f"id-{label}-{sha256(value.encode('utf-8')).hexdigest()}"


@dataclass
class ConversationMemory:
    person_id: str | None
    agent: AgentMemory


FORMAT = TypeAdapter(ConversationMemory)


def load(path: Path, person_id: str | None) -> AgentMemory | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    memory = FORMAT.validate_json(data)
    return memory.agent if memory.person_id == person_id else None


def save(path: Path, person_id: str | None, memory: AgentMemory) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = FORMAT.dump_json(ConversationMemory(person_id, memory), indent=2) + b"\n"
    handle = NamedTemporaryFile(dir=path.parent, delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def reset(root: Path) -> None:
    if root.is_symlink() or root.is_junction():
        raise OSError("记忆目录不能通过符号链接或目录联接清空")
    groups = root / "groups"
    scopes = [root / "single"]
    if not (groups.is_symlink() or groups.is_junction()):
        scopes.extend(groups.glob("id-*"))
    for scope in scopes:
        if scope.is_symlink() or scope.is_junction():
            continue
        for path in scope.glob("id-*.txt"):
            if re.fullmatch(r"id-[A-Za-z0-9_-]{0,32}-[0-9a-f]{64}\.txt", path.name):
                path.unlink()

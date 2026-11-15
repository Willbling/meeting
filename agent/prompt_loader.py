from pathlib import Path


PROMPT_DIR = Path(__file__).with_name("prompts")


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


GROUP_CONTEXT = load_prompt("group_context").rstrip("\n")

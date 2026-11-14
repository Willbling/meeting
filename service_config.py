import tomllib
from pathlib import Path

from pydantic import TypeAdapter

from api.models import PersonId


DEFAULT_CONFIG = Path(__file__).resolve().with_name("services.toml")


def validate_person_id(person_id: str) -> str:
    return TypeAdapter(PersonId).validate_python(person_id)


def test_person_id(config_file: Path) -> str:
    with config_file.open("rb") as config:
        return validate_person_id(tomllib.load(config)["testing"]["person_id"])

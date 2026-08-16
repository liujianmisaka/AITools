from __future__ import annotations

from pydantic import BaseModel, ConfigDict


def _to_camel_case(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class JsonModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel_case,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

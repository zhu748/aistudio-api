"""Request-local name hints for replayed compatibility tool results."""

from collections.abc import Mapping

from aistudio_api.api.schemas import GeminiFunctionResponse, GeminiPart


def index_call_name(names: dict[str, str | None], call_id: str, name: str) -> None:
    """Index original names only; None means this ID has conflicting owners."""
    if not call_id or not name:
        return
    if names.setdefault(call_id, name) != name:
        names[call_id] = None


def result_part(
    names: Mapping[str, str | None], result: GeminiFunctionResponse
) -> GeminiPart:
    name = names.get(result.id or "", result.name)
    if name is None:
        # Retain the caller's label (or "unknown") for the orphan transcript.
        # Neither that label nor a single remaining ID-less call proves ownership.
        result._compat_owner.exact_id_only = True
    else:
        result.name = name
    return GeminiPart(functionResponse=result)

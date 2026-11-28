"""Schema encoding must not leave completed request inputs waiting for cyclic GC."""

import gc
import json
import sys
import weakref

import pytest

from aistudio_api.application import schema_wire_codec as codec
from aistudio_api.infrastructure.gateway.wire_spec import SchemaIndex


@pytest.mark.skipif(
    sys.implementation.name != "cpython", reason="tests refcount release"
)
@pytest.mark.parametrize("adapter", ["response", "declaration"])
@pytest.mark.parametrize("recursive", [False, True])
def test_schema_released_with_wire_still_in_use(monkeypatch, adapter, recursive):
    # Watch the original root and any normalized deepcopy the adapter makes.
    refs: list[weakref.ReferenceType[dict[str, object]]] = []

    class Schema(dict[str, object]):
        def __new__(cls, *args, **kwargs):
            root = super().__new__(cls)
            refs.append(weakref.ref(root))
            return root

    fields = {"label": {"type": "string"}}
    schema = Schema(type="object", properties=fields)
    if recursive:
        fields["child"] = {"$ref": "#/$defs/node"}
        schema["$defs"] = {
            "node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/node"}}}
        }
    original = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    monkeypatch.setattr(codec, "_declaration_cache", {})  # exercise the cold path
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        if adapter == "response":
            wire = codec.encode_response_schema_to_wire(schema)
            description = wire[SchemaIndex.DESCRIPTION]
        else:
            wire = codec.encode_function_declaration_to_wire(
                {"name": "catalog_entry", "parameters": schema}
            )
            description = wire[1]
        del schema
        assert refs and all(ref() is None for ref in refs)
        if recursive:
            assert isinstance(description, str) and description.endswith(original)
        else:
            assert description is None
    finally:
        if was_enabled:
            gc.enable()

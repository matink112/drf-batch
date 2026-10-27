"""OpenAPI (drf-spectacular) and browsable-API support for batch endpoints."""

from __future__ import annotations

import pytest
from drf_spectacular.generators import SchemaGenerator


@pytest.fixture(scope="module")
def schema():
    return SchemaGenerator().get_schema(request=None, public=True)


def _request_schema(operation):
    return operation["requestBody"]["content"]["application/json"]["schema"]


def test_create_takes_an_array_of_rows(schema):
    post = schema["paths"]["/orders/batch/"]["post"]

    assert _request_schema(post) == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/Order"},
    }
    assert {"201", "400", "409", "413", "504"} <= set(post["responses"])


def test_update_rows_require_only_the_pk(schema):
    patch = schema["paths"]["/orders/batch/"]["patch"]
    row = schema["components"]["schemas"]["OrderBatchUpdateRow"]

    assert _request_schema(patch)["type"] == "array"
    assert row["required"] == ["id"]
    assert row["properties"]["id"] == {"type": "integer"}
    assert {"403", "404"} <= set(patch["responses"])


def test_replace_rows_keep_the_child_required_fields(schema):
    put = schema["paths"]["/replace-orders/batch/"]["put"]
    row = schema["components"]["schemas"]["OrderBatchReplaceRow"]

    assert _request_schema(put)["items"] == {
        "$ref": "#/components/schemas/OrderBatchReplaceRow"
    }
    assert set(row["required"]) == {
        "id",
        "ticker",
        "customer",
        "volume",
        "reference_id",
    }
    assert {"403", "404"} <= set(put["responses"])
    # The partial row is a separate component.
    assert schema["components"]["schemas"]["OrderBatchUpdateRow"]["required"] == ["id"]


def test_replace_is_not_documented_unless_enabled(schema):
    assert "put" not in schema["paths"]["/orders/batch/"]


def test_destroy_documents_its_pk_array(schema):
    delete = schema["paths"]["/orders/batch/"]["delete"]

    assert "array of primary keys" in delete["description"]


def test_count_and_data_responses(schema):
    count = schema["paths"]["/orders/batch/"]["post"]["responses"]["201"]
    data = schema["paths"]["/data-orders/batch/"]["post"]["responses"]["201"]

    assert count["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/BatchCount"
    }
    assert data["content"]["application/json"]["schema"]["type"] == "array"


def test_cached_fields_are_typed_by_their_lookup(schema):
    order = schema["components"]["schemas"]["Order"]["properties"]
    symbol = schema["components"]["schemas"]["SymbolOrder"]["properties"]

    assert order["ticker"] == {"type": "string"}  # lookup="isin"
    assert symbol["symbol"] == {"type": "string", "format": "uuid"}  # UUID pk


def test_disabled_operations_are_not_documented(schema):
    ops = schema["paths"]["/create-only-orders/batch/"]

    assert set(ops) == {"post"}


def test_browsable_api_renders_the_batch_route(client):
    response = client.post("/orders/batch/", [], HTTP_ACCEPT="text/html")

    assert response.status_code == 201
    assert b"Batch" in response.content or b"batch" in response.content

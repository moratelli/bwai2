"""Generated OpenAPI contract checks."""

from typing import cast

from document_processor.api.app import create_app
from document_processor.domain.models import ParserKind


def mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def sequence(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


def test_parser_domain_contains_only_supported_modes() -> None:
    assert [parser.value for parser in ParserKind] == ["pypdf", "gemini"]


def test_openapi_matches_upload_polling_result_health_and_error_contracts() -> None:
    schema = mapping(cast(object, create_app().openapi()))
    paths = mapping(schema["paths"])
    components = mapping(mapping(schema["components"])["schemas"])

    upload = mapping(mapping(paths["/api/v1/documents"])["post"])
    upload_content = mapping(mapping(upload["requestBody"])["content"])
    assert "multipart/form-data" in upload_content
    upload_responses = mapping(upload["responses"])
    assert set(upload_responses) == {"202", "413", "422", "503"}
    for status_code in ("413", "422", "503"):
        error_schema = mapping(
            mapping(mapping(upload_responses[status_code])["content"])["application/json"]
        )
        assert mapping(error_schema["schema"])["$ref"] == "#/components/schemas/ErrorResponse"

    parser = mapping(components["ParserKind"])
    assert parser["enum"] == ["pypdf", "gemini"]

    result = mapping(mapping(paths["/api/v1/documents/{document_id}"])["get"])
    result_responses = mapping(result["responses"])
    assert {"200", "404", "503"}.issubset(result_responses)
    result_schema = mapping(components["DocumentResponse"])
    assert mapping(result_schema["discriminator"])["propertyName"] == "status"
    assert set(mapping(mapping(result_schema["discriminator"])["mapping"])) == {
        "queued",
        "processing",
        "completed",
        "failed",
    }
    assert len(sequence(result_schema["oneOf"])) == 4

    completed = mapping(components["CompletedDocumentResponse"])
    completed_required = set(cast(list[str], completed["required"]))
    assert {"pages", "summary", "completed_at"}.issubset(completed_required)
    failed = mapping(components["FailedDocumentResponse"])
    failed_required = set(cast(list[str], failed["required"]))
    assert {"error", "completed_at"}.issubset(failed_required)
    assert "pages" not in mapping(failed["properties"])
    assert "summary" not in mapping(failed["properties"])

    live = mapping(mapping(paths["/health/live"])["get"])
    ready = mapping(mapping(paths["/health/ready"])["get"])
    assert set(mapping(live["responses"])) == {"200"}
    assert set(mapping(ready["responses"])) == {"200", "503"}

    error_detail = mapping(components["ErrorDetail"])
    assert set(cast(list[str], error_detail["required"])) == {"code", "message"}
    assert error_detail["additionalProperties"] is False

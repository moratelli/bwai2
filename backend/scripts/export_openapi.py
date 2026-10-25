"""Export the FastAPI OpenAPI document for frontend type generation."""

import json
import sys
from pathlib import Path

from document_processor.api.app import create_app


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: export_openapi.py OUTPUT_PATH")
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(output)


if __name__ == "__main__":
    main()

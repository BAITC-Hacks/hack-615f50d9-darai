import json
from pathlib import Path

from app.main import app


def test_openapi_json_is_up_to_date():
    """backend/openapi.json must be re-exported after API changes (scripts/export_openapi.py)."""
    exported = json.loads((Path(__file__).resolve().parents[2] / "openapi.json").read_text(encoding="utf-8"))
    assert exported == json.loads(json.dumps(app.openapi(), ensure_ascii=False))


def test_routes_have_no_api_prefix():
    assert not any(r.path.startswith("/api") for r in app.routes)

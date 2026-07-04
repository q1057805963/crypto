from pathlib import Path


ASSET_DIR = Path(__file__).with_name("static")

CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


def read_dashboard_html() -> str:
    return (ASSET_DIR / "dashboard.html").read_text(encoding="utf-8")


def read_static_asset(name: str) -> tuple[bytes, str] | None:
    safe_name = Path(name).name
    if safe_name != name:
        return None
    path = ASSET_DIR / safe_name
    if not path.is_file():
        return None
    content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return path.read_bytes(), content_type

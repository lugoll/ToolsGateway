"""The catalog: the shipped entries load, broken entries are refused with a clear message."""
import pytest

from gateway import catalog
from gateway.settings import REPO_CATALOG


def test_shipped_catalog_loads():
    cat = catalog.load(REPO_CATALOG)
    assert {"icloud-mail", "gmail", "remote-mcp"} <= set(cat.entries)
    assert "mail" in cat.native_integrations
    assert [f.name for f in cat.get("icloud-mail").secret_fields] == ["password"]


@pytest.mark.parametrize("data, message", [
    ({"id": "x", "label": "X", "kind": "native", "integration": "nope", "backend": "imap"}, "unknown native"),
    ({"id": "x", "label": "X", "kind": "native", "integration": "mail", "backend": "pop3"}, "backend: one of"),
    ({"id": "x", "label": "X", "kind": "native", "integration": "mail", "backend": "imap",
      "capabilities": {"default": ["send"]}}, r"unknown \['send'\]"),
    ({"id": "x", "label": "X", "kind": "native", "integration": "mail", "backend": "imap",
      "tools": {"deny": ["*"]}}, "no url, headers or tools"),
    ({"id": "x", "label": "X", "kind": "remote", "integration": "x"}, "need `url`"),
    ({"id": "x", "label": "X", "kind": "remote", "integration": "x", "url": "https://x",
      "headers": {"Authorization": "Bearer {tok}"}}, "unknown placeholders"),
    ({"id": "x", "label": "X", "kind": "remote", "integration": "x", "url": "https://x", "extra": 1},
     "Extra inputs"),
])
def test_broken_entries(data, message):
    with pytest.raises(catalog.CatalogError, match=message):
        catalog.parse_entry(data, "x.yaml")


def test_file_name_must_match_id(tmp_path):
    (tmp_path / "other.yaml").write_text("id: x\nlabel: X\nkind: remote\nintegration: x\nurl: https://x\n")
    with pytest.raises(catalog.CatalogError, match="must match the file name"):
        catalog.load(tmp_path)

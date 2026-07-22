import datetime as dt

from plane_pages_mcp import db


def test_is_uuid():
    assert db._is_uuid("803bbaf5-51b7-4ae2-b3f1-0c8ec2abbea1")
    assert not db._is_uuid("TEST")
    assert not db._is_uuid("")


def test_snippet_centers_on_match():
    text = "x" * 200 + "NEEDLE" + "y" * 200
    snip = db._snippet(text, "needle", radius=10)
    assert "NEEDLE" in snip
    assert snip.startswith("…") and snip.endswith("…")
    assert len(snip) < len(text)


def test_snippet_no_match_returns_head():
    assert db._snippet("hello world", "zzz").startswith("hello")


def test_snippet_empty():
    assert db._snippet("", "q") == ""


def test_page_summary_shapes_row():
    row = {
        "id": "abc",
        "name": "My Page",
        "project_identifiers": ["TEST"],
        "archived_at": None,
        "access": db.ACCESS_PRIVATE,
        "updated_at": dt.datetime(2026, 7, 22, 12, 0, 0),
    }
    out = db._page_summary(row)
    assert out["id"] == "abc"
    assert out["project_identifiers"] == ["TEST"]
    assert out["archived"] is False
    assert out["access"] == "private"
    assert out["updated_at"].startswith("2026-07-22")


def test_page_summary_archived_and_public():
    row = {
        "id": "abc", "name": "n", "project_identifiers": None,
        "archived_at": dt.date(2026, 1, 1), "access": db.ACCESS_PUBLIC, "updated_at": None,
    }
    out = db._page_summary(row)
    assert out["archived"] is True
    assert out["access"] == "public"
    assert out["project_identifiers"] == []


def test_insert_column_sets_are_complete():
    # These mirror the INSERT builders; verify.py checks them against the live DB.
    assert "sort_order" in db.PAGES_INSERT_COLUMNS
    assert "color" in db.PAGES_INSERT_COLUMNS
    assert {"page_id", "project_id", "workspace_id"} <= db.PROJECT_PAGES_INSERT_COLUMNS

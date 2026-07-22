import pytest

from plane_pages_mcp import rest


def test_is_uuid():
    assert rest._is_uuid("803bbaf5-51b7-4ae2-b3f1-0c8ec2abbea1")
    assert not rest._is_uuid("TEST-42")


def test_parse_sequence():
    assert rest._parse_sequence("TEST-42") == 42
    assert rest._parse_sequence("42") == 42
    with pytest.raises(rest.RestNotFound):
        rest._parse_sequence("TEST-abc")


def test_resolve_named_found_case_insensitive():
    items = [{"id": "1", "name": "Todo"}, {"id": "2", "name": "In Progress"}]
    assert rest.resolve_named(items, "in progress", what="state") == "2"


def test_resolve_named_unknown_lists_options():
    items = [{"id": "1", "name": "Todo"}]
    with pytest.raises(rest.RestNotFound) as exc:
        rest.resolve_named(items, "Done", what="state")
    assert "Todo" in str(exc.value) and "Done" in str(exc.value)


def test_member_display_map_shapes():
    members = [
        {"member": "u1", "display_name": "Alice"},
        {"id": "u2", "email": "bob@x.com"},
    ]
    m = rest.member_display_map(members)
    assert m["u1"] == "Alice"
    assert m["u2"] == "bob@x.com"


def test_resolve_member_by_display_or_email():
    members = [{"member": "u1", "display_name": "Alice", "email": "alice@x.com"}]
    assert rest.resolve_member(members, "alice") == "u1"
    assert rest.resolve_member(members, "alice@x.com") == "u1"


def test_resolve_member_unknown_lists_options():
    members = [{"member": "u1", "display_name": "Alice"}]
    with pytest.raises(rest.RestNotFound) as exc:
        rest.resolve_member(members, "Zoe")
    assert "Alice" in str(exc.value)

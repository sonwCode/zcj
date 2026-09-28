r"""Upstream Outlook ids must not be able to abort a mailbox poll.

``assast/outlookEmail`` is an external admin service: the JSON it returns is not ours to
control, and it is inconsistent about how it serializes ids. The module already treats
``0`` as "no usable id" and guards ``int()`` in some places, but two sites coerced the id
with a bare ``int(tag.get("id") or 0)``:

* ``_get_or_create_tag_id`` (the tag lookup and the tag-create response), and
* ``_resolve_account_id`` (the account lookup and the caller-supplied account id).

An id of ``"abc"`` or ``"12.5"`` therefore raised ``ValueError`` out of ``_list_tags``-
driven code, turning a poll into a crash instead of the "no usable id" path the rest of
the module is written around. ``_bounded_int`` had the matching hole for
``float("inf")``, which raises ``OverflowError`` and so escaped its
``(TypeError, ValueError)`` guard.
"""
from __future__ import annotations

import pytest

from core.outlook_email_mailbox import (
    OutlookEmailMailbox,
    _bounded_int,
    _int_or_zero,
)


UNPARSEABLE = [
    "abc",
    "12.5",
    "1e5",
    " ",
    "0x10",
    "12 34",
    [1],
    {"a": 1},
    object(),
    # ``json.loads`` accepts these by default, so a hostile or sloppy upstream can put
    # them in a tag id. ``int(inf)`` raises OverflowError - the part of the guard that a
    # plain (TypeError, ValueError) catch would miss. This mirrors the real "test data
    # never reached the guard" miss from the account-export timestamps.
    float("inf"),
    float("-inf"),
    float("nan"),
]


@pytest.mark.parametrize("value", UNPARSEABLE)
def test_an_unparseable_upstream_id_becomes_zero(value):
    assert _int_or_zero(value) == 0


@pytest.mark.parametrize(
    "value,expected",
    [
        (7, 7),
        ("7", 7),
        (" 7 ", 7),
        (3.9, 3),
        (0, 0),
        (None, 0),
        ("", 0),
    ],
)
def test_a_parseable_upstream_id_is_preserved(value, expected):
    assert _int_or_zero(value) == expected


@pytest.mark.parametrize("value", [float("inf"), float("-inf")])
def test_bounded_int_survives_infinity(value):
    """``int(inf)`` raises OverflowError, which the old guard did not catch."""
    assert _bounded_int(value, default=9, minimum=0, maximum=1000) == 9


def test_bounded_int_still_clamps_and_defaults():
    assert _bounded_int(10**400, default=9, minimum=0, maximum=1000) == 1000
    assert _bounded_int("abc", default=9, minimum=0, maximum=1000) == 9
    assert _bounded_int("500", default=9, minimum=0, maximum=1000) == 500


class _StubMailbox(OutlookEmailMailbox):
    """Drive the real tag/account resolution with a stubbed upstream service."""

    def __init__(self, *, tags=None, created_tag=None, accounts=None):
        super().__init__(api_url="https://mail.example.com", api_key="k")
        self._tags = tags or []
        self._created_tag = created_tag
        self._accounts = accounts or []
        self.created: list[dict] = []

    def _admin_get_json(self, path):
        assert path == "/api/tags"
        return {"tags": self._tags}

    def _admin_post_json(self, path, body):
        self.created.append(body)
        return {"tag": self._created_tag}

    def _list_accounts(self):
        return self._accounts


def test_a_matching_tag_with_a_non_numeric_id_does_not_crash():
    """The lookup degrades to the "no usable id" sentinel instead of raising.

    A name match short-circuits, so this returns 0 rather than creating a duplicate.
    That is exactly what the caller is written around: ``add_tags_to_account`` does
    ``tag_id = self._get_or_create_tag_id(name); if tag_id <= 0: continue`` - it skips
    a tag it cannot address rather than failing the whole mail read.
    """
    box = _StubMailbox(tags=[{"name": "zcj", "id": "abc"}], created_tag={"id": 42})

    assert box._get_or_create_tag_id("zcj") == 0
    assert box.created == []  # no duplicate tag is created for a name that exists


def test_add_tags_to_account_skips_a_tag_it_cannot_address():
    """The consumer of that 0 sentinel: skip the tag, keep going, do not raise."""
    box = _StubMailbox(tags=[{"name": "bad", "id": "abc"}], created_tag={"id": 55})

    applied = box.add_tags_to_account(email="a@b.com", account_id="3", tag_names=["bad"])

    assert applied == []


def test_a_matching_tag_with_a_string_numeric_id_is_reused():
    box = _StubMailbox(tags=[{"name": "zcj", "id": "77"}])

    assert box._get_or_create_tag_id("ZCJ") == 77
    assert box.created == []


def test_a_created_tag_with_a_bad_id_still_reports_a_clear_error():
    box = _StubMailbox(tags=[], created_tag={"id": "abc"})

    with pytest.raises(RuntimeError, match="未返回有效 ID"):
        box._get_or_create_tag_id("zcj")


def test_a_created_tag_with_a_missing_id_reports_the_same_error():
    box = _StubMailbox(tags=[], created_tag={})

    with pytest.raises(RuntimeError, match="未返回有效 ID"):
        box._get_or_create_tag_id("zcj")


def test_account_lookup_survives_a_non_numeric_account_id():
    box = _StubMailbox(accounts=[{"email": "a@b.com", "id": "abc"}])

    assert box._resolve_account_id(email="a@b.com", account_id="not-a-number") == 0


def test_account_lookup_returns_a_string_numeric_id():
    box = _StubMailbox(accounts=[{"email": "a@b.com", "id": "99"}])

    assert box._resolve_account_id(email="a@b.com") == 99


def test_a_valid_account_id_argument_short_circuits_the_lookup():
    box = _StubMailbox(accounts=[])

    assert box._resolve_account_id(email="a@b.com", account_id="12") == 12

r"""Every exit from the SMS-slot holder must give the slot back.

``_do_one`` acquires a slot with a blocking ``sms_slot_queue.get()`` and later returns
it from a ``finally``. Anything that leaves the function between the acquire and that
``try`` therefore leaks the slot. Enough leaks and the queue is empty for good, at
which point the next ``sms_slot_queue.get()`` blocks forever - the scheduling guard
only withholds new work while ``len(futures) >= concurrency``. That guard stops helping
exactly when the leaked workers drain.

These checks are structural (AST over the real source) because ``_do_one`` needs the
whole task subsystem to execute; the property under test is about control flow, not
values.
"""
from __future__ import annotations

import ast
import os

import pytest


MODULE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "application", "tasks.py",
)


@pytest.fixture(scope="module")
def do_one():
    with open(MODULE, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_do_one":
            return node
    pytest.fail("_do_one not found in application/tasks.py")


def _returns_outside(node, start, stop):
    """Return statements that leave ``node`` itself, between two line numbers.

    A return inside a nested ``def`` (e.g. the phone-swap callback) leaves that
    callback, not ``_do_one``, so it is not an escape from the slot holder.
    """
    nested = set()
    for child in ast.walk(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child is not node:
            for inner in ast.walk(child):
                nested.add(id(inner))
    found = []
    for child in ast.walk(node):
        if isinstance(child, ast.Return) and start < child.lineno < stop:
            if id(child) not in nested:
                found.append(child)
    return found


def _acquire_line(node) -> int:
    """Line of the blocking ``sms_slot_queue.get()``."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            if child.func.attr == "get":
                value = child.func.value
                if isinstance(value, ast.Name) and value.id == "sms_slot_queue":
                    return child.lineno
    pytest.fail("no sms_slot_queue.get() found in _do_one")


def _slot_returning_finally(node):
    """The try/finally that puts the slot back."""
    for child in ast.walk(node):
        if not isinstance(child, ast.Try) or not child.finalbody:
            continue
        for inner in ast.walk(child):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                if inner.func.attr == "put":
                    value = inner.func.value
                    if isinstance(value, ast.Name) and value.id == "sms_slot_queue":
                        return child
    pytest.fail("no finally returning the slot found")


def test_the_slot_is_returned_from_a_finally(do_one):
    holder = _slot_returning_finally(do_one)

    assert holder.finalbody
    assert _acquire_line(do_one) < holder.lineno


def test_the_guard_stops_before_the_slot_can_leak(do_one):
    """A return between the acquire and the try must hand the slot back itself."""
    acquire = _acquire_line(do_one)
    holder = _slot_returning_finally(do_one)
    window_start = holder.body[0].lineno

    offenders = []
    for ret in _returns_outside(do_one, acquire, window_start):
        # Walk up to the enclosing statement list and look for a preceding
        # sms_slot_queue.put(...) inside the same if-block.
        guarded = False
        for candidate in ast.walk(do_one):
            if not isinstance(candidate, (ast.If, ast.FunctionDef)):
                continue
            body = getattr(candidate, "body", [])
            for index, stmt in enumerate(body):
                if stmt is ret:
                    before = body[:index]
                    for prev in before:
                        for inner in ast.walk(prev):
                            if (
                                isinstance(inner, ast.Call)
                                and isinstance(inner.func, ast.Attribute)
                                and inner.func.attr == "put"
                                and isinstance(inner.func.value, ast.Name)
                                and inner.func.value.id == "sms_slot_queue"
                            ):
                                guarded = True
        if not guarded:
            offenders.append(ret.lineno)

    assert offenders == [], (
        "these returns leave _do_one after acquiring an SMS slot without returning it: "
        "%s" % offenders
    )


def _puts_slot_back(node) -> bool:
    """True when ``node`` contains a ``sms_slot_queue.put(sms_slot_id)``."""
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
            if inner.func.attr != "put":
                continue
            value = inner.func.value
            if not (isinstance(value, ast.Name) and value.id == "sms_slot_queue"):
                continue
            if any(
                isinstance(arg, ast.Name) and arg.id == "sms_slot_id"
                for arg in inner.args
            ):
                return True
    return False


def test_the_proxy_resolution_window_is_protected(do_one):
    """The resolver calls out to the pool/pinner; a failure there must not leak.

    It is not enough for a ``try`` to exist - its handler has to hand the slot
    back, otherwise the slot leaks exactly as before and the guard is decorative.
    """
    acquire = _acquire_line(do_one)
    holder = _slot_returning_finally(do_one)
    window_start = holder.body[0].lineno

    guards = [
        node
        for node in ast.walk(do_one)
        if isinstance(node, ast.Try)
        and acquire < node.lineno < window_start
        and node.handlers
    ]

    assert guards, "no except-guard protects the post-acquire region"
    assert any(
        _puts_slot_back(handler) for guard in guards for handler in guard.handlers
    ), "the except-guard in the post-acquire region does not return the SMS slot"
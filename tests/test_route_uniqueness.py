"""Guard against two routers claiming the same path and method.

``GET /tasks/{task_id}/events`` was registered twice - once in ``api/tasks.py``
(``since=``, 404s on an unknown task) and once in ``api/task_logs.py``
(``after_id=``). Whichever router was included first won and the other was
unreachable. That is worth guarding because the two copies disagreed on the
cursor parameter: the frontend sends ``?since=``, so had the include order ever
been flipped the shadowing handler would have ignored the cursor and replayed the
whole log on every poll. It also emitted a duplicate OpenAPI operation id, which
breaks generated clients.
"""
from __future__ import annotations

import importlib
import pathlib

import main


def _registered_routes() -> list[tuple[str, str, str]]:
    """(method, prefixed path, module) for every route the app exposes."""
    routes: list[tuple[str, str, str]] = []
    api_dir = pathlib.Path(__file__).resolve().parent.parent / "api"
    for module_path in sorted(api_dir.glob("*.py")):
        if module_path.stem.startswith("_"):
            continue
        module = importlib.import_module("api." + module_path.stem)
        router = getattr(module, "router", None)
        if router is None:
            continue
        for route in getattr(router, "routes", []):
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if not path or not methods:
                continue
            for method in methods:
                routes.append((method, "/api" + path, module_path.name))
    return routes


def _registered_portal_routes() -> list[tuple[str, str, str]]:
    """Return routes from the separately mounted customer portal app."""
    from customer_portal_api.main import app as portal_app

    routes: list[tuple[str, str, str]] = []
    for included in getattr(portal_app, "routes", []):
        router = getattr(included, "original_router", None)
        context = getattr(included, "include_context", None)
        if router is None or context is None:
            continue
        prefix = str(getattr(context, "prefix", "") or "")
        for route in getattr(router, "routes", []):
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if not path or not methods:
                continue
            for method in methods:
                routes.append((method, prefix + path, "customer_portal_api"))
    return routes


def test_the_scan_finds_the_api_surface():
    """A broken import would make the duplicate check pass vacuously."""
    routes = _registered_routes()
    assert len(routes) > 50, "expected the whole API surface, found %d" % len(routes)


def test_no_two_routers_register_the_same_path_and_method():
    seen: dict[tuple[str, str], str] = {}
    clashes = []
    for method, path, module in _registered_routes():
        key = (method, path)
        if key in seen and seen[key] != module:
            clashes.append("%s %s -> %s and %s" % (method, path, seen[key], module))
        seen.setdefault((method, path), module)
    assert not clashes, "duplicate routes:\n  " + "\n  ".join(clashes)


def test_the_polling_and_stream_endpoints_still_exist():
    """The endpoints the task log panel depends on, by name."""
    paths = {path for _method, path, _module in _registered_routes()}
    assert "/api/tasks/{task_id}/events" in paths
    assert "/api/tasks/{task_id}/events/stream" in paths


def test_customer_portal_route_surface_is_scanned():
    routes = _registered_portal_routes()
    assert len(routes) > 25, "expected the customer portal route surface"


def test_customer_portal_has_no_duplicate_path_and_method():
    seen: dict[tuple[str, str], str] = {}
    clashes = []
    for method, path, module in _registered_portal_routes():
        key = (method, path)
        if key in seen and seen[key] != module:
            clashes.append(f"{method} {path} -> {seen[key]} and {module}")
        seen.setdefault(key, module)
    assert not clashes, "duplicate portal routes:\n  " + "\n  ".join(clashes)


def test_no_duplicate_openapi_operation_ids():
    """Duplicate operation ids break generated clients."""
    main.app.openapi_schema = None  # the schema is cached after first build
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        main.app.openapi()
    duplicates = [str(w.message) for w in caught if "Duplicate Operation ID" in str(w.message)]
    assert not duplicates, "\n".join(duplicates)

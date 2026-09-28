r"""A UA string must not contradict the TLS fingerprint it is sent with.

curl_cffi's impersonate targets pin an operating system as well as a browser build
(``curl_cffi/fingerprints.py``, ``NATIVE_IMPERSONATE_TARGETS``). Sending a Windows UA
under a macOS target - or vice versa - leaves the transport-layer fingerprint and the
request header disagreeing about the same machine, which is exactly the signal that
fingerprint-sensitive endpoints (Cloudflare, Stripe) act on.

These tests read the installed fingerprint table as ground truth rather than hardcoding
a second copy of it, so they keep working when curl_cffi adds targets. Coverage is
deliberately tree-wide: the named per-module cases below are the ones that once broke,
and the two scans at the end check every module - and every individual call site - under
the repository, so a contradiction cannot hide in a file no test names.
"""
from __future__ import annotations

import ast
import os
import re

import pytest

from curl_cffi import fingerprints as cffi_fingerprints


MODULES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "platforms", "chatgpt")


def _target_os() -> dict:
    """Map every impersonate target name to the OS curl_cffi ships for it."""
    return {
        spec["target_name"]: (spec["os"], spec["browser"])
        for spec in cffi_fingerprints.NATIVE_IMPERSONATE_TARGETS
    }


def _os_of_user_agent(user_agent: str) -> str:
    """Classify a UA string by the OS it claims to run on."""
    ua = str(user_agent or "")
    if "Windows NT" in ua:
        return "Windows"
    if "Macintosh" in ua or "Mac OS X" in ua:
        return "macOS"
    if "iPhone" in ua or "iPad" in ua:
        return "iOS"
    if "Android" in ua:
        return "Android"
    if "Linux" in ua or "X11" in ua:
        return "Linux"
    return ""


def _string_constants(path: str) -> dict:
    """Collect module-level ``NAME = "..."`` (including implicit concatenation)."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            value = ast.literal_eval(node.value)
        except Exception:
            continue
        if isinstance(value, str) and value:
            found[target.id] = value
    return found


def test_every_firefox_target_curl_cffi_ships_is_macos():
    """The premise the payment protocol comment relies on."""
    oses = {
        spec["os"]
        for spec in cffi_fingerprints.NATIVE_IMPERSONATE_TARGETS
        if spec["browser"] == "Firefox"
    }

    assert oses == {"macOS"}


def test_the_payment_protocol_ua_matches_its_impersonate_target():
    path = os.path.join(MODULES, "payment_protocol.py")
    constants = _string_constants(path)
    target = constants.get("_DEFAULT_IMPERSONATE")
    user_agent = constants.get("_DEFAULT_USER_AGENT")

    assert target and user_agent, "constants renamed: %s" % sorted(constants)

    expected_os, _browser = _target_os()[target]
    assert _os_of_user_agent(user_agent) == expected_os


@pytest.mark.parametrize(
    "filename",
    ["token_refresh.py", "workspace_join.py"],
)
def test_a_hardcoded_ua_agrees_with_the_chrome120_target_it_is_sent_with(filename):
    """Both modules pair impersonate="chrome120" with a literal UA string."""
    path = os.path.join(MODULES, filename)
    with open(path, encoding="utf-8") as handle:
        source = handle.read()

    assert 'impersonate="chrome120"' in source, "target changed; update this test"

    targets = re.findall(r"Chrome/(\d+)\.\d+\.\d+\.\d+", source)
    assert targets, "no Chrome UA found in %s" % filename

    expected_os, _browser = _target_os()["chrome120"]
    for user_agent in re.findall(r"Mozilla/5\.0 \(([^)]*)\)", source):
        assert _os_of_user_agent("Mozilla/5.0 (%s)" % user_agent) == expected_os


def test_the_header_builders_no_longer_fabricate_client_hints():
    """Sec-* is forbidden on an in-page fetch, and the backend is Firefox."""
    path = os.path.join(MODULES, "browser_register.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()

    assert "_infer_sec_ch_ua" not in source
    assert '"sec-ch-ua-platform": \'"Windows"\'' not in source

def test_no_windows_user_agent_survives_in_the_browser_module():
    """The backend is Camoufox (Firefox on macOS) - a Windows UA cannot belong here.

    Each of these was paired with a macOS ``impersonate`` target, or with the live
    Firefox page, so the UA contradicted the transport fingerprint underneath it.
    """
    path = os.path.join(MODULES, "browser_register.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()

    offenders = [
        line.strip()
        for line in source.splitlines()
        if "Windows NT" in line
    ]

    assert offenders == []


def test_the_browser_fallback_ua_is_a_firefox_ua():
    """Only reached when the page cannot report its UA, but it still ships in the payload."""
    path = os.path.join(MODULES, "browser_register.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_fallback_browser_ua"
    )
    # ast.get_docstring() returns a cleaned copy, so compare the node by identity
    # instead of by value - otherwise the prose (which mentions Chrome) leaks in.
    doc_node = None
    first = function.body[0] if function.body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        doc_node = first.value
    literals = [
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node is not doc_node
    ]
    joined = "".join(literals)

    assert "Firefox/" in joined
    assert "Chrome/" not in joined
    assert _os_of_user_agent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15) " + joined) == "macOS"


def test_the_oauth_session_ua_matches_its_chrome131_target():
    """_complete_oauth_with_session builds a chrome131 session and sends a literal UA."""
    path = os.path.join(MODULES, "browser_register.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()

    assert 'impersonate="chrome131"' in source

    expected_os, _browser = _target_os()["chrome131"]
    for parenthesised in re.findall(r"Mozilla/5\.0 \(([^)]*)\)", source):
        assert _os_of_user_agent("Mozilla/5.0 (%s)" % parenthesised) == expected_os


_ALL_PLATFORM_ROOT = os.path.dirname(MODULES)
_REPO_ROOT = os.path.dirname(_ALL_PLATFORM_ROOT)


def _platform_modules():
    """Every ``*.py`` under ``platforms/`` - the guard below is deliberately tree-wide."""
    found = []
    for dirpath, _dirnames, filenames in os.walk(_ALL_PLATFORM_ROOT):
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                found.append(os.path.join(dirpath, filename))
    return found


def _resolve_target(name):
    """Map an alias such as ``chrome`` onto the target curl_cffi really uses."""
    try:
        from curl_cffi.requests.impersonate import REAL_TARGET_MAP
    except Exception:  # pragma: no cover - layout changed
        REAL_TARGET_MAP = {}
    return REAL_TARGET_MAP.get(name, name)


def _impersonate_targets(source, constants):
    """Targets from ``impersonate="x"``, a ``.impersonate = "x"`` assignment, or a constant."""
    names = set(re.findall(r'impersonate\s*=\s*"([A-Za-z0-9_]+)"', source))
    for const in re.findall(r"impersonate\s*=\s*([A-Za-z_][A-Za-z0-9_]*)", source):
        value = constants.get(const)
        if value:
            names.add(value)
    return {_resolve_target(name) for name in names}


def test_every_platform_module_agrees_with_its_impersonate_target():
    """Tree-wide check: a module's UA must not contradict its TLS target's OS.

    The per-module tests above each name a single file, so a contradiction in any
    *other* module was invisible - and five had one. ``oauth.py`` and ``plugin.py``
    send ``impersonate="chrome"``, which resolves to chrome150 (macOS Tahoe), while
    ``agent_identity.py``, ``oreateai/core.py`` and ``trae/switch.py`` send
    ``chrome124``; all five shipped a Windows UA under a macOS fingerprint.

    A module is judged only when it names exactly one target: with two targets a
    single literal UA cannot be attributed to either one, so e.g. ``payment.py``
    (chrome124 + chrome110) is out of scope rather than silently wrong.
    """
    offenders = []
    checked = 0
    for path in _platform_modules():
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        targets = _impersonate_targets(source, _string_constants(path))
        if len(targets) != 1:
            continue
        target = targets.pop()
        spec = _target_os().get(target)
        if spec is None:
            continue
        checked += 1
        for parenthesised in re.findall(r"Mozilla/5\.0 \(([^)]*)\)", source):
            claimed = _os_of_user_agent("Mozilla/5.0 (%s)" % parenthesised)
            if claimed and claimed != spec[0]:
                offenders.append(
                    "%s: impersonate=%s is %s, UA claims %s"
                    % (os.path.relpath(path, _ALL_PLATFORM_ROOT), target, spec[0], claimed)
                )

    assert checked >= 10, "scan matched only %d modules; it stopped reaching them" % checked
    assert offenders == []

def _call_sites_with_identity(path):
    '''Every call that names both an impersonate target and a literal User-Agent.

    Returns (line, resolved_target, user_agent). Module-level NAME = 'literal'
    assignments are inlined so headers styles that reference a constant resolve.
    '''
    import ast

    with open(path, encoding='utf-8') as handle:
        source = handle.read()
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - a broken file is another test's problem
        return []

    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = None
        user_agent = None
        for keyword in node.keywords:
            if keyword.arg == 'impersonate':
                value = keyword.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    target = value.value
                elif isinstance(value, ast.Name) and value.id in constants:
                    target = constants[value.id]
            if keyword.arg == 'headers':
                for sub in ast.walk(keyword.value):
                    if not isinstance(sub, ast.Dict):
                        continue
                    for key, value in zip(sub.keys, sub.values):
                        name = key.value if isinstance(key, ast.Constant) else None
                        if not (isinstance(name, str) and name.lower() == 'user-agent'):
                            continue
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            user_agent = value.value
                        elif isinstance(value, ast.Name) and value.id in constants:
                            user_agent = constants[value.id]
        if target and user_agent:
            found.append((node.lineno, _resolve_target(target), user_agent))
    return found


def test_every_call_site_with_a_user_agent_matches_its_impersonate_target():
    '''Per-call attribution, so multi-target modules are covered too.

    The module-level check above skips any file naming more than one target - which
    leaves payment.py (chrome124 + chrome110) and cpa_upload.py (chrome110 +
    chrome120) uncovered. Attributing the header to the individual call instead of
    to the file closes that gap: a Windows UA under a macOS target is caught
    wherever it appears, however many targets its module names.
    '''
    candidates = []
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in {'vendor', 'node_modules', '.git'}]
        for filename in sorted(filenames):
            if not filename.endswith('.py'):
                continue
            full = os.path.join(dirpath, filename)
            candidates.extend(_call_sites_with_identity(full))

    offenders = []
    checked = 0
    for line, target, user_agent in candidates:
        spec = _target_os().get(target)
        if spec is None:
            continue
        claimed = _os_of_user_agent(user_agent)
        if not claimed:
            continue
        checked += 1
        if claimed != spec[0]:
            offenders.append(
                'impersonate=%s is %s, UA claims %s (line %d)'
                % (target, spec[0], claimed, line)
            )

    # Reach is deliberately reported, not inflated: most impersonate= call sites pass
    # no UA at all (the session default is the header), so only a handful can be
    # attributed per call. The floor is what this tree currently yields, so a scan
    # that quietly stops matching fails here instead of passing vacuously.
    assert checked >= 3, 'scan matched only %d call sites; it stopped reaching them' % checked
    assert offenders == []

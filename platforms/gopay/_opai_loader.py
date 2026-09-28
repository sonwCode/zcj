"""Lazy loader for the bundled ``gopay-deploy`` SDK.

Background:
    The deploy package lives at ``platforms/gopay-deploy/app/src/opai`` and is
    written to be installed via ``pip install -e .`` so that ``import opai...``
    works. We do not want to require that install step inside the main project,
    so we surgically add ``app/src`` to ``sys.path`` on first import and let
    callers use ``from opai.core.gopay_protocol_worker import _register_one``
    style imports.

The hyphen in the directory name (``gopay-deploy``) makes it impossible to
``import platforms.gopay-deploy`` - that is why we do path injection rather than
a relative import.

The SDK is **not** part of this repository: ``.gitignore`` excludes
``platforms/gopay-deploy/``. A fresh clone therefore has no GoPay protocol
support at all, and only the GoPay paths need it.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading

_LOCK = threading.Lock()
_LOADED = False


def _opai_src_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    # platforms/gopay/_opai_loader.py -> platforms/gopay-deploy/app/src
    deploy_root = os.path.normpath(os.path.join(here, "..", "gopay-deploy"))
    return os.path.join(deploy_root, "app", "src")


def opai_src_dir() -> str:
    """Absolute path where the bundled SDK is expected to live."""
    return _opai_src_dir()


def opai_available() -> bool:
    """True when the SDK can be imported, bundled or pip-installed."""
    if os.path.isdir(_opai_src_dir()):
        return True
    return importlib.util.find_spec("opai") is not None


def ensure_opai_on_path() -> None:
    """Idempotently add the bundled ``opai`` SDK to ``sys.path``.

    Raises:
        RuntimeError: the SDK is neither present at :func:`opai_src_dir` nor
            importable from an installed distribution.

    Raising here is deliberate. Previously a missing SDK was swallowed, and the
    caller's own ``from opai... import`` failed a few frames later with a bare
    ``No module named 'opai'`` - which gives no hint that a separate SDK has to
    be dropped in at a fixed path. Every call site either already wraps this in
    ``except Exception`` or lets it propagate, so the control flow is unchanged
    and only the message differs.

    Keep calling this lazily, from inside a function. It used to be safe to
    call at module import scope because a missing SDK raised
    ``ModuleNotFoundError``, which
    ``core/registry.py::load_all()`` swallows while scanning plugins. The
    ``RuntimeError`` raised here is *not* an ``ImportError``, so a module-scope
    call would abort the whole plugin scan instead of skipping GoPay alone.
    """
    global _LOADED
    if _LOADED:
        return
    with _LOCK:
        if _LOADED:
            return
        src_dir = _opai_src_dir()
        if not os.path.isdir(src_dir):
            # ``pip install -e .`` inside the SDK leaves ``opai`` importable
            # without the bundled directory, which is a legitimate setup.
            if importlib.util.find_spec("opai") is not None:
                _LOADED = True
                return
            raise RuntimeError(
                "GoPay 协议 SDK 未找到。期望目录: %s（也接受 pip 安装的 opai）。"
                "该 SDK 不随本仓库分发——.gitignore 排除了 platforms/gopay-deploy/，"
                "所以全新克隆里没有它。只有 GoPay 相关功能需要它，"
                "不用 GoPay 时可以忽略此错误。" % src_dir
            )
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
        _LOADED = True

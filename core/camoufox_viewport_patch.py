"""
Camoufox (Firefox) CDP 补丁

问题：Camoufox 使用 Firefox，其 CDP (Chrome DevTools Protocol) 不支持 Chrome 专有字段：
  - isMobile
  - hasTouch
  - deviceScaleFactor

当 Playwright/Firefox 调用 Browser.setDefaultViewport 时会报错。

修复：在 new_page/new_context 时自动设置 viewport=None，完全绕过 setDefaultViewport。
"""
import sys
import importlib
from pathlib import Path

# 保存原始方法的模块级变量
_orig_new_page = None
_orig_new_context = None
_patch_applied = False
_pageerror_patch_applied = False
_websocket_patch_applied = False

_PAGEERROR_PATCH_REPLACEMENTS = (
    ('url: pageError.location.url,', 'url: pageError.location?.url || "",'),
    ('line: pageError.location.lineNumber,', 'line: pageError.location?.lineNumber || 0,'),
    ('column: pageError.location.columnNumber', 'column: pageError.location?.columnNumber || 0'),
)

_WEBSOCKET_PATCH_REPLACEMENTS = (
    (
        """      _onWebSocketOpened(event) {
        const request2 = this._webSocketRequests.get(event.requestId);
        assert(request2);
        const response2 = this._webSocketResponses.get(event.requestId);
        assert(response2);
        this._webSocketRequests.delete(event.requestId);
        this._webSocketResponses.delete(event.requestId);
        this._page.frameManager.onWebSocketRequest(webSocketId(event.frameId, event.wsid), request2.headers);
        this._page.frameManager.onWebSocketResponse(webSocketId(event.frameId, event.wsid), response2.status, response2.statusText, response2.headers);
      }""",
        """      _onWebSocketOpened(event) {
        const request2 = this._webSocketRequests.get(event.requestId);
        const response2 = this._webSocketResponses.get(event.requestId);
        if (!request2 || !response2)
          return;
        this._webSocketRequests.delete(event.requestId);
        this._webSocketResponses.delete(event.requestId);
        this._page.frameManager.onWebSocketRequest(webSocketId(event.frameId, event.wsid), request2.headers);
        this._page.frameManager.onWebSocketResponse(webSocketId(event.frameId, event.wsid), response2.status, response2.statusText, response2.headers);
      }""",
    ),
    (
        """      _onWebSocketRequestFinished(requestId) {
        const response2 = this._webSocketResponses.get(requestId);
        assert(response2);
        if (response2.status >= 400) {
          const request2 = this._webSocketRequests.get(requestId);
          assert(request2);""",
        """      _onWebSocketRequestFinished(requestId) {
        const response2 = this._webSocketResponses.get(requestId);
        if (!response2)
          return;
        if (response2.status >= 400) {
          const request2 = this._webSocketRequests.get(requestId);
          if (!request2)
            return;""",
    ),
)


def _playwright_core_bundle_path() -> Path:
    import playwright

    return Path(playwright.__file__).resolve().parent / "driver" / "package" / "lib" / "coreBundle.js"


def _get_orig_methods():
    """获取并保存原始方法（使用 importlib 避免递归导入）"""
    global _orig_new_page, _orig_new_context
    if _orig_new_page is None:
        # 直接从 _generated 模块获取原始方法，避免触发补丁
        import playwright.sync_api._generated as generated
        _orig_new_page = generated.Browser.new_page
        _orig_new_context = generated.Browser.new_context
    return _orig_new_page, _orig_new_context


def _patched_new_page(self, **kwargs):
    """Patch过的 new_page，自动设置 viewport=None"""
    orig_method, _ = _get_orig_methods()
    # 关键：设置 viewport=None 跳过 setDefaultViewport
    if 'viewport' not in kwargs:
        kwargs['viewport'] = None
    return orig_method(self, **kwargs)


def _patched_new_context(self, **kwargs):
    """Patch过的 new_context，自动设置 viewport=None"""
    _, orig_method = _get_orig_methods()
    # 关键：设置 viewport=None 跳过 setDefaultViewport
    if 'viewport' not in kwargs:
        kwargs['viewport'] = None
    return orig_method(self, **kwargs)


def apply_patch():
    """应用补丁"""
    global _patch_applied
    if _patch_applied:
        return True
    try:
        # 直接修改 _generated 模块中的类
        import playwright.sync_api._generated as generated

        # 保存原始方法（只在第一次）
        global _orig_new_page, _orig_new_context
        if _orig_new_page is None:
            _orig_new_page = generated.Browser.new_page
            _orig_new_context = generated.Browser.new_context

        # 应用补丁
        generated.Browser.new_page = _patched_new_page
        generated.Browser.new_context = _patched_new_context

        _patch_applied = True
        print("[OK] Playwright Browser 补丁已应用 (viewport=None)")
        return True
    except Exception as e:
        print(f"[WARN] Playwright 补丁失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def apply_pageerror_patch():
    """Patch Firefox pageerror dispatch when Playwright receives no location."""
    global _pageerror_patch_applied
    if _pageerror_patch_applied:
        return True
    try:
        path = _playwright_core_bundle_path()
        text = path.read_text(encoding="utf-8")
        patched = text
        for old, new in _PAGEERROR_PATCH_REPLACEMENTS:
            patched = patched.replace(old, new)
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            print("[OK] Playwright Firefox pageerror 补丁已应用")
        _pageerror_patch_applied = True
        return True
    except Exception as e:
        print(f"[WARN] Playwright pageerror 补丁失败: {e}")
        import traceback
        traceback.print_exc()
        return False


# 立即应用补丁
def apply_websocket_patch():
    """Patch Firefox websocket bookkeeping asserts that can crash the driver."""
    global _websocket_patch_applied
    if _websocket_patch_applied:
        return True
    try:
        path = _playwright_core_bundle_path()
        text = path.read_text(encoding="utf-8")
        patched = text
        for old, new in _WEBSOCKET_PATCH_REPLACEMENTS:
            patched = patched.replace(old, new)
        if patched != text:
            path.write_text(patched, encoding="utf-8")
            print("[OK] Playwright Firefox websocket patch applied")
        _websocket_patch_applied = True
        return True
    except Exception as e:
        print(f"[WARN] Playwright websocket patch failed: {e}")
        import traceback
        traceback.print_exc()
        return False


apply_patch()
apply_pageerror_patch()
apply_websocket_patch()

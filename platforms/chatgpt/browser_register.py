"""ChatGPT 浏览器注册流程（Camoufox）。"""
import base64
import json
import random
import re
import secrets
import sys
import threading
import time
import uuid
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse

from camoufox.sync_api import Camoufox

from .._browser_backend import BrowserBackendConfig, open_browser_backend
from .constants import (
    OPENAI_AUTH,
    CHATGPT_APP,
    PLATFORM_LOGIN_ENTRY,
    SENTINEL_SDK_URL,
    SENTINEL_REQ_URL,
    SENTINEL_FRAME_URL,
    SENTINEL_BASE,
    OAUTH_CONSENT_FORM_SELECTOR,
)
from .playwright_compat import patch_playwright_firefox_pageerror_location_bug


CAMOUFOX_VISIBLE_WINDOW_SIZE = (1280, 720)


def _apply_camoufox_visible_window_limit(
    launch_opts: dict,
    backend_config: BrowserBackendConfig,
) -> None:
    if not backend_config.is_camoufox:
        return
    if backend_config.is_headless or bool(launch_opts.get("headless")):
        return
    launch_opts.setdefault("window", CAMOUFOX_VISIBLE_WINDOW_SIZE)


def _is_transient_nav_error(exc: BaseException) -> bool:
    """page.goto / page.reload 抛错是否属于可重试的瞬时网络断连。

    覆盖 Chromium/Firefox 常见的瞬时网络错误码。业务/页面错误（4xx、选择器
    超时等）不在此列，不会被误判重试。
    """
    msg = str(exc or "").lower()
    return any(
        token in msg
        for token in (
            "err_connection_closed",
            "err_connection_reset",
            "err_connection_refused",
            "err_connection_aborted",
            "err_connection_failed",
            "err_timed_out",
            "err_network_changed",
            "err_empty_response",
            "err_socks_connection_failed",
            "err_proxy_connection_failed",
            "err_tunnel_connection_failed",
            "err_name_not_resolved",
            "err_address_unreachable",
            "ns_error_net",            # Firefox/Camoufox 网络错误前缀
            "neterror",
            "navigating to",           # Playwright 包装的导航失败常带这句
        )
    )


def _is_browser_driver_disconnect(exc: BaseException) -> bool:
    msg = str(exc or "").lower()
    return any(
        token in msg
        for token in (
            "connection closed while reading from the driver",
            "target page, context or browser has been closed",
            "target closed",
        )
    )


def _goto_with_retry(
    page,
    url: str,
    *,
    wait_until: str = "domcontentloaded",
    timeout: int = 30000,
    attempts: int = 3,
    log: Optional[Callable[[str], None]] = None,
):
    """``page.goto`` 带瞬时网络错误重试（默认 3 次，指数退避）。

    全局统一：注册流程里所有打开页面都该走这个，避免一次网络波动
    （ERR_CONNECTION_CLOSED / RESET / TIMED_OUT 等）就直接判失败。
    瞬时错误重试；业务错误（页面 4xx、选择器问题）原样抛出不重试。
    """
    _log = log or (lambda *_a, **_k: None)
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max(int(attempts), 1) + 1):
        try:
            return page.goto(url, wait_until=wait_until, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 按错误内容判定是否重试
            last_exc = exc
            if attempt >= attempts or not _is_transient_nav_error(exc):
                raise
            backoff = 1.5 * attempt
            _log(
                f"打开页面瞬时网络失败（第 {attempt}/{attempts} 次，{backoff:.1f}s 后重试）："
                f"{str(exc)[:120]}"
            )
            time.sleep(backoff)
    if last_exc is not None:
        raise last_exc


def _reload_with_retry(
    page,
    *,
    wait_until: str = "domcontentloaded",
    timeout: int = 30000,
    attempts: int = 3,
    log: Optional[Callable[[str], None]] = None,
):
    """``page.reload`` 带瞬时网络错误重试。"""
    _log = log or (lambda *_a, **_k: None)
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max(int(attempts), 1) + 1):
        try:
            return page.reload(wait_until=wait_until, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= attempts or not _is_transient_nav_error(exc):
                raise
            time.sleep(1.5 * attempt)
    if last_exc is not None:
        raise last_exc


def _run_in_isolated_thread(
    fn,
    *,
    thread_name: str,
    log_fn: Optional[Callable[[str], None]] = None,
    timeout_sec: int | None = None,
):
    result_box = {}
    error_box = {}
    parent_logger = getattr(log_fn, "__self__", None)
    parent_subtask = None
    if parent_logger is not None and hasattr(parent_logger, "_current_subtask"):
        try:
            parent_subtask = parent_logger._current_subtask()
        except Exception:
            parent_subtask = None

    def _target():
        if parent_logger is not None and parent_subtask and parent_subtask[0]:
            try:
                parent_logger.set_subtask(parent_subtask[0], parent_subtask[1])
            except Exception:
                pass
        try:
            result_box["result"] = fn()
        except BaseException as exc:
            error_box["error"] = exc
        finally:
            if parent_logger is not None and parent_subtask and parent_subtask[0]:
                try:
                    parent_logger.clear_subtask()
                except Exception:
                    pass

    thread = threading.Thread(target=_target, name=thread_name)
    if timeout_sec and timeout_sec > 0:
        thread.daemon = True
    thread.start()
    thread.join(timeout=timeout_sec if timeout_sec and timeout_sec > 0 else None)
    if thread.is_alive():
        if callable(log_fn):
            try:
                log_fn(f"{thread_name} 超过 {timeout_sec}s 仍未返回，判定为浏览器页面创建/执行卡住")
            except Exception:
                pass
        raise TimeoutError(f"{thread_name} timeout after {timeout_sec}s")
    if error_box:
        raise error_box["error"]
    return result_box.get("result")


EMAIL_INPUT_SELECTORS = [
    'input#login-email',
    'input[type="email"]',
    'input[name="email"]',
    'input[name="username"]',
    'input[autocomplete="username"]',
    'input[autocomplete*="username"]',
    'input[inputmode="email"]',
    'input[id*="email"]',
]

PASSWORD_INPUT_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="new-password"]',
]

EMAIL_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button[data-testid="continue-button"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Next")',
    'button:has-text("next")',
    'button:has-text("続ける")',
    'button:has-text("続行")',
    'button:has-text("次へ")',
]

PASSWORD_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button[data-testid="continue-button"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Sign up")',
    'button:has-text("sign up")',
    'button:has-text("Create account")',
    'button:has-text("create account")',
    'button:has-text("続ける")',
    'button:has-text("続行")',
    'button:has-text("登録")',
    'button:has-text("新規登録")',
    'button:has-text("アカウントを作成")',
    'button:has-text("サインアップ")',
]

OTP_INPUT_SELECTORS = [
    "input[inputmode='numeric']",
    "input[autocomplete='one-time-code']",
    "input[type='tel']",
    "input[type='number']",
    "input[name*='code' i]",
    "input[id*='code' i]",
]

SIGNUP_RECOVERY_SELECTORS = [
    'a:has-text("Sign up")',
    'button:has-text("Sign up")',
    'a:has-text("sign up")',
    'button:has-text("sign up")',
    'a:has-text("Register")',
    'button:has-text("Register")',
    'a:has-text("Create account")',
    'button:has-text("Create account")',
    'a:has-text("创建账号")',
    'button:has-text("创建账号")',
    'a:has-text("注册")',
    'button:has-text("注册")',
    'a:has-text("登録")',
    'button:has-text("登録")',
    'a:has-text("新規登録")',
    'button:has-text("新規登録")',
    'a:has-text("アカウントを作成")',
    'button:has-text("アカウントを作成")',
    'a:has-text("サインアップ")',
    'button:has-text("サインアップ")',
]

PASSWORDLESS_LOGIN_SELECTORS = [
    'button[name="intent"][value="passwordless_login_send_otp"]',
    'button[value="passwordless_login_send_otp"]',
    'button:has-text("one-time code")',
    'button:has-text("one time code")',
    'button:has-text("passwordless")',
    'button:has-text("一次性验证码")',
    'button:has-text("驗證碼")',
    'button:has-text("验证码")',
    'button:has-text("código único")',
    'button:has-text("code unique")',
    'button:has-text("Einmalcode")',
    'button:has-text("código de uso único")',
    'button:has-text("ワンタイムコード")',
    'button:has-text("一回限りのコード")',
    'button:has-text("認証コード")',
]

# add-phone 页面国际拨号码 -> 国家名映射（用于 UI 下拉选择）
PHONE_COUNTRY_CODE_MAP = {
    "1": "United States", "7": "Russia", "20": "Egypt", "27": "South Africa",
    "30": "Greece", "31": "Netherlands", "32": "Belgium", "33": "France",
    "34": "Spain", "36": "Hungary", "39": "Italy", "40": "Romania",
    "44": "United Kingdom", "45": "Denmark", "46": "Sweden", "47": "Norway",
    "48": "Poland", "49": "Germany", "51": "Peru", "52": "Mexico",
    "53": "Cuba", "54": "Argentina", "55": "Brazil", "56": "Chile",
    "57": "Colombia", "58": "Venezuela", "60": "Malaysia", "61": "Australia",
    "62": "Indonesia", "63": "Philippines", "64": "New Zealand",
    "65": "Singapore", "66": "Thailand", "81": "Japan", "82": "South Korea",
    "84": "Vietnam", "86": "China", "90": "Turkey", "91": "India",
    "92": "Pakistan", "93": "Afghanistan", "94": "Sri Lanka", "95": "Myanmar",
    "98": "Iran", "212": "Morocco", "213": "Algeria", "216": "Tunisia",
    "218": "Libya", "220": "Gambia", "221": "Senegal", "234": "Nigeria",
    "254": "Kenya", "255": "Tanzania", "256": "Uganda", "260": "Zambia",
    "263": "Zimbabwe", "351": "Portugal", "353": "Ireland", "354": "Iceland",
    "358": "Finland", "370": "Lithuania", "371": "Latvia", "372": "Estonia",
    "374": "Armenia", "375": "Belarus", "380": "Ukraine", "381": "Serbia",
    "385": "Croatia", "420": "Czech Republic", "421": "Slovakia",
    "855": "Cambodia", "856": "Laos", "880": "Bangladesh", "886": "Taiwan",
    "960": "Maldives", "966": "Saudi Arabia", "971": "United Arab Emirates",
    "972": "Israel", "977": "Nepal", "992": "Tajikistan",
    "993": "Turkmenistan", "994": "Azerbaijan", "995": "Georgia",
    "996": "Kyrgyzstan", "998": "Uzbekistan",
}

# 拨号码 -> ISO 3166-1 alpha-2 国家代码（用于 React Aria <select> 的 value 匹配）
PHONE_DIAL_TO_ISO = {
    "1": "US", "7": "RU", "20": "EG", "27": "ZA",
    "30": "GR", "31": "NL", "32": "BE", "33": "FR",
    "34": "ES", "36": "HU", "39": "IT", "40": "RO",
    "44": "GB", "45": "DK", "46": "SE", "47": "NO",
    "48": "PL", "49": "DE", "51": "PE", "52": "MX",
    "53": "CU", "54": "AR", "55": "BR", "56": "CL",
    "57": "CO", "58": "VE", "60": "MY", "61": "AU",
    "62": "ID", "63": "PH", "64": "NZ",
    "65": "SG", "66": "TH", "81": "JP", "82": "KR",
    "84": "VN", "86": "CN", "90": "TR", "91": "IN",
    "92": "PK", "93": "AF", "94": "LK", "95": "MM",
    "98": "IR", "212": "MA", "213": "DZ", "216": "TN",
    "218": "LY", "220": "GM", "221": "SN", "234": "NG",
    "254": "KE", "255": "TZ", "256": "UG", "260": "ZM",
    "263": "ZW", "351": "PT", "353": "IE", "354": "IS",
    "358": "FI", "370": "LT", "371": "LV", "372": "EE",
    "374": "AM", "375": "BY", "380": "UA", "381": "RS",
    "385": "HR", "420": "CZ", "421": "SK",
    "855": "KH", "856": "LA", "880": "BD", "886": "TW",
    "960": "MV", "966": "SA", "971": "AE",
    "972": "IL", "977": "NP", "992": "TJ",
    "993": "TM", "994": "AZ", "995": "GE",
    "996": "KG", "998": "UZ",
}

PHONE_INPUT_SELECTORS = [
    'input[type="tel"]',
    'input[name="phone"]',
    'input[name="phone_number"]',
    'input[name="phoneNumber"]',
    'input[id*="phone" i]',
    'input[placeholder*="phone" i]',
    'input[autocomplete="tel"]',
    'input[autocomplete="tel-national"]',
]

PHONE_SEND_SELECTORS = [
    'button[data-testid="continue-button"]',
    'button[data-testid*="send" i]',
    'button:has-text("Send code via SMS")',
    'button:has-text("Send code")',
    'button:has-text("Send via SMS")',
    'button:has-text("Send link via SMS")',
    'button:has-text("Send")',
    'button[type="submit"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("发送")',
    'button:has-text("コードを送信")',
    'button:has-text("SMSで送信")',
    'button:has-text("送信")',
    'button:has-text("続ける")',
    'button:has-text("続行")',
    'button:has-text("次へ")',
]

PHONE_VERIFY_SELECTORS = [
    'button:has-text("Verify")',
    'button:has-text("verify")',
    'button:has-text("Check")',
    'button[type="submit"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("验证")',
    'button:has-text("确认")',
    'button:has-text("確認")',
    'button:has-text("認証")',
    'button:has-text("続ける")',
    'button:has-text("続行")',
    'button:has-text("次へ")',
]


AUTH_TIMEOUT_TITLE_RE = re.compile(r"oops,\s*an\s*error\s*occurred|出错|發生錯誤|エラーが発生|問題が発生", re.I)
AUTH_TIMEOUT_DETAIL_RE = re.compile(
    r"operation\s+timed\s+out|route\s+error|405\s+method\s+not\s+allowed|failed\s+to\s+fetch|network\s+error|fetch\s+failed|タイムアウト|ネットワークエラー|取得に失敗",
    re.I,
)
AUTH_RETRY_TEXT_RE = re.compile(r"try\s+again|重试|重試|再試行|もう一度|やり直す", re.I)


def _is_auth_timeout_retry_text(text: str) -> bool:
    value = str(text or "")
    return bool(
        AUTH_RETRY_TEXT_RE.search(value)
        and (AUTH_TIMEOUT_TITLE_RE.search(value) or AUTH_TIMEOUT_DETAIL_RE.search(value))
    )


def _parse_phone_country_and_local(phone_number: str) -> tuple[str, str, str]:
    """从完整手机号解析出 (拨号码, 本地号码, 国家名)。

    例: +66XXXXXXXXX -> ("66", "XXXXXXXXX", "Thailand")
    """
    num = str(phone_number or "").lstrip("+").strip()
    for length in (3, 2, 1):
        if length > len(num):
            continue
        prefix = num[:length]
        if prefix in PHONE_COUNTRY_CODE_MAP:
            return prefix, num[length:], PHONE_COUNTRY_CODE_MAP[prefix]
    return "", num, ""


def _select_phone_country_ui(page, dial_code: str, country_name: str, log) -> bool:
    """在 add-phone 页面的国家下拉框中选择对应国家。

    OpenAI add-phone 页面使用 React Aria Select 组件，底层有一个隐藏的原生 <select>
    和一个可视的 button trigger + listbox 弹出层。
    """
    if not dial_code and not country_name:
        log("  无法识别国家码，跳过国家选择")
        return False

    iso_code = PHONE_DIAL_TO_ISO.get(dial_code, "")
    log(f"  目标国家: {country_name} (+{dial_code}) ISO={iso_code}")

    # 先检查当前下拉框是否已经是目标国家
    dial_pattern = f"(+{dial_code})"
    already = page.evaluate(
        """
        (dialPattern) => {
          const visible = (el) => {
            if (!el) return false;
            const s = window.getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return s && s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
          };
          const all = Array.from(document.querySelectorAll('button, div, span, a, [role="button"], [role="combobox"], select'));
          for (const el of all) {
            if (!visible(el)) continue;
            const text = (el.innerText || el.textContent || '').trim();
            if (text.includes(dialPattern) && text.length < 80) return true;
          }
          return false;
        }
        """,
        dial_pattern,
    )
    if already:
        log(f"  国家已是目标值: (+{dial_code})")
        return True

    # ═══════════════════════════════════════════════════════════════════
    # 策略 1: 通过底层原生 <select> 直接设置值（最可靠）
    # React Aria Select 底层会有一个隐藏的 <select> 用于表单提交和无障碍。
    # 直接修改它的值并触发 change 事件可以同步 React 状态。
    # ═══════════════════════════════════════════════════════════════════
    native_selected = page.evaluate(
        """
        ({ isoCode, dialCode, countryName }) => {
          const selects = document.querySelectorAll('select');
          for (const sel of selects) {
            if (sel.options.length < 10) continue;  // 排除非国家的 select

            // 尝试多种匹配策略找到目标 option
            let targetValue = null;
            for (const opt of sel.options) {
              const v = (opt.value || '').trim();
              const t = (opt.text || opt.label || '').trim();
              // 匹配 ISO 代码 (如 "TH")
              if (isoCode && v === isoCode) { targetValue = v; break; }
              // 匹配拨号码 (如 value 包含 "66" 或 text 包含 "+66")
              if (t.includes('(+' + dialCode + ')')) { targetValue = v; break; }
              if (t.includes(countryName)) { targetValue = v; break; }
            }

            if (targetValue !== null) {
              // 使用 React 兼容的方式设置值
              const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLSelectElement.prototype, 'value'
              )?.set;
              if (nativeInputValueSetter) {
                nativeInputValueSetter.call(sel, targetValue);
              } else {
                sel.value = targetValue;
              }
              sel.dispatchEvent(new Event('change', { bubbles: true }));
              sel.dispatchEvent(new Event('input', { bubbles: true }));
              return { ok: true, value: targetValue, method: 'native_setter' };
            }
          }
          return { ok: false };
        }
        """,
        {"isoCode": iso_code, "dialCode": dial_code, "countryName": country_name},
    )
    if native_selected and native_selected.get("ok"):
        log(f"  ✓ 通过原生 <select> 选择成功: value={native_selected.get('value')}")
        time.sleep(0.5)
        # 验证 UI 是否同步更新
        verify = page.evaluate(
            "(dp) => { const b = document.querySelector('button[aria-haspopup=\"listbox\"]'); return b ? (b.innerText || '').trim() : ''; }",
            dial_pattern,
        )
        if f"+{dial_code}" in (verify or ""):
            log(f"  ✓ UI 已同步: {verify}")
            return True
        log(f"  原生 select 已设置但 UI 未同步 ({verify})，尝试 UI 交互...")

    # ═══════════════════════════════════════════════════════════════════
    # 策略 2: 通过 React Aria 的 key 属性直接操作
    # ═══════════════════════════════════════════════════════════════════
    key_selected = page.evaluate(
        """
        ({ isoCode, dialCode, countryName }) => {
          // 找到 React Aria Select 的隐藏 <select> 并通过 selectOption 模拟
          const selects = document.querySelectorAll('select');
          for (const sel of selects) {
            if (sel.options.length < 10) continue;
            for (const opt of sel.options) {
              const v = (opt.value || '').trim();
              const t = (opt.text || opt.label || '').trim();
              if ((isoCode && v === isoCode) || t.includes('(+' + dialCode + ')') || t.includes(countryName)) {
                sel.value = v;
                // 触发 React 合成事件
                const ev = new Event('change', { bubbles: true });
                Object.defineProperty(ev, 'target', { writable: false, value: sel });
                sel.dispatchEvent(ev);
                return { ok: true, value: v, text: t };
              }
            }
          }
          return { ok: false };
        }
        """,
        {"isoCode": iso_code, "dialCode": dial_code, "countryName": country_name},
    )

    # ═══════════════════════════════════════════════════════════════════
    # 策略 3: 使用 Playwright 的 selectOption API（对原生 select 最可靠）
    # ═══════════════════════════════════════════════════════════════════
    try:
        select_el = page.query_selector("select")
        if select_el:
            # 尝试用 ISO 代码选择
            if iso_code:
                try:
                    select_el.select_option(value=iso_code)
                    log(f"  ✓ Playwright selectOption(value={iso_code}) 成功")
                    time.sleep(0.5)
                    return True
                except Exception:
                    pass
            # 尝试用 label 匹配（包含国家名或拨号码）
            try:
                # 获取所有 option 的 value 和 text，找到匹配的
                match_value = page.evaluate(
                    """
                    ({ dialCode, countryName }) => {
                      const sel = document.querySelector('select');
                      if (!sel) return '';
                      for (const opt of sel.options) {
                        const t = (opt.text || opt.label || '').trim();
                        const v = (opt.value || '').trim();
                        if (t.includes('(+' + dialCode + ')') || t.includes(countryName)) return v;
                      }
                      return '';
                    }
                    """,
                    {"dialCode": dial_code, "countryName": country_name},
                )
                if match_value:
                    select_el.select_option(value=match_value)
                    log(f"  ✓ Playwright selectOption(value={match_value}) 成功")
                    time.sleep(0.5)
                    return True
            except Exception as e:
                log(f"  selectOption label 匹配失败: {e}")
    except Exception as e:
        log(f"  Playwright selectOption 策略失败: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # 策略 4: 点击 trigger 按钮打开 listbox，然后在 listbox 中选择
    # ═══════════════════════════════════════════════════════════════════
    trigger = None
    for sel in [
        'button[aria-haspopup="listbox"]',
        '.react-aria-Select button',
        'button[class*="select" i]',
        'button[class*="country" i]',
    ]:
        trigger = page.query_selector(sel)
        if trigger:
            break

    if not trigger:
        trigger = page.evaluate(
            r"""
            () => {
              const pattern = /\(\+\d{1,4}\)/;
              const all = document.querySelectorAll('button, [role="button"], [role="combobox"]');
              for (const el of all) {
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                const text = (el.innerText || '').trim();
                if (pattern.test(text)) {
                  el.scrollIntoView({ block: 'center' });
                  el.click();
                  return true;
                }
              }
              return false;
            }
            """,
        )
        if not trigger:
            log("  ⚠️ 未找到国家选择器触发按钮")
            return False
        log("  已通过 JS 点击触发按钮")
    else:
        trigger.scroll_into_view_if_needed()
        trigger.click()
        log("  已点击国家选择器下拉框")

    time.sleep(0.8)

    # 等待 listbox 出现
    listbox = None
    for _ in range(10):
        listbox = page.query_selector('[role="listbox"]')
        if listbox:
            break
        time.sleep(0.3)

    if not listbox:
        log("  ⚠️ 下拉框 listbox 未出现")
        return False

    log("  listbox 已出现")

    # 在 listbox 中查找并点击目标 option
    option = None
    if iso_code:
        for attr in ["data-key", "data-value", "value", "id"]:
            # 尝试精确匹配和包含匹配
            option = page.query_selector(f'[role="option"][{attr}="{iso_code}"]')
            if not option:
                option = page.query_selector(f'[role="option"][{attr}*="{iso_code}"]')
            if option:
                log(f"  找到 option: [{attr} 含 {iso_code}]")
                break

    if not option:
        option_idx = page.evaluate(
            """
            ({ countryName, dialCode }) => {
              const options = document.querySelectorAll('[role="option"]');
              for (let i = 0; i < options.length; i++) {
                const text = (options[i].innerText || options[i].textContent || '').trim();
                if (text.includes(countryName) || text.includes('(+' + dialCode + ')') || text.includes('+' + dialCode)) {
                  return i;
                }
              }
              // 宽松匹配：只匹配拨号码数字
              for (let i = 0; i < options.length; i++) {
                const text = (options[i].innerText || options[i].textContent || '').trim();
                if (text.includes(dialCode)) {
                  return i;
                }
              }
              return -1;
            }
            """,
            {"countryName": country_name, "dialCode": dial_code},
        )
        if option_idx >= 0:
            options = page.query_selector_all('[role="option"]')
            if option_idx < len(options):
                option = options[option_idx]
                log(f"  找到 option: 文本匹配 index={option_idx}")

    if option:
        option.scroll_into_view_if_needed()
        option.click()
        time.sleep(0.5)
        new_text = page.evaluate(
            """() => {
              const btn = document.querySelector('button[aria-haspopup="listbox"]') ||
                          document.querySelector('.react-aria-Select button');
              return btn ? (btn.innerText || '').trim() : '';
            }""",
        )
        log(f"  选择后下拉框显示: {new_text}")
        if f"+{dial_code}" in (new_text or ""):
            log(f"  ✓ 国家选择成功: {new_text}")
            return True

    # 键盘 type-ahead 搜索
    log(f"  尝试键盘 type-ahead: {country_name}")
    page.keyboard.type(country_name, delay=80)
    time.sleep(0.8)

    # 按 Enter 确认选择
    page.keyboard.press("Enter")
    time.sleep(0.5)

    # 验证
    final_text = page.evaluate(
        """() => {
          const btn = document.querySelector('button[aria-haspopup="listbox"]') ||
                      document.querySelector('.react-aria-Select button');
          return btn ? (btn.innerText || '').trim() : '';
        }""",
    )
    if f"+{dial_code}" in (final_text or ""):
        log(f"  ✓ type-ahead 选择成功: {final_text}")
        return True

    log(f"  ⚠️ 下拉框已展开但未找到匹配国家: {country_name} (+{dial_code})")
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return False


def _build_proxy_config(proxy: Optional[str]) -> Optional[dict]:
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        return {"server": proxy}
    config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config


def _wait_for_url(page, substring: str, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if substring in page.url:
            return True
        time.sleep(1)
    return False


def _find_first_selector(page, selectors: list[str]) -> str | None:
    for sel in selectors:
        try:
            node = page.query_selector(sel)
        except Exception:
            node = None
        if node:
            return sel
    return None


def _wait_for_any_selector(page, selectors: list[str], timeout: int = 30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = _find_first_selector(page, selectors)
        if found:
            return found
        time.sleep(0.5)
    return None


def _click_first(page, selectors: list[str], *, timeout: int = 10) -> str | None:
    found = _wait_for_any_selector(page, selectors, timeout=timeout)
    if not found:
        return None
    try:
        page.click(found)
        return found
    except Exception:
        return None


def _click_first_no_wait(page, selectors: list[str], *, timeout: int = 10) -> str | None:
    """Click a visible element without waiting for navigation.

    OpenAI's add-phone page sometimes leaves the submit XHR pending long enough
    that Playwright reports "Operation timed out" even though the click was
    delivered. This helper treats that as a click problem only after a
    no-wait click and a DOM fallback both fail.
    """
    found = _wait_for_any_selector(page, selectors, timeout=timeout)
    if not found:
        return None
    for kwargs in (
        {"timeout": 3000, "no_wait_after": True},
        {"timeout": 3000, "force": True, "no_wait_after": True},
    ):
        try:
            page.click(found, **kwargs)
            return found
        except Exception:
            pass
    try:
        clicked = bool(
            page.evaluate(
                """
                (selector) => {
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  let target = null;
                  try {
                    target = document.querySelector(selector);
                  } catch (_) {
                    const textMatch = selector.match(/:has-text\\(["'](.+?)["']\\)/);
                    const tag = String(selector.split(':')[0] || 'button').trim() || 'button';
                    const needle = textMatch ? textMatch[1].toLowerCase() : '';
                    target = Array.from(document.querySelectorAll(tag)).find((el) => {
                      const text = String(el.innerText || el.textContent || '').trim().toLowerCase();
                      return visible(el) && (!needle || text.includes(needle));
                    });
                  }
                  if (!target || !visible(target) || target.disabled) return false;
                  target.click();
                  return true;
                }
                """,
                found,
            )
        )
        return found if clicked else None
    except Exception:
        return None


def _phone_page_status(page) -> dict:
    try:
        result = page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              const text = String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
              const addPhoneForm = document.querySelector('form[action*="/add-phone" i]');
              const verificationForm = document.querySelector('form[action*="/phone-verification" i]');
              const codeInput = verificationForm?.querySelector('input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]');
              const addPhoneError = (() => {
                if (!addPhoneForm) return '';
                const selectors = [
                  '.react-aria-FieldError',
                  '[slot="errorMessage"]',
                  '[id$="-error"]',
                  '[data-invalid="true"] + *',
                  '[aria-invalid="true"] + *',
                  '[class*="error" i]'
                ];
                for (const selector of selectors) {
                  for (const el of Array.from(addPhoneForm.querySelectorAll(selector))) {
                    const msg = String(el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (msg) return msg;
                  }
                }
                return '';
              })();
              const verifyError = (() => {
                if (!verificationForm) return '';
                const selectors = [
                  '.react-aria-FieldError',
                  '[slot="errorMessage"]',
                  '[id$="-error"]',
                  '[data-invalid="true"] + *',
                  '[aria-invalid="true"] + *',
                  '[class*="error" i]',
                  '[role="alert"]'
                ];
                for (const selector of selectors) {
                  for (const el of Array.from(verificationForm.querySelectorAll(selector))) {
                    const msg = String(el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (msg) return msg;
                  }
                }
                return '';
              })();
              return {
                url: location.href,
                addPhoneReady: Boolean(addPhoneForm && visible(addPhoneForm.querySelector('input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"], input[name="phoneNumber"]'))),
                phoneVerificationReady: Boolean(verificationForm && codeInput && visible(codeInput)),
                addPhoneError,
                verifyError,
                text,
              };
            }
            """
        )
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _auth_timeout_retry_page_state(page, *, path_patterns: list[str] | None = None) -> dict:
    try:
        result = page.evaluate(
            """
            (pathPatterns) => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              const pathname = String(location.pathname || '');
              if (Array.isArray(pathPatterns) && pathPatterns.length) {
                const matched = pathPatterns.some((raw) => {
                  try { return new RegExp(raw, 'i').test(pathname); } catch (_) { return false; }
                });
                if (!matched) return { retryPage: false, url: location.href, text: '' };
              }
              const text = String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
              const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]'));
              const retryButton = document.querySelector('button[data-dd-action-name="Try again"]')
                || buttons.find((button) => {
                  const label = String([button.value, button.textContent, button.getAttribute?.('aria-label'), button.getAttribute?.('title')].filter(Boolean).join(' '));
                  return visible(button) && /try\\s+again|重试|重試|再試行|もう一度|やり直す/i.test(label);
                });
              return {
                retryPage: Boolean(retryButton && /try\\s+again|重试|重試/i.test(text) && (/oops,?\\s*an\\s*error\\s*occurred|operation\\s+timed\\s+out|route\\s+error|405\\s+method\\s+not\\s+allowed|failed\\s+to\\s+fetch|network\\s+error/i.test(text))),
                retryEnabled: Boolean(retryButton && visible(retryButton) && !retryButton.disabled && retryButton.getAttribute('aria-disabled') !== 'true'),
                url: location.href,
                text,
              };
            }
            """,
            path_patterns or [],
        )
        if isinstance(result, dict):
            result["retryPage"] = bool(result.get("retryPage") or _is_auth_timeout_retry_text(str(result.get("text") or "")))
            return result
    except Exception:
        pass
    return {"retryPage": False, "retryEnabled": False, "url": str(page.url or ""), "text": ""}


def _recover_auth_timeout_retry_page(
    page,
    log,
    *,
    path_patterns: list[str] | None = None,
    max_clicks: int = 3,
    wait_after_click: float = 3.0,
) -> dict:
    last_state = {}
    for attempt in range(1, max_clicks + 1):
        state = _auth_timeout_retry_page_state(page, path_patterns=path_patterns)
        last_state = state
        if not state.get("retryPage"):
            return {"recovered": attempt > 1, "clicks": attempt - 1, "url": str(state.get("url") or page.url)}
        if not state.get("retryEnabled"):
            time.sleep(0.5)
            continue
        log(f"  检测到 OpenAI auth 超时重试页，点击 Try again ({attempt}/{max_clicks})")
        clicked = _click_first_no_wait(
            page,
            [
                'button[data-dd-action-name="Try again"]',
                'button:has-text("Try again")',
                'button:has-text("try again")',
                'button:has-text("重试")',
                'button:has-text("重試")',
                'button:has-text("再試行")',
                'button:has-text("もう一度")',
                'button:has-text("やり直す")',
            ],
            timeout=2,
        )
        if not clicked:
            try:
                clicked = "dom" if page.evaluate(
                    """
                    () => {
                      const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                      };
                      const direct = document.querySelector('button[data-dd-action-name="Try again"]');
                      const target = direct || Array.from(document.querySelectorAll('button, [role="button"]')).find((el) => {
                        const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                        return visible(el) && /try\\s+again|重试|重試/i.test(text);
                      });
                      if (!target) return false;
                      target.click();
                      return true;
                    }
                    """
                ) else ""
            except Exception:
                clicked = ""
        if not clicked:
            break
        time.sleep(wait_after_click)
        state = _auth_timeout_retry_page_state(page, path_patterns=path_patterns)
        last_state = state
        if not state.get("retryPage"):
            return {"recovered": True, "clicks": attempt, "url": str(state.get("url") or page.url)}
    return {
        "recovered": False,
        "clicks": max_clicks,
        "url": str(last_state.get("url") or page.url),
        "text": str(last_state.get("text") or "")[:300],
    }


def _wait_for_phone_verification_ready(page, *, timeout: int = 25) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = _phone_page_status(page)
        if last.get("phoneVerificationReady"):
            return last
        if last.get("addPhoneError"):
            return last
        time.sleep(0.25)
    return last


def _submit_add_phone_dom(
    page,
    *,
    phone_number: str,
    dial_code: str,
    local_number: str,
    country_name: str,
    log,
) -> dict:
    """Submit OpenAI add-phone with GuJumpgate-style DOM state sync."""
    e164 = "+" + str(phone_number or "").lstrip("+").strip()
    national = str(local_number or "").strip() or e164
    iso_code = PHONE_DIAL_TO_ISO.get(str(dial_code or ""), "")
    payload = {
        "phoneNumber": e164,
        "nationalPhoneNumber": national,
        "dialCode": str(dial_code or ""),
        "countryLabel": str(country_name or ""),
        "isoCode": iso_code,
    }
    try:
        result = page.evaluate(
            """
            async (payload) => {
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              const dispatchInputEvents = (el) => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
              };
              const setNativeValue = (el, value) => {
                const proto = el instanceof HTMLTextAreaElement ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(el, value);
                else el.value = value;
                dispatchInputEvents(el);
              };
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
              const form = document.querySelector('form[action*="/add-phone" i]');
              if (!form) return { ok: false, reason: 'missing_add_phone_form', url: location.href };

              const channelInput = form.querySelector('input[name="channel"]');
              const radioEntries = Array.from(form.querySelectorAll('input[type="radio"]')).map((input) => {
                const label = input.closest('label');
                const root = label || input.closest('[role="radio"], [data-state], [class*="option"]') || input;
                const text = normalize([input.value, label?.textContent, root?.textContent, root?.getAttribute?.('aria-label')].filter(Boolean).join(' '));
                const channel = /^(sms)$/i.test(input.value || '') || /\\b(sms|text message)\\b/i.test(text)
                  ? 'sms'
                  : (/whats\\s*app/i.test(text) || /^(whatsapp)$/i.test(input.value || '') ? 'whatsapp' : '');
                return { input, label, root, channel, text };
              }).filter((entry) => entry.channel || entry.text);
              const sms = radioEntries.find((entry) => entry.channel === 'sms');
              if (sms) {
                const target = sms.label || sms.root || sms.input;
                target?.click?.();
                await sleep(120);
                radioEntries.forEach((entry) => {
                  entry.input.checked = entry.input === sms.input;
                  entry.input.dispatchEvent(new Event('input', { bubbles: true }));
                  entry.input.dispatchEvent(new Event('change', { bubbles: true }));
                  entry.label?.setAttribute?.('data-state', entry.input === sms.input ? 'on' : 'off');
                  entry.root?.setAttribute?.('data-state', entry.input === sms.input ? 'on' : 'off');
                });
                if (channelInput) {
                  channelInput.value = 'sms';
                  dispatchInputEvents(channelInput);
                }
              }

              const phoneInput = form.querySelector('input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"]');
              if (!phoneInput) return { ok: false, reason: 'missing_phone_input', url: location.href };
              phoneInput.focus();
              setNativeValue(phoneInput, payload.nationalPhoneNumber);
              phoneInput.dispatchEvent(new Event('blur', { bubbles: true }));

              const hidden = form.querySelector('input[name="phoneNumber"]');
              if (hidden) {
                setNativeValue(hidden, payload.phoneNumber);
              }
              await sleep(120);

              const buttons = Array.from(form.querySelectorAll('button[type="submit"], input[type="submit"], button'));
              const submit = buttons.find((button) => visible(button) && !button.disabled && button.getAttribute('aria-disabled') !== 'true')
                || buttons.find((button) => visible(button));
              if (!submit) return { ok: false, reason: 'missing_submit_button', url: location.href };
              submit.click();
              return {
                ok: true,
                url: location.href,
                selectedCountry: payload.isoCode || payload.dialCode || '',
                channel: channelInput ? channelInput.value : (sms ? 'sms' : ''),
                visibleValue: phoneInput.value || '',
                hiddenValue: hidden ? hidden.value : '',
              };
            }
            """,
            payload,
        )
    except Exception as exc:
        return {"ok": False, "reason": f"dom_exception: {exc}", "url": str(page.url or "")}

    if not isinstance(result, dict):
        result = {"ok": False, "reason": "dom_result_invalid", "url": str(page.url or "")}
    if result.get("ok"):
        log(
            "  add-phone DOM 提交: "
            f"country={result.get('selectedCountry') or iso_code or '-'} "
            f"channel={result.get('channel') or '-'} "
            f"hidden={'yes' if result.get('hiddenValue') else 'no'}"
        )
    return result


def _submit_phone_otp_dom(page, code: str, log) -> dict:
    otp = str(code or "").strip()
    if not otp:
        return {"ok": False, "status": 400, "url": str(page.url or ""), "text": "empty phone otp"}
    try:
        result = page.evaluate(
            """
            async (code) => {
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              const setNativeValue = (el, value) => {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                if (setter) setter.call(el, value);
                else el.value = value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
              };
              const form = document.querySelector('form[action*="/phone-verification" i]');
              if (!form) return { ok: false, reason: 'missing_phone_verification_form', url: location.href };
              const input = form.querySelector('input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]');
              if (!input || !visible(input)) return { ok: false, reason: 'missing_code_input', url: location.href };
              input.focus();
              setNativeValue(input, code);
              input.dispatchEvent(new Event('blur', { bubbles: true }));
              await sleep(120);
              const buttons = Array.from(form.querySelectorAll('button[type="submit"], input[type="submit"], button'));
              const submit = buttons.find((button) => {
                if (!visible(button) || button.disabled || button.getAttribute('aria-disabled') === 'true') return false;
                const text = String([button.value, button.textContent, button.getAttribute('aria-label')].filter(Boolean).join(' '));
                return !/resend/i.test(text);
              }) || buttons.find((button) => visible(button));
              if (!submit) return { ok: false, reason: 'missing_submit_button', url: location.href };
              submit.click();
              return { ok: true, url: location.href, value: input.value || '' };
            }
            """,
            otp,
        )
    except Exception as exc:
        return {"ok": False, "status": 0, "url": str(page.url or ""), "text": f"phone otp dom exception: {exc}"}
    if not isinstance(result, dict) or not result.get("ok"):
        return {
            "ok": False,
            "status": 0,
            "url": str((result or {}).get("url") or page.url),
            "text": str((result or {}).get("reason") or "phone otp dom submit failed"),
        }
    log("  phone-otp DOM 已填写并提交")
    deadline = time.time() + 25
    last_url = str(page.url or "")
    while time.time() < deadline:
        status = _phone_page_status(page)
        current_url = str(status.get("url") or page.url or "")
        last_url = current_url or last_url
        if status.get("verifyError"):
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": str(status.get("verifyError") or "")}
        if "phone-verification" not in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if any(key in current_url for key in ("code=", "consent", "sign-in-with-chatgpt", "workspace", "organization")):
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        time.sleep(0.4)
    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": "phone otp submit stayed on verification page"}


def _is_login_password_url(url: str) -> bool:
    return bool(re.search(r"(?:auth|accounts)\.openai\.com/.*log-?in/password", str(url or ""), flags=re.I))


def _build_manual_flow_state(page_type: str, current_url: str) -> dict:
    state = _extract_flow_state(None, current_url)
    state["page_type"] = page_type
    state["current_url"] = current_url
    return state


def _get_visible_page_text(page) -> str:
    try:
        return str(page.evaluate("() => document.body?.innerText || ''") or "")
    except Exception:
        return ""


def _has_signup_registration_choice(page) -> bool:
    if not _is_login_password_url(str(page.url or "")):
        return False
    if _find_first_selector(page, SIGNUP_RECOVERY_SELECTORS):
        return True
    text = _get_visible_page_text(page)
    return bool(re.search(r"sign\s*up|register|create\s*account|还没有帐户|还没有账户|請註冊|请注册|去注册|注册", text, flags=re.I))


def _click_passwordless_login_if_available(page, log, *, context: str) -> bool:
    selector = _click_first(page, PASSWORDLESS_LOGIN_SELECTORS, timeout=1)
    if selector:
        log(f"{context} 已选择一次性验证码登录: {selector}")
        time.sleep(1)
        return True
    try:
        clicked = bool(
            page.evaluate(
                """
                () => {
                  const nodes = Array.from(document.querySelectorAll('button, [role="button"], a'));
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  const target = nodes.find((el) => {
                    const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    return visible(el) && /使用一次性验证码登录|使用一次性驗證碼登入|one-time code|one time code|passwordless|ワンタイムコード|一回限りのコード|認証コード/i.test(text);
                  });
                  if (!target) return false;
                  target.click();
                  return true;
                }
                """
            )
        )
    except Exception:
        clicked = False
    if clicked:
        log(f"{context} 已选择一次性验证码登录")
        time.sleep(1)
    return clicked


def _get_page_oauth_url(page) -> str:
    try:
        return str(
            page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  const anchors = Array.from(document.querySelectorAll('a[href*="/api/oauth/authorize"], a[href*="/oauth/authorize"]'));
                  const anchor = anchors.find((el) => visible(el));
                  return anchor ? String(anchor.href || anchor.getAttribute('href') || '') : '';
                }
                """
            )
            or ""
        ).strip()
    except Exception:
        return ""


def _oauth_url_matches_state(url: str, state: str) -> bool:
    if not url or not state:
        return False
    return f"state={state}" in url or f"state%3D{state}" in url


def _extract_auth_error_text(page) -> str:
    selectors = [
        "text=Failed to create account",
        "text=Sorry, we cannot create your account",
        "text=Please try again",
        "text=Invalid code",
        "text=Enter a valid age to continue",
        "text=doesn't look right",
        "[role='alert']",
        ".error, [class*='error'], [class*='Error']",
    ]
    for selector in selectors:
        try:
            text = str(page.locator(selector).first.text_content(timeout=350) or "").strip()
        except Exception:
            text = ""
        if text and "oai_log" not in text and "SSR_HTML" not in text:
            return text
    return ""


def _fill_input_like_user(page, selector: str, value: str) -> bool:
    try:
        locator = page.locator(selector).first
        locator.wait_for(state="visible", timeout=2000)
        current = str(locator.input_value() or "").strip()
        if current == str(value).strip():
            return True
        locator.click(timeout=1500)
        _browser_pause(page)
        try:
            locator.fill("")
        except Exception:
            pass
        _browser_pause(page, headed=False)
        try:
            locator.type(value, delay=random.randint(35, 85))
        except Exception:
            try:
                page.fill(selector, value)
            except Exception:
                return False
        final_value = str(locator.input_value() or "").strip()
        if final_value == str(value):
            return True
    except Exception:
        pass

    try:
        ok = page.evaluate(
            """
            ({ selector, value }) => {
              const input = document.querySelector(selector);
              if (!input) return false;
              const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
              if (!setter) return false;
              setter.call(input, value);
              input.dispatchEvent(new Event('input', { bubbles: true }));
              input.dispatchEvent(new Event('change', { bubbles: true }));
              return String(input.value || '') === String(value || '');
            }
            """,
            {"selector": selector, "value": value},
        )
        return bool(ok)
    except Exception:
        return False


def _phone_input_digits_match(actual: str, expected: str) -> bool:
    actual_digits = re.sub(r"\D", "", str(actual or ""))
    expected_digits = re.sub(r"\D", "", str(expected or ""))
    if not actual_digits or not expected_digits:
        return False
    return actual_digits == expected_digits or actual_digits.endswith(expected_digits)


def _fill_signup_phone_input(page, selector: str, value: str, e164: str) -> bool:
    """Fill the formatted phone widget and validate its numeric value.

    The signup widget formats the visible value after each input event. Comparing
    the rendered string with the raw local number therefore reports false
    failures even when the number is present.
    """
    locator = page.locator(selector).first
    try:
        locator.wait_for(state="visible", timeout=2000)
        locator.click(timeout=1500)
        locator.fill("")
        locator.type(value, delay=random.randint(35, 85))
    except Exception:
        try:
            locator.fill(value)
        except Exception:
            return False

    try:
        locator.blur()
    except Exception:
        pass
    try:
        page.wait_for_timeout(150)
    except Exception:
        time.sleep(0.15)

    try:
        actual = str(locator.input_value() or "")
    except Exception:
        actual = ""

    if not _phone_input_digits_match(actual, value):
        try:
            actual = str(
                page.evaluate(
                    """
                    ({ selector, value }) => {
                      const input = document.querySelector(selector);
                      if (!input) return '';
                      const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                      )?.set;
                      if (setter) setter.call(input, value); else input.value = value;
                      input.dispatchEvent(new InputEvent('input', {
                        bubbles: true,
                        inputType: 'insertText',
                        data: value,
                      }));
                      input.dispatchEvent(new Event('change', { bubbles: true }));
                      input.dispatchEvent(new Event('blur', { bubbles: true }));
                      return String(input.value || '');
                    }
                    """,
                    {"selector": selector, "value": value},
                )
                or ""
            )
        except Exception:
            return False

    if not _phone_input_digits_match(actual, value):
        return False

    # Some versions of the signup form keep a separate hidden E.164 field.
    # Do not overwrite the formatted visible input when both share a similar name.
    try:
        page.evaluate(
            """
            (value) => {
              const inputs = document.querySelectorAll(
                'input[type="hidden"][name="phoneNumber"], input[type="hidden"][name*="phone" i]'
              );
              const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
              )?.set;
              for (const input of inputs) {
                if (setter) setter.call(input, value); else input.value = value;
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
              }
              return inputs.length;
            }
            """,
            e164,
        )
    except Exception:
        pass
    return True


def _submit_form_with_fallback(page, input_selector: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                (selector) => {
                  const input = document.querySelector(selector);
                  if (!input) return false;
                  const form = input.form || input.closest?.('form');
                  if (form?.requestSubmit) {
                    form.requestSubmit();
                    return true;
                  }
                  if (form?.submit) {
                    form.submit();
                    return true;
                  }
                  input.focus?.();
                  for (const type of ['keydown', 'keypress', 'keyup']) {
                    input.dispatchEvent(new KeyboardEvent(type, {
                      key: 'Enter',
                      code: 'Enter',
                      bubbles: true,
                      cancelable: true,
                    }));
                  }
                  return true;
                }
                """,
                input_selector,
            )
        )
    except Exception:
        return False


ABOUT_YOU_SUBMIT_SELECTORS = [
    'button:has-text("Finish creating account")',
    'button:has-text("finish creating account")',
    'button:has-text("Create account")',
    'button:has-text("create account")',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Next")',
    'button:has-text("next")',
    '[role="button"]:has-text("Finish creating account")',
    '[role="button"]:has-text("Create account")',
    '[role="button"]:has-text("Continue")',
    '[role="button"]:has-text("Next")',
    'button[type="submit"]',
    'input[type="submit"]',
    'button[data-testid="continue-button"]',
    'button[data-dd-action-name="Continue"]',
    'button[data-dd-action-name="Create account"]',
    'button:has-text("继续")',
    'button:has-text("下一步")',
    'button:has-text("完成")',
    'button:has-text("创建账户")',
    'button:has-text("建立帳戶")',
    'button:has-text("続ける")',
    'button:has-text("続行")',
    'button:has-text("次へ")',
    'button:has-text("完了")',
]


def _visible_button_summary(page, *, limit: int = 8) -> str:
    try:
        items = page.evaluate(
            """
            (limit) => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              return Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"], input[type="button"]'))
                .filter(visible)
                .slice(0, limit)
                .map((el) => {
                  const label = String([
                    el.value,
                    el.innerText,
                    el.textContent,
                    el.getAttribute?.('aria-label'),
                    el.getAttribute?.('title'),
                    el.getAttribute?.('data-dd-action-name'),
                  ].filter(Boolean).join(' ')).replace(/\\s+/g, ' ').trim();
                  const disabled = Boolean(el.disabled || el.getAttribute?.('aria-disabled') === 'true');
                  const role = el.getAttribute?.('role');
                  return `${el.tagName.toLowerCase()}${role ? `[role=${role}]` : ''}${disabled ? '[disabled]' : ''}:${label || '-'}`;
                });
            }
            """,
            limit,
        )
        if isinstance(items, list):
            return " | ".join(str(item)[:120] for item in items if str(item or "").strip())
    except Exception:
        pass
    return ""


def _click_about_you_submit(page, *, timeout: int = 12) -> str | None:
    deadline = time.time() + max(1, timeout)
    while time.time() < deadline:
        for selector in ABOUT_YOU_SUBMIT_SELECTORS:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=350)
                try:
                    if locator.is_disabled(timeout=200):
                        continue
                except Exception:
                    pass
                locator.click(timeout=1200, no_wait_after=True)
                return selector
            except Exception:
                continue
        try:
            result = page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  const enabled = (el) => visible(el) && !el.disabled && el.getAttribute?.('aria-disabled') !== 'true';
                  const labelOf = (el) => String([
                    el.value,
                    el.innerText,
                    el.textContent,
                    el.getAttribute?.('aria-label'),
                    el.getAttribute?.('title'),
                    el.getAttribute?.('data-dd-action-name'),
                  ].filter(Boolean).join(' ')).replace(/\\s+/g, ' ').trim();
                  const submitPattern = /finish\\s+creating\\s+account|create\\s+account|continue|next|submit|done|完成|继续|繼續|下一步|创建账户|建立帳戶|続ける|続行|次へ|完了|登録|continuar|continuer|fortfahren|weiter|siguiente/i;
                  const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"], input[type="button"]'));
                  const target = buttons.find((el) => enabled(el) && submitPattern.test(labelOf(el)))
                    || buttons.find((el) => enabled(el) && el.matches?.('button[type="submit"], input[type="submit"]'));
                  if (target) {
                    const form = target.form || target.closest?.('form');
                    if (form && target.matches?.('button[type="submit"], input[type="submit"]') && typeof form.requestSubmit === 'function') {
                      form.requestSubmit(target);
                    } else {
                      target.click();
                    }
                    return `dom:${labelOf(target) || target.tagName.toLowerCase()}`;
                  }
                  const forms = Array.from(document.querySelectorAll('form')).filter(visible);
                  const form = forms.find((candidate) => {
                    const text = String(candidate.innerText || candidate.textContent || '').toLowerCase();
                    const inputs = Array.from(candidate.querySelectorAll('input'));
                    return /full\\s*name|age|birthday|birth|姓名|年龄|生日/i.test(text)
                      || inputs.some((input) => /name|age|birth/i.test(String([input.name, input.id, input.placeholder, input.getAttribute('aria-label')].filter(Boolean).join(' '))));
                  }) || forms[0];
                  if (form && typeof form.requestSubmit === 'function') {
                    form.requestSubmit();
                    return 'form:requestSubmit';
                  }
                  if (form && typeof form.submit === 'function') {
                    form.submit();
                    return 'form:submit';
                  }
                  return '';
                }
                """
            )
            if result:
                return str(result)
        except Exception:
            pass
        time.sleep(0.35)
    return None


def _sync_hidden_birthday_input(page, birthdate: str, log) -> bool:
    try:
        synced = bool(
            page.evaluate(
                """
                (value) => {
                  const input = document.querySelector("input[name='birthday']");
                  if (!input) return false;
                  input.value = value;
                  input.dispatchEvent(new Event('input', { bubbles: true }));
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                  return String(input.value || '') === String(value || '');
                }
                """,
                birthdate,
            )
        )
    except Exception:
        synced = False
    if synced:
        log(f"about_you 已同步隐藏 birthday: {birthdate}")
    return synced


def _collect_visible_text_inputs(page) -> list[dict]:
    try:
        inputs = page.evaluate(
            """
            () => {
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
              const nodes = Array.from(document.querySelectorAll("input:not([type='hidden']):not([disabled]):not([readonly])"));
              const visible = nodes.filter((el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style
                  && style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && rect.width > 0
                  && rect.height > 0;
              });
              return visible.map((el, visibleIndex) => {
                const explicitLabels = Array.from(document.querySelectorAll('label'))
                  .filter((label) => String(label.getAttribute('for') || '') === String(el.id || ''))
                  .map((label) => normalize(label.textContent));
                const wrappedLabel = normalize(el.closest('label')?.textContent || '');
                const ariaLabel = normalize(el.getAttribute('aria-label'));
                const labelledByText = normalize(
                  String(el.getAttribute('aria-labelledby') || '')
                    .split(/\\s+/)
                    .filter(Boolean)
                    .map((id) => normalize(document.getElementById(id)?.textContent || ''))
                    .join(' ')
                );
                const parentText = normalize(el.parentElement?.textContent || '');
                return {
                  visibleIndex,
                  type: normalize(el.getAttribute('type') || el.type || ''),
                  name: normalize(el.getAttribute('name') || ''),
                  id: normalize(el.id || ''),
                  placeholder: normalize(el.getAttribute('placeholder') || ''),
                  ariaLabel,
                  labels: explicitLabels.filter(Boolean),
                  wrappedLabel,
                  labelledByText,
                  parentText,
                };
              });
            }
            """
        ) or []
    except Exception:
        inputs = []
    return [item for item in inputs if isinstance(item, dict)]


def _about_you_input_hints(entry: dict) -> str:
    parts: list[str] = []
    labels = entry.get("labels") or []
    if isinstance(labels, list):
        parts.extend(str(item or "") for item in labels)
    parts.extend(
        [
            str(entry.get("wrappedLabel") or ""),
            str(entry.get("labelledByText") or ""),
            str(entry.get("ariaLabel") or ""),
            str(entry.get("placeholder") or ""),
            str(entry.get("name") or ""),
            str(entry.get("id") or ""),
            str(entry.get("parentText") or ""),
        ]
    )
    return " ".join(part for part in parts if part).strip().lower()


def _pick_best_about_you_input(entries: list[dict], field: str, exclude_visible_indices: set[int] | None = None) -> dict | None:
    exclude = {int(value) for value in (exclude_visible_indices or set())}
    best_entry = None
    best_score = float("-inf")
    for entry in entries:
        try:
            visible_index = int(entry.get("visibleIndex"))
        except Exception:
            continue
        if visible_index in exclude:
            continue
        hints = _about_you_input_hints(entry)
        if not hints:
            continue

        score = 0
        if field == "name":
            if any(token in hints for token in ("full name", "fullname", "全名", "姓名", "nombre completo", "nom complet", "vollständiger name", "nome completo")):
                score += 10
            if any(token in hints for token in (" name ", "name", "autocomplete=name", "nombre", "nom", "nome")):
                score += 3
            if any(token in hints for token in ("age", "年龄", "edad", "âge", "alter", "idade", "birthday", "birth", "date of birth", "出生", "生日")):
                score -= 8
        elif field == "age":
            if any(token in hints for token in ("age", "年龄", "how old", "edad", "âge", "alter", "idade", "나이")):
                score += 10
            if any(token in hints for token in ("full name", "fullname", "全名", "姓名", "nombre completo", "nom complet")):
                score -= 10
            if "name" in hints and "age" not in hints and "年龄" not in hints and "edad" not in hints:
                score -= 6
            if any(token in hints for token in ("birthday", "birth", "date of birth", "出生", "生日", "fecha de nacimiento", "nascimento")):
                score -= 3
        else:
            continue

        if score > best_score:
            best_score = score
            best_entry = entry

    if best_score > 0:
        return best_entry

    if field == "age" and len(entries) == 2:
        ordered = []
        for entry in entries:
            try:
                visible_index = int(entry.get("visibleIndex"))
            except Exception:
                continue
            if visible_index not in exclude:
                ordered.append(entry)
        if len(ordered) == 1:
            return ordered[0]
        if len(ordered) == 2:
            return ordered[1]
    return None


def _derive_registration_state_from_page(page) -> dict:
    current_url = str(page.url or "")
    state = _extract_flow_state(None, current_url)
    if state.get("page_type"):
        return state

    if _find_first_selector(page, PASSWORD_INPUT_SELECTORS):
        page_type = "login_password" if _is_login_password_url(current_url) else "create_account_password"
        return _build_manual_flow_state(page_type, current_url)

    phone_status = _phone_page_status(page)
    if phone_status.get("phoneVerificationReady"):
        return _build_manual_flow_state("phone_otp_verification", current_url)

    otp_selector = _find_first_selector(page, OTP_INPUT_SELECTORS)
    if otp_selector and "password" not in otp_selector:
        return _build_manual_flow_state("email_otp_verification", current_url)

    try:
        about_visible = bool(
            page.evaluate(
                """
                () => {
                  const inputs = Array.from(document.querySelectorAll("input:not([type='hidden'])"));
                  const text = String(document.body?.innerText || '').toLowerCase();
                  const hasName = inputs.some((el) => {
                    const hint = `${el.name || ''} ${el.id || ''} ${el.placeholder || ''}`.toLowerCase();
                    return hint.includes('name') || hint.includes('姓名') || hint.includes('全名');
                  });
                  const hasAgeOrBirth = inputs.some((el) => {
                    const hint = `${el.name || ''} ${el.id || ''} ${el.placeholder || ''}`.toLowerCase();
                    return hint.includes('age') || hint.includes('birth') || hint.includes('birthday') || hint.includes('年龄') || hint.includes('生日');
                  });
                  return (hasName && hasAgeOrBirth) || text.includes('about you');
                }
                """
            )
        )
    except Exception:
        about_visible = False
    if about_visible:
        return _build_manual_flow_state("about_you", current_url)

    return state


def _recover_signup_password_page(page, log) -> bool:
    if not _is_login_password_url(str(page.url or "")):
        return False
    if not _has_signup_registration_choice(page):
        return False
    selector = _click_first(page, SIGNUP_RECOVERY_SELECTORS, timeout=2)
    if not selector:
        return False
    log(f"密码页落到登录态，尝试点击注册入口恢复: {selector}")
    time.sleep(1.2)
    return True


def _wait_for_signup_entry_transition(page, log, timeout: int = 20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _click_passwordless_login_if_available(page, log, context="邮箱页提交后"):
            time.sleep(0.5)
            continue
        state = _derive_registration_state_from_page(page)
        if state.get("page_type") in {
            "create_account_password",
            "login_password",
            "email_otp_verification",
            "about_you",
            "add_phone",
            "chatgpt_home",
            "oauth_callback",
        }:
            if state.get("page_type") == "login_password" and _recover_signup_password_page(page, log):
                return _derive_registration_state_from_page(page)
            return state
        error_text = _extract_auth_error_text(page)
        if error_text:
            raise RuntimeError(f"邮箱页提交失败: {error_text[:300]}")
        time.sleep(0.25)
    raise RuntimeError("邮箱页提交后未进入密码/验证码页面")


def _start_browser_signup_via_page(page, email: str, log) -> dict:
    for entry_url in (PLATFORM_LOGIN_ENTRY, f"{OPENAI_AUTH}/log-in"):
        try:
            log(f"打开 OpenAI 注册入口: {entry_url}")
            _goto_with_retry(page, entry_url, wait_until="domcontentloaded", timeout=30000, log=log)
        except Exception as exc:
            log(f"注册入口访问失败: {entry_url} -> {exc}")
            continue

        initial_state = _derive_registration_state_from_page(page)
        if initial_state.get("page_type") in {
            "create_account_password",
            "login_password",
            "email_otp_verification",
            "about_you",
            "add_phone",
        }:
            return initial_state

        email_selector = _wait_for_any_selector(page, EMAIL_INPUT_SELECTORS, timeout=12)
        if not email_selector:
            continue
        if not _fill_input_like_user(page, email_selector, email):
            raise RuntimeError("邮箱页填写失败")
        log(f"邮箱页输入框: {email_selector}")

        inline_state = _derive_registration_state_from_page(page)
        if inline_state.get("page_type") in {"create_account_password", "login_password"}:
            if inline_state.get("page_type") == "login_password" and _recover_signup_password_page(page, log):
                return _derive_registration_state_from_page(page)
            return inline_state

        submit_selector = _click_first(page, EMAIL_SUBMIT_SELECTORS, timeout=8)
        if submit_selector:
            log(f"邮箱页已点击继续按钮: {submit_selector}")
        elif _submit_form_with_fallback(page, email_selector):
            log("邮箱页未找到可点击 Continue，已使用表单 fallback 提交")
        else:
            raise RuntimeError("邮箱页未找到 Continue 按钮")

        return _wait_for_signup_entry_transition(page, log)

    raise RuntimeError("未找到 OpenAI 注册入口邮箱输入框")


def _start_browser_signup_via_authorize(page, email: str, device_id: str, log) -> dict:
    log("访问 ChatGPT 首页...")
    _goto_with_retry(page, f"{CHATGPT_APP}/", wait_until="domcontentloaded", timeout=30000, log=log)

    log("获取 CSRF token...")
    csrf_token = _get_browser_csrf_token(page)
    if not csrf_token:
        raise RuntimeError("获取 CSRF token 失败")

    log(f"提交邮箱: {email}")
    authorize_url = _start_browser_signin(page, email, device_id, csrf_token)
    if not authorize_url:
        raise RuntimeError("提交邮箱失败，未获取 authorize URL")

    final_url = _browser_authorize(page, authorize_url, log)
    if not final_url:
        raise RuntimeError("访问 authorize URL 失败")
    return _derive_registration_state_from_page(page)


def _dump_debug(page, prefix: str) -> None:
    page.screenshot(path=f"/tmp/{prefix}.png")
    with open(f"/tmp/{prefix}.html", "w") as f:
        f.write(page.content())


def _get_cookies(page) -> dict:
    return {c["name"]: c["value"] for c in page.context.cookies()}


def _cookies_to_header(cookies_dict: dict) -> str:
    parts = []
    for name, value in (cookies_dict or {}).items():
        if name and value not in (None, ""):
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _decode_jwt_payload_no_verify(token: str) -> dict:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_chatgpt_account_id(access_token: str) -> str:
    payload = _decode_jwt_payload_no_verify(access_token)
    auth_info = payload.get("https://api.openai.com/auth") or {}
    if isinstance(auth_info, dict):
        account_id = str(auth_info.get("chatgpt_account_id") or "").strip()
        if account_id:
            return account_id
    return str(payload.get("sub") or "").strip()


def _chatgpt_session_result_from_data(data: dict, page, cookies_dict: dict, log) -> tuple[dict | None, str]:
    if not isinstance(data, dict):
        return None, "session API JSON 不是对象"

    access_token = str(data.get("accessToken") or data.get("access_token") or "").strip()
    if not access_token:
        return None, "session API 未返回 accessToken"

    latest_cookies = dict(cookies_dict or {})
    try:
        latest_cookies.update(_get_cookies(page))
    except Exception as exc:
        log(f"ChatGPT session cookies 读取失败，使用已捕获 cookies: {exc}")
    session_token = str(latest_cookies.get("__Secure-next-auth.session-token") or "").strip()
    account_id = _extract_chatgpt_account_id(access_token)
    result = {
        "access_token": access_token,
        "refresh_token": str(data.get("refreshToken") or data.get("refresh_token") or "").strip(),
        "id_token": str(data.get("idToken") or data.get("id_token") or "").strip(),
        "session_token": session_token,
        "account_id": account_id,
        "workspace_id": str(data.get("workspaceId") or data.get("workspace_id") or "").strip(),
        "profile": data.get("user") if isinstance(data.get("user"), dict) else {},
        "expires_at": str(data.get("expires") or "").strip(),
        "cookies": _cookies_to_header(latest_cookies),
        "session": data,
    }
    log(
        "ChatGPT session 获取成功: "
        f"accessToken=yes, session_token={'yes' if session_token else 'no'}, "
        f"account_id={account_id or '-'}"
    )
    return result, ""


def _chatgpt_session_result_from_text(text: str, page, cookies_dict: dict, log) -> tuple[dict | None, str]:
    try:
        data = json.loads(text)
    except Exception as exc:
        return None, f"session API JSON 解析失败: {exc}"
    return _chatgpt_session_result_from_data(data, page, cookies_dict, log)


def _fetch_chatgpt_session_via_same_origin(page, cookies_dict: dict, log, session_url: str) -> tuple[dict | None, str, bool]:
    current_url = str(getattr(page, "url", "") or "")
    if "chatgpt.com" not in current_url.lower():
        return None, "", False

    log(f"浏览器内请求 ChatGPT session API: {session_url}")
    try:
        payload = page.evaluate(
            """
            async (sessionUrl) => {
              const response = await fetch(sessionUrl, {
                method: "GET",
                credentials: "include",
                headers: { "accept": "application/json" },
              });
              return {
                status: response.status,
                url: response.url,
                text: await response.text(),
              };
            }
            """,
            session_url,
        )
    except Exception as exc:
        return None, str(exc), True

    if not isinstance(payload, dict):
        return None, "session API 浏览器内请求未返回对象", True

    status = int(payload.get("status") or 0)
    response_url = str(payload.get("url") or "")
    text = str(payload.get("text") or "")
    log(f"ChatGPT session API 浏览器内请求状态: {status} url={response_url[:120]}")
    if status == 200 and text:
        return (*_chatgpt_session_result_from_text(text, page, cookies_dict, log), True)
    return None, f"session API HTTP {status}: {text[:200]}", True


def _fetch_chatgpt_session_from_page(page, cookies_dict: dict, log, timeout: int = 45) -> dict:
    deadline = time.time() + max(int(timeout or 0), 5)
    last_error = ""
    session_url = f"{CHATGPT_APP}/api/auth/session"
    log(f"打开 ChatGPT session API: {session_url}")

    while time.time() < deadline:
        same_origin_result, same_origin_error, same_origin_attempted = _fetch_chatgpt_session_via_same_origin(
            page,
            cookies_dict,
            log,
            session_url,
        )
        if same_origin_result:
            return same_origin_result
        if same_origin_attempted and same_origin_error:
            last_error = same_origin_error
            log(f"ChatGPT session API 浏览器内请求暂未拿到 token: {last_error}")
            if "object has no attribute 'evaluate'" not in last_error:
                time.sleep(2)
                continue

        try:
            response = page.goto(session_url, wait_until="domcontentloaded", timeout=15000)
            status = int(response.status if response else 0)
            if response:
                try:
                    text = response.text()
                except Exception as body_exc:
                    last_error = str(body_exc)
                    log(f"ChatGPT session API 响应体不可直接读取，改读页面正文: {last_error}")
                    text = page.locator("body").inner_text(timeout=3000)
            else:
                text = page.locator("body").inner_text(timeout=3000)
            current_url = str(getattr(page, "url", "") or "")
            log(f"ChatGPT session API 状态: {status} url={current_url[:120]}")
            if status == 200 and text:
                result, error = _chatgpt_session_result_from_text(text, page, cookies_dict, log)
                if result:
                    return result
                last_error = error
            else:
                last_error = f"session API HTTP {status}: {text[:200]}"
            log(f"ChatGPT session API 暂未拿到 token: {last_error}")
        except Exception as exc:
            last_error = str(exc)
            log(f"ChatGPT session API 打开异常: {last_error}")
        time.sleep(2)

    raise RuntimeError(f"ChatGPT session 未返回 accessToken: {last_error}")


def _random_chrome_ua() -> str:
    patch = random.randint(0, 220)
    return (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/136.0.7103.{patch} Safari/537.36"
    )


def _infer_sec_ch_ua(user_agent: str) -> str:
    match = re.search(r"Chrome/(\d+)", str(user_agent or ""))
    major = str(match.group(1) if match else "136")
    return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not.A/Brand";v="99"'


def _build_browser_headers(
    *,
    user_agent: str,
    accept: str,
    referer: str = "",
    origin: str = "",
    content_type: str = "",
    navigation: bool = False,
    extra_headers: dict | None = None,
) -> dict:
    headers = {
        "user-agent": user_agent or _random_chrome_ua(),
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": _infer_sec_ch_ua(user_agent),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "accept": accept,
    }
    if referer:
        headers["referer"] = referer
    if origin:
        headers["origin"] = origin
    if content_type:
        headers["content-type"] = content_type
    if navigation:
        headers["sec-fetch-dest"] = "document"
        headers["sec-fetch-mode"] = "navigate"
        headers["sec-fetch-user"] = "?1"
        headers["upgrade-insecure-requests"] = "1"
    else:
        headers["sec-fetch-dest"] = "empty"
        headers["sec-fetch-mode"] = "cors"
    for key, value in dict(extra_headers or {}).items():
        if value is not None:
            headers[key] = value
    return headers


def _browser_pause(page, *, headed: bool = True):
    delay_ms = random.randint(150, 450) if headed else random.randint(60, 180)
    try:
        page.wait_for_timeout(delay_ms)
    except Exception:
        time.sleep(delay_ms / 1000)


def _generate_datadog_trace_headers() -> dict:
    trace_hex = secrets.token_hex(8).rjust(16, "0")
    parent_hex = secrets.token_hex(8).rjust(16, "0")
    trace_id = str(int(trace_hex, 16))
    parent_id = str(int(parent_hex, 16))
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _infer_page_type(data: dict | None, current_url: str = "") -> str:
    raw = data if isinstance(data, dict) else {}
    page_type = str(((raw.get("page") or {}).get("type")) or "").strip().lower().replace("-", "_").replace("/", "_").replace(" ", "_")
    if page_type:
        return {
            "phone_verification": "phone_otp_verification",
            "phone_otp": "phone_otp_verification",
            "verify_phone": "phone_otp_verification",
        }.get(page_type, page_type)
    url = (current_url or "").lower()
    if "code=" in url:
        return "oauth_callback"
    if "create-account/password" in url:
        return "create_account_password"
    if "email-verification" in url or "email-otp" in url:
        return "email_otp_verification"
    if "about-you" in url:
        return "about_you"
    if "phone-verification" in url:
        return "phone_otp_verification"
    if "log-in/password" in url:
        return "login_password"
    if "sign-in-with-chatgpt" in url and "consent" in url:
        return "consent"
    if "workspace" in url and "select" in url:
        return "workspace_selection"
    if "organization" in url and "select" in url:
        return "organization_selection"
    if "add-phone" in url:
        return "add_phone"
    if "/api/oauth/oauth2/auth" in url:
        return "external_url"
    if "chatgpt.com" in url:
        return "chatgpt_home"
    return ""


def _extract_flow_state(data: dict | None, current_url: str = "") -> dict:
    raw = data if isinstance(data, dict) else {}
    page = raw.get("page") or {}
    payload = page.get("payload") or {}
    continue_url = str(raw.get("continue_url") or payload.get("url") or "").strip()
    if continue_url and continue_url.startswith("/"):
        continue_url = urljoin(OPENAI_AUTH, continue_url)
    effective_url = continue_url or current_url
    return {
        "page_type": _infer_page_type(raw, effective_url),
        "continue_url": continue_url,
        "method": str(raw.get("method") or payload.get("method") or "GET").upper(),
        "current_url": effective_url,
        "payload": payload if isinstance(payload, dict) else {},
        "raw": raw,
    }


def _extract_code_from_url(url: str) -> str:
    if not url or "code=" not in url:
        return ""
    try:
        from urllib.parse import parse_qs, urlparse as _up

        parsed = _up(url)
        values = parse_qs(parsed.query, keep_blank_values=True)
        return str((values.get("code") or [""])[0] or "").strip()
    except Exception:
        return ""


def _normalize_url(target_url: str, base_url: str = OPENAI_AUTH) -> str:
    value = str(target_url or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    try:
        return urljoin(base_url, value)
    except Exception:
        return value


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        pad = "=" * ((4 - (len(payload) % 4)) % 4)
        return json.loads(base64.urlsafe_b64decode((payload + pad).encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


class _SentinelTokenGenerator:
    def __init__(self, device_id: str, user_agent: str):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or _random_chrome_ua()
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= (h >> 16)
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= (h >> 13)
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= (h >> 16)
        return f"{h & 0xFFFFFFFF:08x}"

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":")).encode("utf-8")).decode("ascii")

    def _config(self) -> list:
        perf_now = 1000 + random.random() * 49000
        return [
            "1920x1080",
            time.strftime("%a, %d %b %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            SENTINEL_SDK_URL,
            None,
            None,
            "en-US",
            "en-US,en",
            random.random(),
            "webkitTemporaryStorage−undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            int(time.time() * 1000 - perf_now),
        ]

    def generate_requirements_token(self) -> str:
        cfg = self._config()
        cfg[3] = 1
        cfg[9] = round(5 + random.random() * 45)
        return "gAAAAAC" + self._b64(cfg)

    def generate_token(self, seed: str, difficulty: str) -> str:
        max_attempts = 500000
        cfg = self._config()
        start_ms = int(time.time() * 1000)
        diff = str(difficulty or "0")
        for nonce in range(max_attempts):
            cfg[3] = nonce
            cfg[9] = round(int(time.time() * 1000) - start_ms)
            encoded = self._b64(cfg)
            digest = self._fnv1a32((seed or "") + encoded)
            if digest[: len(diff)] <= diff:
                return "gAAAAAB" + encoded + "~S"
        return "gAAAAAB" + self._b64(None)


def _browser_fetch(page, url: str, *, method: str = "GET", headers: dict | None = None, body: str | None = None, redirect: str = "manual", timeout_ms: int = 30000) -> dict:
    return page.evaluate(
        """
        async ({ url, method, headers, body, redirect, timeoutMs }) => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(new Error(`fetch timeout after ${timeoutMs}ms`)), timeoutMs);
          try {
            const resp = await fetch(url, {
              method,
              headers: headers || {},
              body: body === null ? undefined : body,
              redirect,
              signal: controller.signal,
            });
            const respHeaders = {};
            resp.headers.forEach((v, k) => { respHeaders[k] = v; });
            let text = '';
            try { text = await resp.text(); } catch {}
            let data = null;
            try { data = JSON.parse(text); } catch {}
            return { ok: resp.ok, status: resp.status, url: resp.url || url, headers: respHeaders, text, data };
          } catch (e) {
            return { ok: false, status: 0, url, headers: {}, text: String(e && e.message || e), data: null };
          } finally {
            clearTimeout(timer);
          }
        }
        """,
        {
            "url": url,
            "method": method,
            "headers": headers or {},
            "body": body,
            "redirect": redirect,
            "timeoutMs": timeout_ms,
        },
    )


def _build_browser_sentinel_token(page, device_id: str, flow: str, user_agent: str) -> str:
    generator = _SentinelTokenGenerator(device_id, user_agent)
    req_body = json.dumps(
        {"p": generator.generate_requirements_token(), "id": device_id, "flow": flow},
        separators=(",", ":"),
    )
    result = _browser_fetch(
        page,
        SENTINEL_REQ_URL,
        method="POST",
        headers=_build_browser_headers(
            user_agent=user_agent,
            accept="*/*",
            referer=SENTINEL_FRAME_URL,
            origin=SENTINEL_BASE,
            content_type="text/plain;charset=UTF-8",
            extra_headers={
                "sec-fetch-site": "same-origin",
            },
        ),
        body=req_body,
        redirect="follow",
    )
    data = result.get("data") or {}
    challenge_token = str(data.get("token") or "").strip()
    if not challenge_token:
        return ""
    pow_meta = data.get("proofofwork") or {}
    if pow_meta.get("required") and pow_meta.get("seed"):
        p_value = generator.generate_token(str(pow_meta.get("seed") or ""), str(pow_meta.get("difficulty") or "0"))
    else:
        p_value = generator.generate_requirements_token()
    return json.dumps(
        {
            "p": p_value,
            "t": "",
            "c": challenge_token,
            "id": device_id,
            "flow": flow,
        },
        separators=(",", ":"),
    )


def _submit_browser_user_register(page, email: str, password: str, device_id: str, user_agent: str) -> dict:
    headers = _build_browser_headers(
        user_agent=user_agent,
        accept="application/json",
        referer=f"{OPENAI_AUTH}/create-account/password",
        origin=OPENAI_AUTH,
        content_type="application/json",
        extra_headers={
            "sec-fetch-site": "same-origin",
            "oai-device-id": device_id,
            **_generate_datadog_trace_headers(),
        },
    )
    sentinel = _build_browser_sentinel_token(page, device_id, "username_password_create", user_agent)
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    _browser_pause(page)
    return _browser_fetch(
        page,
        f"{OPENAI_AUTH}/api/accounts/user/register",
        method="POST",
        headers=headers,
        body=json.dumps({"username": email, "password": password}),
        redirect="follow",
    )


def _send_browser_email_otp(page) -> dict:
    _browser_pause(page)
    return _browser_fetch(
        page,
        f"{OPENAI_AUTH}/api/accounts/email-otp/send",
        method="GET",
        headers={
            "accept": "application/json, text/plain, */*",
            "referer": f"{OPENAI_AUTH}/create-account/password",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "accept-language": "en-US,en;q=0.9",
        },
        redirect="follow",
    )


def _decode_oauth_session_cookie(cookies_dict: dict) -> dict:
    raw = str(cookies_dict.get("oai-client-auth-session") or "").strip()
    if not raw:
        return {}
    first = raw.split(".")[0]
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            pad = "=" * ((4 - (len(first) % 4)) % 4)
            decoded = decoder((first + pad).encode("ascii")).decode("utf-8")
            parsed = json.loads(decoded)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return {}


def _extract_workspace_from_consent_html(session, consent_url: str) -> dict:
    try:
        response = session.get(consent_url, allow_redirects=True, timeout=30)
        html = response.text or ""
        if "workspaces" not in html:
            return {}
        ids = re.findall(r'"id"(?:,|:)"([0-9a-f-]{36})"', html, flags=re.I)
        kinds = re.findall(r'"kind"(?:,|:)"([^"]+)"', html, flags=re.I)
        if not ids:
            return {}
        seen: set[str] = set()
        workspaces: list[dict] = []
        for idx, workspace_id in enumerate(ids):
            if workspace_id in seen:
                continue
            seen.add(workspace_id)
            item = {"id": workspace_id}
            if idx < len(kinds):
                item["kind"] = kinds[idx]
            workspaces.append(item)
        return {"workspaces": workspaces} if workspaces else {}
    except Exception:
        return {}


def _seed_session_cookies(session, cookies_dict: dict):
    for name, value in cookies_dict.items():
        for domain in [".openai.com", ".chatgpt.com", ".auth.openai.com", "auth.openai.com", "chatgpt.com"]:
            try:
                session.cookies.set(name, value, domain=domain, path="/")
            except Exception:
                pass


def _follow_redirects_for_code(session, start_url: str, log, *, max_redirects: int = 12) -> str:
    current_url = start_url
    for idx in range(max_redirects):
        response = session.get(current_url, allow_redirects=False, timeout=30)
        log(f"  redirect-follow[{idx+1}] {response.status_code} {str(current_url)[:140]}")
        location = str(response.headers.get("Location") or "").strip()
        if not location:
            break
        next_url = urljoin(current_url, location)
        code = _extract_code_from_url(next_url)
        if code:
            return next_url
        if response.status_code not in (301, 302, 303, 307, 308):
            break
        current_url = next_url
    return ""


def _complete_oauth_with_session(cookies_dict: dict, oauth_start, proxy: str | None, log) -> dict | None:
    from .oauth import normalize_oauth_token_response, submit_callback_url
    from curl_cffi import requests as cffi_requests

    s = cffi_requests.Session(impersonate="chrome131")
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    _seed_session_cookies(s, cookies_dict)

    try:
        session_meta = _decode_oauth_session_cookie(cookies_dict)
        consent_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
        workspaces = list(session_meta.get("workspaces") or [])
        if not workspaces:
            session_meta = _extract_workspace_from_consent_html(s, consent_url)
            workspaces = list(session_meta.get("workspaces") or [])
        if not workspaces:
            log("  ⚠️ 缺少 oai-client-auth-session workspaces，OAuth 失败")
            return None
        workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
        log(f"  选择 workspace: {workspace_id}")
        ws_resp = s.post(
            "https://auth.openai.com/api/accounts/workspace/select",
            headers={
                "accept": "application/json",
                "referer": consent_url,
                "origin": OPENAI_AUTH,
                "content-type": "application/json",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            },
            data=json.dumps({"workspace_id": workspace_id}),
            allow_redirects=False,
            timeout=30,
        )
        log(f"  workspace/select -> {ws_resp.status_code}")

        next_url = str(ws_resp.headers.get("Location") or "").strip()
        next_data = {}
        if not next_url:
            try:
                next_data = ws_resp.json() or {}
            except Exception:
                next_data = {}
            next_url = str(next_data.get("continue_url") or "").strip()
        next_url = _normalize_url(next_url, consent_url)
        direct_code = _extract_code_from_url(next_url)
        if direct_code:
            result_json = submit_callback_url(
                callback_url=next_url,
                expected_state=oauth_start.state,
                code_verifier=oauth_start.code_verifier,
                proxy_url=proxy,
            )
            return json.loads(result_json)

        orgs = list((((next_data.get("data") or {}).get("orgs")) or []))
        if orgs and orgs[0].get("id"):
            org_id = str(orgs[0].get("id") or "").strip()
            org_body = {"org_id": org_id}
            projects = list(orgs[0].get("projects") or [])
            if projects and projects[0].get("id"):
                org_body["project_id"] = str(projects[0].get("id") or "").strip()
            log(f"  选择 organization: {org_id}")
            org_resp = s.post(
                "https://auth.openai.com/api/accounts/organization/select",
                headers={
                    "accept": "application/json",
                    "referer": consent_url,
                    "origin": OPENAI_AUTH,
                    "content-type": "application/json",
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                },
                data=json.dumps(org_body),
                allow_redirects=False,
                timeout=30,
            )
            log(f"  organization/select -> {org_resp.status_code}")
            next_url = str(org_resp.headers.get("Location") or "").strip() or next_url
            if not next_url:
                try:
                    org_data = org_resp.json() or {}
                    next_url = str(org_data.get("continue_url") or "").strip()
                    if not next_url:
                        org_state = _extract_flow_state(org_data, str(org_resp.url))
                        next_url = org_state.get("continue_url") or org_state.get("current_url") or ""
                except Exception:
                    next_url = ""
            next_url = _normalize_url(next_url, consent_url)

        if not next_url and next_data:
            state = _extract_flow_state(next_data, str(ws_resp.url))
            next_url = state.get("continue_url") or state.get("current_url") or ""
            next_url = _normalize_url(next_url, consent_url)

        if not next_url:
            next_url = "https://auth.openai.com/api/oauth/oauth2/auth?" + oauth_start.auth_url.split("?", 1)[1]

        callback_url = _follow_redirects_for_code(s, next_url, log)
        if not callback_url:
            log("  ⚠️ 未能跟到 OAuth callback")
            return None
        result_json = submit_callback_url(
            callback_url=callback_url,
            expected_state=oauth_start.state,
            code_verifier=oauth_start.code_verifier,
            proxy_url=proxy,
        )
        return json.loads(result_json)
    except Exception as e:
        log(f"  OAuth 会话补全异常: {e}")
        return None


def _submit_callback_result(callback_url: str, oauth_start, proxy: str | None) -> dict:
    from .oauth import submit_callback_url

    result_json = submit_callback_url(
        callback_url=callback_url,
        expected_state=oauth_start.state,
        code_verifier=oauth_start.code_verifier,
        redirect_uri=oauth_start.redirect_uri,
        client_id=oauth_start.client_id,
        proxy_url=proxy,
    )
    return json.loads(result_json)


def _wait_for_oauth_callback_result(
    page,
    oauth_start,
    proxy: str | None,
    log,
    *,
    timeout_sec: int = 90,
) -> dict | None:
    """Wait for the browser to land on the localhost OAuth callback and exchange it."""
    deadline = time.time() + max(int(timeout_sec or 0), 1)
    seen_urls: set[str] = set()

    while time.time() < deadline:
        candidates: list[str] = []
        try:
            candidates.append(str(page.url or ""))
        except Exception:
            pass
        try:
            location_href = str(page.evaluate("() => location.href") or "")
            if location_href:
                candidates.append(location_href)
        except Exception:
            pass

        for url in candidates:
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            if "localhost" in url or "code=" in url:
                log(f"  OAuth callback wait 检测到 URL: {url[:160]}")
            if not _extract_code_from_url(url):
                continue
            try:
                result = _submit_callback_result(url, oauth_start, proxy)
                log("  OAuth callback 已换取 token")
                return result
            except Exception as exc:
                log(f"  OAuth callback token exchange 失败: {exc}")
                return {"error": f"OAuth callback token exchange 失败: {exc}"}
        time.sleep(0.8)
    return None


def _extract_callback_url_from_exception(exc: Exception) -> str:
    text = str(exc or "")
    if not text:
        return ""
    match = re.search(r"(https?://localhost[^\s\"')]+)", text, flags=re.I)
    if not match:
        return ""
    callback_url = str(match.group(1) or "").strip().rstrip(".,")
    return callback_url if _extract_code_from_url(callback_url) else ""


def _derive_oauth_state_from_page(page) -> dict:
    state = _derive_registration_state_from_page(page)
    if state.get("page_type"):
        return state
    current_url = str(page.url or "")
    if _find_first_selector(page, EMAIL_INPUT_SELECTORS):
        return _build_manual_flow_state("login_email", current_url)
    return _extract_flow_state(None, current_url)


def _submit_login_email_via_page(page, email: str, log) -> dict:
    start_url = str(page.url or "")
    last_url = start_url
    last_text = ""

    for submit_attempt in range(1, 4):
        input_selector = _wait_for_any_selector(page, EMAIL_INPUT_SELECTORS, timeout=15)
        if not input_selector:
            retry_state = _auth_timeout_retry_page_state(page, path_patterns=[r"/log-in(?:[/?#]|$)", r"/email-verification(?:[/?#]|$)"])
            if retry_state.get("retryPage"):
                recovery = _recover_auth_timeout_retry_page(
                    page,
                    log,
                    path_patterns=[r"/log-in(?:[/?#]|$)", r"/email-verification(?:[/?#]|$)"],
                )
                if recovery.get("recovered"):
                    continue
            raise RuntimeError("OAuth 邮箱页未找到输入框")
        if not _fill_input_like_user(page, input_selector, email):
            raise RuntimeError("OAuth 邮箱页填写失败")
        log(f"OAuth 邮箱页输入框: {input_selector}")
        _browser_pause(page)

        submit_selector = _click_first_no_wait(page, EMAIL_SUBMIT_SELECTORS, timeout=8)
        if submit_selector:
            log(f"OAuth 邮箱页已点击继续按钮: {submit_selector}")
        elif _submit_form_with_fallback(page, input_selector):
            log("OAuth 邮箱页未找到可点击 Continue，已使用表单 fallback 提交")
        else:
            raise RuntimeError("OAuth 邮箱页未找到 Continue 按钮")

        deadline = time.time() + 20
        while time.time() < deadline:
            current_url = str(page.url or "")
            last_url = current_url or last_url
            retry_state = _auth_timeout_retry_page_state(page, path_patterns=[r"/log-in(?:[/?#]|$)", r"/email-verification(?:[/?#]|$)"])
            if retry_state.get("retryPage"):
                last_text = str(retry_state.get("text") or "")
                recovery = _recover_auth_timeout_retry_page(
                    page,
                    log,
                    path_patterns=[r"/log-in(?:[/?#]|$)", r"/email-verification(?:[/?#]|$)"],
                )
                if recovery.get("recovered"):
                    time.sleep(0.8)
                    state = _derive_oauth_state_from_page(page)
                    page_type = str(state.get("page_type") or "")
                    if page_type and page_type != "login_email":
                        return {"ok": True, "status": 200, "url": str(page.url or ""), "data": None, "text": ""}
                    break
                return {"ok": False, "status": 0, "url": str(recovery.get("url") or current_url), "data": None, "text": str(recovery.get("text") or "OpenAI auth retry page recovery failed")}

            if _click_passwordless_login_if_available(page, log, context="OAuth 邮箱页提交后"):
                time.sleep(0.5)
                continue
            state = _derive_oauth_state_from_page(page)
            page_type = str(state.get("page_type") or "")
            if page_type in {
                "login_password",
                "create_account_password",
                "email_otp_verification",
                "about_you",
                "consent",
                "workspace_selection",
                "organization_selection",
                "add_phone",
                "external_url",
                "oauth_callback",
                "chatgpt_home",
            }:
                return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
            if current_url != start_url and page_type != "login_email":
                return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
            error_text = _extract_auth_error_text(page)
            if error_text:
                return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
            time.sleep(0.5)
        log(f"OAuth 邮箱页提交后未跳转，准备重试提交 ({submit_attempt}/3)")

    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": last_text or "OAuth 邮箱页提交后未跳转"}


def _do_codex_oauth(
    page,
    cookies_dict: dict,
    email: str,
    password: str,
    otp_callback,
    phone_callback,
    proxy: str | None,
    log,
    *,
    allow_add_phone_retry: bool = True,
    oauth_start=None,
) -> dict | None:
    """在真实浏览器会话内完成 Codex OAuth，返回完整 token 包。

    如果传入 ``oauth_start``，则使用预生成的 OAuth 参数（重用 state/code_verifier），
    这样外层可以用同一 code_verifier 完成 token 交换（fallback 场景）。
    """
    from .oauth import generate_oauth_url
    from .constants import CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE

    if oauth_start is None:
        oauth_start = generate_oauth_url(
            redirect_uri=CODEX_REDIRECT_URI,
            scope=CODEX_SCOPE,
            client_id=CODEX_CLIENT_ID,
        )
    try:
        user_agent = str(page.evaluate("() => navigator.userAgent") or "").strip() or _random_chrome_ua()
    except Exception:
        user_agent = _random_chrome_ua()
    device_id = str(cookies_dict.get("oai-did") or uuid.uuid4())
    log(f"  Codex OAuth 授权链接: {oauth_start.auth_url}")
    log(f"  OAuth state={oauth_start.state[:20]}...")

    try:
        try:
            _goto_with_retry(page, oauth_start.auth_url, wait_until="domcontentloaded", timeout=30000, log=log)
        except Exception as exc:
            callback_url = _extract_callback_url_from_exception(exc)
            if callback_url:
                log(f"  OAuth bootstrap 直接捕获 callback: {callback_url[:100]}...")
                return _submit_callback_result(callback_url, oauth_start, proxy)
            raise

        current_url = str(page.url or "")
        log(f"  OAuth bootstrap -> {current_url[:100]}...")

        for step in range(20):
            state = _derive_oauth_state_from_page(page)
            current_url = str(page.url or "")
            next_url = str(state.get("continue_url") or "").strip()
            log(
                f"  OAuth state step[{step+1}/20]: "
                f"page={state.get('page_type') or '-'} next={next_url[:60]}"
                f" url={current_url[:120]}"
            )

            callback_url = ""
            if _extract_code_from_url(current_url):
                callback_url = current_url
            elif _extract_code_from_url(next_url):
                callback_url = next_url
            if callback_url:
                return _submit_callback_result(callback_url, oauth_start, proxy)

            page_oauth_url = _get_page_oauth_url(page)
            if (
                page_oauth_url
                and page_oauth_url != current_url
                and _oauth_url_matches_state(page_oauth_url, oauth_start.state)
            ):
                log("  OAuth 页面检测到更新的授权链接，跟随页面授权链接...")
                _goto_with_retry(page, page_oauth_url, wait_until="domcontentloaded", timeout=30000, log=log)
                continue

            if state["page_type"] == "login_email":
                log("  OAuth 页面需要邮箱登录，提交邮箱...")
                email_resp = _submit_login_email_via_page(page, email, log)
                log(f"  OAuth 邮箱页提交状态: {email_resp.get('status', 0)}")
                if not email_resp.get("ok"):
                    raise RuntimeError(f"OAuth 邮箱页提交失败: {(email_resp.get('text') or '')[:300]}")
                continue

            if state["page_type"] in {"login_password", "create_account_password"}:
                log("  OAuth 页面需要密码登录，提交密码...")
                # OAuth 流程中直接填密码登录，不尝试恢复到注册态
                password_resp = _submit_oauth_password_direct(page, password, log)
                log(f"  OAuth 密码页提交状态: {password_resp.get('status', 0)}")
                if not password_resp.get("ok"):
                    raise RuntimeError(f"OAuth 密码页提交失败: {(password_resp.get('text') or '')[:300]}")
                continue

            if state["page_type"] == "email_otp_verification":
                if not otp_callback:
                    log("  ⚠️ OAuth 需要邮箱 OTP 但没有 otp_callback")
                    return None
                log("  OAuth 等待邮箱验证码...")
                code = otp_callback()
                if not code:
                    log("  ⚠️ OAuth OTP 获取失败")
                    return None
                otp_resp = _submit_otp_via_page(page, code, log)
                log(f"  OAuth 验证码页提交状态: {otp_resp.get('status', 0)}")
                if not otp_resp.get("ok"):
                    raise RuntimeError(f"OAuth 验证码校验失败: {(otp_resp.get('text') or '')[:300]}")
                continue

            if state["page_type"] == "about_you":
                log("  OAuth 页面出现 about_you，继续页面填写...")
                about_resp = _submit_about_you_via_page(page, log)
                log(f"  OAuth about_you 提交状态: {about_resp.get('status', 0)}")
                if not about_resp.get("ok"):
                    raise RuntimeError(f"OAuth about_you 提交失败: {(about_resp.get('text') or '')[:300]}")
                continue

            if state["page_type"] in {"consent", "workspace_selection", "organization_selection", "external_url"}:
                browser_result = _complete_oauth_in_browser(page, oauth_start, proxy, log)
                if browser_result:
                    return browser_result
                cookies_dict = _get_cookies(page)
                session_result = _complete_oauth_with_session(cookies_dict, oauth_start, proxy, log)
                if session_result:
                    return session_result
                log("  ⚠️ 页面已到 consent/workspace，但会话补全失败")
                return None

            if state["page_type"] == "add_phone":
                if phone_callback:
                    log("  OAuth 检测到 add_phone，优先执行短信验证...")
                    try:
                        _handle_add_phone_challenge(
                            page, phone_callback,
                            device_id=device_id, user_agent=user_agent,
                            log=log, resume_url=oauth_start.auth_url,
                        )
                        callback_result = _wait_for_oauth_callback_result(
                            page,
                            oauth_start,
                            proxy,
                            log,
                            timeout_sec=45,
                        )
                        if callback_result:
                            return callback_result
                        continue
                    except Exception as exc:
                        log(f"  短信验证失败，停止 OAuth 流程: {exc}")
                        return None

                if not allow_add_phone_retry:
                    log("  OAuth 检测到 add_phone，等待手动完成手机号验证并跳转 callback...")
                    callback_result = _wait_for_oauth_callback_result(
                        page,
                        oauth_start,
                        proxy,
                        log,
                        timeout_sec=180,
                    )
                    if callback_result:
                        return callback_result
                    return {"error": "OpenAI OAuth 要求手机号验证，等待后未捕获 callback URL"}

                # 先尝试跳过 add_phone，直接重新访问 OAuth 授权 URL
                # 用户已登录，重新访问 auth URL 应该能直接跳到 callback
                log("  检测到 add_phone，尝试跳过...")
                try:
                    _goto_with_retry(page, oauth_start.auth_url, wait_until="domcontentloaded", timeout=15000, log=log)
                    time.sleep(2)
                    current_url = str(page.url or "")

                    # 检查是否直接拿到了 callback
                    callback_url = ""
                    if "code=" in current_url:
                        callback_url = current_url
                    else:
                        # 可能需要跟随重定向
                        for _ in range(5):
                            time.sleep(1)
                            current_url = str(page.url or "")
                            if "code=" in current_url:
                                callback_url = current_url
                                break

                    if callback_url:
                        log("  ✓ 成功跳过 add_phone，获取到 OAuth callback")
                        return _submit_callback_result(callback_url, oauth_start, proxy)

                    # 检查页面状态
                    skip_state = _derive_registration_state_from_page(page)
                    if skip_state.get("page_type") in {"consent", "workspace_selection", "organization_selection"}:
                        log("  ✓ 跳过 add_phone 到达 consent 页面")
                        # 尝试在浏览器里完成 consent 流程
                        browser_result = _complete_oauth_in_browser(page, oauth_start, proxy, log)
                        if browser_result:
                            return browser_result
                        # 回退到 curl session 方式
                        cookies_dict = _get_cookies(page)
                        session_result = _complete_oauth_with_session(cookies_dict, oauth_start, proxy, log)
                        if session_result:
                            return session_result

                    if skip_state.get("page_type") == "add_phone":
                        log("  跳过失败，仍在 add_phone 页面")
                    else:
                        log(f"  跳过后页面状态: {skip_state.get('page_type') or '-'}")
                        # 继续状态机循环
                        continue

                except Exception as exc:
                    callback_url = _extract_callback_url_from_exception(exc)
                    if callback_url:
                        return _submit_callback_result(callback_url, oauth_start, proxy)
                    log(f"  跳过 add_phone 异常: {exc}")

                log("  ⚠️ add_phone 无法跳过且无可用接码服务")
                return None

            # chatgpt_home: 页面可能正在 JS 重定向（如跳转到 add-phone）
            # 等待更长时间让重定向完成
            if state["page_type"] == "chatgpt_home":
                # 检查是否是错误页面
                if "error" in current_url:
                    error_msg = current_url.split("error=")[-1].split("&")[0] if "error=" in current_url else "unknown"
                    log(f"  OAuth 错误页面: {error_msg} url={current_url[:150]}")
                    raise RuntimeError(f"OpenAI OAuth 错误: {error_msg}")
                time.sleep(2)
                new_url = str(page.url or "")
                if new_url != current_url:
                    continue
                # 检查 cookie 里是否有 session
                cookies_dict = _get_cookies(page)
                for ck, cv in cookies_dict.items():
                    if "session" in ck.lower() and cv:
                        log(f"  chatgpt_home 检测到 session cookie: {ck}")
                        session_result = _complete_oauth_with_session(cookies_dict, oauth_start, proxy, log)
                        if session_result:
                            return session_result
                        break
                continue

            target_url = _normalize_url(state.get("continue_url") or "", OPENAI_AUTH)
            if target_url and target_url != current_url:
                try:
                    _goto_with_retry(page, target_url, wait_until="domcontentloaded", timeout=30000, log=log)
                except Exception as exc:
                    callback_url = _extract_callback_url_from_exception(exc)
                    if callback_url:
                        return _submit_callback_result(callback_url, oauth_start, proxy)
                    log(f"  OAuth navigation failed: {exc}")
                    break
                continue

            error_text = _extract_auth_error_text(page)
            if error_text:
                raise RuntimeError(f"OAuth 页面错误: {error_text[:300]}")
            time.sleep(0.5)
    except Exception as e:
        log(f"  OAuth 异常: {e}")
        return None

    cookies_dict = _get_cookies(page)
    result = _complete_oauth_with_session(cookies_dict, oauth_start, proxy, log)
    if result:
        return result

    session_token = cookies_dict.get("__Secure-next-auth.session-token", "")
    if not session_token:
        log("  ⚠️ 无 session_token，OAuth 失败")
        return None
    log("  ⚠️ 完整 OAuth 失败，回退 session access_token")
    return None


def _wait_for_access_token(page, timeout: int = 60) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = page.evaluate("""
            async () => {
                const r = await fetch('/api/auth/session');
                const j = await r.json();
                return j.accessToken || '';
            }
            """)
            if r:
                return r
        except Exception:
            pass
        time.sleep(2)
    return ""


def _is_registration_complete(state: dict) -> bool:
    page_type = str(state.get("page_type") or "")
    url = str(state.get("current_url") or state.get("continue_url") or "").lower()
    return page_type in {"callback", "oauth_callback", "chatgpt_home"} or (
        "chatgpt.com" in url and "redirect_uri" not in url and "about-you" not in url
    )


def _handle_post_signup_onboarding(page, log) -> None:
    current_url = str(page.url or "")
    if "chatgpt.com" not in current_url:
        return

    try:
        page.context.grant_permissions(["persistent-storage"], origin=CHATGPT_APP)
        log("已授予 ChatGPT persistent-storage 权限")
    except Exception:
        pass

    try:
        # Firefox/Camoufox may show a native persistent-storage prompt. It is not
        # part of the DOM, so try granting permissions and pressing Enter before
        # using DOM locators. If no prompt exists, Enter is harmless here.
        try:
            page.keyboard.press("Enter")
            time.sleep(0.2)
        except Exception:
            pass
        allow_selector = _click_first(
            page,
            [
                'button:has-text("Allow")',
                'button:has-text("allow")',
                'button:has-text("Block")',
                'button:has-text("block")',
                'button:has-text("许可")',
                'button:has-text("許可")',
                'button:has-text("拒否")',
            ],
            timeout=1,
        )
        if allow_selector:
            log(f"已处理浏览器弹窗: {allow_selector}")
    except Exception:
        pass

    try:
        all_set = False
        try:
            all_set = bool(
                page.locator("text=You're all set").first.is_visible(timeout=1500)
                or page.locator("text=You’re all set").first.is_visible(timeout=1500)
            )
        except Exception:
            try:
                body_text = str(page.locator("body").inner_text(timeout=1500) or "")
                all_set = "You're all set" in body_text or "You’re all set" in body_text
            except Exception:
                all_set = False
        if all_set:
            continue_selector = _click_first(
                page,
                [
                    'button:has-text("Continue")',
                    'button:has-text("continue")',
                    'button[type="submit"]',
                ],
                timeout=8,
            )
            if continue_selector:
                log(f"已处理 You're all set onboarding: {continue_selector}")
                _browser_pause(page)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
    except Exception as exc:
        log(f"处理 You're all set onboarding 失败，继续后续流程: {exc}")

    try:
        if page.locator("text=What brings you to ChatGPT?").first.count() > 0:
            skip_selector = _click_first(
                page,
                [
                    'button:has-text("Skip")',
                    'button:has-text("skip")',
                    'button:has-text("Next")',
                    'button:has-text("next")',
                    'button:has-text("跳过")',
                    'button:has-text("下一步")',
                ],
                timeout=5,
            )
            if skip_selector:
                log(f"已处理 onboarding 页面: {skip_selector}")
                _browser_pause(page)
    except Exception:
        pass

def _is_password_registration(state: dict) -> bool:
    return str(state.get("page_type") or "") in {"create_account_password", "password"}


def _is_email_otp(state: dict) -> bool:
    target = f"{state.get('continue_url') or ''} {state.get('current_url') or ''}".lower()
    return str(state.get("page_type") or "") == "email_otp_verification" or "email-verification" in target or "email-otp" in target


def _is_about_you(state: dict) -> bool:
    target = f"{state.get('continue_url') or ''} {state.get('current_url') or ''}".lower()
    return str(state.get("page_type") or "") == "about_you" or "about-you" in target


def _is_add_phone(state: dict) -> bool:
    target = f"{state.get('continue_url') or ''} {state.get('current_url') or ''}".lower()
    return str(state.get("page_type") or "") == "add_phone" or "add-phone" in target


def _mask_phone_number(phone_number: str) -> str:
    text = str(phone_number or "").strip()
    if len(text) <= 4:
        return text
    if len(text) <= 8:
        return f"{text[:2]}****{text[-2:]}"
    return f"{text[:4]}****{text[-2:]}"


def _is_invalid_phone_otp_response(result: dict) -> bool:
    status = int((result or {}).get("status") or 0)
    if status != 400:
        return False
    data = (result or {}).get("data")
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").lower()
            code = str(error.get("code") or "").lower()
            return code == "invalid_input" and "invalid otp code" in message
    text = str((result or {}).get("text") or "").lower()
    return "invalid otp code" in text


def _handle_add_phone_challenge(
    page,
    phone_callback,
    *,
    device_id: str,
    user_agent: str,
    log,
    resume_url: str = "",
    max_phone_attempts: int = 3,
) -> dict:
    """在 add-phone 页面通过 UI 交互完成手机号验证。

    流程: 选择国家 -> 输入本地号码 -> 点击发送 -> 填写 OTP -> 点击验证。
    如果验证码超时未收到，自动换号重试（最多 max_phone_attempts 次）。
    """
    if not phone_callback:
        log("⚠️ 未配置 phone_callback，手机号验证流程中断")
        raise RuntimeError(
            "manual_phone_required: 未配置接码服务，ChatGPT 注册无法完成手机验证。"
            "请在设置中配置接码 provider 后重试。"
        )

    last_error = None
    for phone_attempt in range(max_phone_attempts):
        if phone_attempt > 0:
            log(f"换号重试第 {phone_attempt + 1}/{max_phone_attempts} 次...")
            # 回到 add-phone 页面
            try:
                _goto_with_retry(page, f"{OPENAI_AUTH}/add-phone", wait_until="domcontentloaded", timeout=15000, log=log)
                time.sleep(1)
            except Exception:
                pass

        try:
            result = _do_add_phone_attempt(
                page, phone_callback,
                device_id=device_id, user_agent=user_agent,
                log=log, resume_url=resume_url,
            )
            return result
        except RuntimeError as exc:
            last_error = exc
            error_msg = str(exc)
            # 验证码超时或号码已被使用时换号重试，其他错误直接抛出
            should_retry = (
                "未获取到短信验证码" in error_msg
                or "短信验证码校验失败" in error_msg
                or "验证码均无效" in error_msg
                or "phone_number_in_use" in error_msg
                or "already" in error_msg.lower()
                or "in use" in error_msg.lower()
                or "risk" in error_msg.lower()
                or "blocked" in error_msg.lower()
                or "not available" in error_msg.lower()
                or "unsupported" in error_msg.lower()
                or "too many" in error_msg.lower()
                or "limit" in error_msg.lower()
                or "invalid phone" in error_msg.lower()
                or "风控" in error_msg
                or "已使用" in error_msg
                or "不可用" in error_msg
            )
            if not should_retry:
                raise
            timeout_or_code_failure = (
                "未获取到短信验证码" in error_msg
                or "短信验证码校验失败" in error_msg
                or "验证码均无效" in error_msg
            )
            if timeout_or_code_failure and hasattr(phone_callback, "mark_send_failed"):
                phone_callback.mark_send_failed(error_msg)
            log(f"⚠️ 当前号码不可用，准备取消并换号: {error_msg[:120]}")
            # 取消当前号码
            if hasattr(phone_callback, "cleanup"):
                phone_callback.cleanup()
            # 重置 phone_callback 状态为 need_number
            if hasattr(phone_callback, "phase"):
                phone_callback.phase = "need_number"
                phone_callback.activation = None
                phone_callback.completed = False

    raise last_error or RuntimeError("短信验证失败: 多次换号均未收到验证码")


def _do_add_phone_attempt(
    page,
    phone_callback,
    *,
    device_id: str,
    user_agent: str,
    log,
    resume_url: str = "",
) -> dict:
    """单次手机号验证尝试（内部函数）。"""

    # 保留 HTTP resend 回调供 SMS provider 内部使用
    referer = _normalize_url(str(page.url or ""), OPENAI_AUTH) or f"{OPENAI_AUTH}/add-phone"
    headers = _build_browser_headers(
        user_agent=user_agent,
        accept="application/json",
        referer=referer,
        origin=OPENAI_AUTH,
        content_type="application/json",
        extra_headers={
            "sec-fetch-site": "same-origin",
            "oai-device-id": device_id,
            **_generate_datadog_trace_headers(),
        },
    )

    def _request_openai_resend():
        # 浏览器模式下只通过页面 UI 点击 Resend 按钮
        resend_clicked = _click_first_no_wait(page, [
            'button:has-text("Resend")',
            'button:has-text("resend")',
            'button:has-text("Resend code")',
            'button[data-testid="resend-link"]',
            'button:has-text("重新发送")',
            'a:has-text("Resend")',
            'a:has-text("resend")',
            'a:has-text("Resend code")',
            'button:has-text("再送信")',
            'button:has-text("コードを再送")',
            'a:has-text("再送信")',
            'a:has-text("コードを再送")',
        ], timeout=3)
        if resend_clicked:
            log(f"  phone-otp/resend -> 已点击页面 Resend 按钮: {resend_clicked}")
        else:
            log("  phone-otp/resend -> 页面未找到 Resend 按钮，跳过（浏览器模式不走 HTTP）")

    if hasattr(phone_callback, "set_resend_callback"):
        phone_callback.set_resend_callback(_request_openai_resend)

    # ---- 第1步: 获取手机号 ----
    log("注册流程已进入 add_phone，开始准备租号并接收短信验证码...")
    phone_number = str(phone_callback() or "").strip()
    if not phone_number:
        raise RuntimeError("未获取到手机号")
    log(f"检测到 add_phone，提交手机号(UI): {_mask_phone_number(phone_number)}")

    # 解析国家拨号码和本地号码
    dial_code, local_number, country_name = _parse_phone_country_and_local(phone_number)
    log(f"  解析号码: 国家={country_name or '未知'} 拨号码=+{dial_code} 本地号={local_number[:4]}...")

    # 确保在 add-phone 页面
    current_url = str(page.url or "")
    if "add-phone" not in current_url:
        _goto_with_retry(page, f"{OPENAI_AUTH}/add-phone", wait_until="domcontentloaded", timeout=30000, log=log)
    time.sleep(1)

    # The selected SMSBower country must be reflected in OpenAI's React Aria
    # country control before entering the national number.
    country_selected = _select_phone_country_ui(page, dial_code, country_name, log)
    if dial_code and not country_selected:
        raise RuntimeError(f"手机号国家选择失败: {country_name or '+' + dial_code}")

    submit_result = _submit_add_phone_dom(
        page,
        phone_number=phone_number,
        dial_code=dial_code,
        local_number=local_number if country_selected else phone_number,
        country_name=country_name,
        log=log,
    )
    if not submit_result.get("ok"):
        log(f"  add-phone DOM 提交失败，回退旧 UI 路径: {submit_result.get('reason') or submit_result}")
        country_selected = _select_phone_country_ui(page, dial_code, country_name, log)
        _browser_pause(page)
        phone_input_sel = _wait_for_any_selector(page, PHONE_INPUT_SELECTORS, timeout=10)
        if not phone_input_sel:
            raise RuntimeError("未找到手机号输入框")
        fill_value = local_number if country_selected else phone_number
        if not _fill_input_like_user(page, phone_input_sel, fill_value):
            raise RuntimeError(f"手机号输入框填写失败: {phone_input_sel}")
        send_sel = _click_first_no_wait(page, PHONE_SEND_SELECTORS, timeout=8)
        if send_sel:
            log(f"  已点击发送按钮: {send_sel}")
        elif _submit_form_with_fallback(page, phone_input_sel):
            log("  未找到发送按钮，已使用表单 fallback 提交")
        else:
            raise RuntimeError("未找到发送验证码按钮")

    phone_status = _wait_for_phone_verification_ready(page, timeout=30)
    if phone_status.get("addPhoneError"):
        if hasattr(phone_callback, "mark_send_failed"):
            phone_callback.mark_send_failed(str(phone_status.get("addPhoneError") or ""))
        raise RuntimeError(f"手机号提交失败: {str(phone_status.get('addPhoneError') or '')[:200]}")
    if not phone_status.get("phoneVerificationReady"):
        error_text = _extract_auth_error_text(page)
        if error_text:
            if hasattr(phone_callback, "mark_send_failed"):
                phone_callback.mark_send_failed(error_text)
            raise RuntimeError(f"手机号提交失败: {error_text[:200]}")
        raise RuntimeError(f"手机号提交后未进入验证码页: {str(phone_status.get('url') or page.url)}")

    # 检查发送是否成功（页面应出现 OTP 输入框或 URL 变化）
    error_text = _extract_auth_error_text(page)
    if error_text:
        if hasattr(phone_callback, "mark_send_failed"):
            phone_callback.mark_send_failed(error_text)
        raise RuntimeError(f"手机号提交失败: {error_text[:200]}")

    if hasattr(phone_callback, "mark_send_succeeded"):
        phone_callback.mark_send_succeeded()
    log("手机号提交成功(UI)，开始等待短信验证码...")

    # ---- 第5步: 等待 SMS 验证码并在页面 OTP 输入框中填写 ----
    for code_attempt in range(3):
        sms_code = str(phone_callback() or "").strip()
        if not sms_code:
            raise RuntimeError("未获取到短信验证码")

        phone_status = _wait_for_phone_verification_ready(page, timeout=12)
        if not phone_status.get("phoneVerificationReady"):
            raise RuntimeError(f"未找到短信验证码输入框: {str(phone_status.get('url') or page.url)}")

        otp_resp = _submit_phone_otp_dom(page, sms_code, log)
        if not otp_resp.get("ok") and "missing_phone_verification" in str(otp_resp.get("text") or ""):
            otp_resp = _submit_otp_via_page(page, sms_code, log)
        otp_status = int(otp_resp.get("status") or 0)
        log(f"  phone-otp 页面提交状态: {otp_status}")

        if otp_resp.get("ok") or otp_status in (200, 201, 204):
            if hasattr(phone_callback, "report_success"):
                phone_callback.report_success()
            # 等待页面跳转
            time.sleep(1.5)
            state = _extract_flow_state(
                otp_resp.get("data"),
                otp_resp.get("url", page.url),
            )
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            next_url = _normalize_url(resume_url, OPENAI_AUTH) if resume_url else ""
            if next_url:
                _goto_with_retry(page, next_url, wait_until="domcontentloaded", timeout=30000, log=log)
                return _extract_flow_state(None, page.url)
            return state

        # 检查是否是无效验证码
        page_error = _extract_auth_error_text(page)
        if page_error and any(kw in page_error.lower() for kw in ("invalid", "incorrect", "wrong", "expired")):
            log(f"短信验证码被判定无效: {page_error[:100]}，继续等待下一条...")
            if hasattr(phone_callback, "mark_code_failed"):
                phone_callback.mark_code_failed(page_error or "invalid otp code")
            continue

        if hasattr(phone_callback, "mark_code_failed"):
            phone_callback.mark_code_failed(page_error or f"status {otp_status}")
        raise RuntimeError(f"短信验证码校验失败: {page_error[:200] if page_error else f'status {otp_status}'}")

    raise RuntimeError("短信验证码校验失败: 多次验证码均无效或未通过")


def _requires_registration_navigation(state: dict) -> bool:
    if str(state.get("method") or "GET").upper() != "GET":
        return False
    if str(state.get("page_type") or "") == "external_url" and state.get("continue_url"):
        return True
    continue_url = str(state.get("continue_url") or "")
    current_url = str(state.get("current_url") or "")
    return bool(continue_url and continue_url != current_url)


def _browser_add_cookies(page, cookies: list[dict]) -> None:
    try:
        page.context.add_cookies(cookies)
    except Exception:
        pass


def _seed_browser_device_id(page, device_id: str) -> None:
    _browser_add_cookies(
        page,
        [
            {"name": "oai-did", "value": device_id, "domain": "chatgpt.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": ".chatgpt.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": "openai.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": "auth.openai.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": ".auth.openai.com", "path": "/"},
        ],
    )


def _get_browser_csrf_token(page) -> str:
    result = _browser_fetch(
        page,
        f"{CHATGPT_APP}/api/auth/csrf",
        method="GET",
        headers={
            "accept": "application/json",
            "referer": f"{CHATGPT_APP}/",
            "sec-fetch-site": "same-origin",
        },
        redirect="follow",
    )
    if result.get("ok") and isinstance(result.get("data"), dict):
        return str((result.get("data") or {}).get("csrfToken") or "").strip()
    return ""


def _start_browser_signin(page, email: str, device_id: str, csrf_token: str) -> str:
    from urllib.parse import urlencode

    query = urlencode(
        {
            "prompt": "login",
            "ext-oai-did": device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "screen_hint": "login_or_signup",
            "login_hint": email,
        }
    )
    body = urlencode(
        {
            "callbackUrl": f"{CHATGPT_APP}/",
            "csrfToken": csrf_token,
            "json": "true",
        }
    )
    result = _browser_fetch(
        page,
        f"{CHATGPT_APP}/api/auth/signin/openai?{query}",
        method="POST",
        headers={
            "accept": "application/json",
            "referer": f"{CHATGPT_APP}/",
            "origin": CHATGPT_APP,
            "content-type": "application/x-www-form-urlencoded",
            "sec-fetch-site": "same-origin",
        },
        body=body,
        redirect="follow",
    )
    if result.get("ok") and isinstance(result.get("data"), dict):
        return str((result.get("data") or {}).get("url") or "").strip()
    return ""


def _browser_authorize(page, auth_url: str, log) -> str:
    if not auth_url:
        return ""
    try:
        _goto_with_retry(page, auth_url, wait_until="domcontentloaded", timeout=30000, log=log)
        final_url = page.url
        log(f"Authorize -> {final_url[:120]}")
        return final_url
    except Exception as exc:
        log(f"Authorize 失败: {exc}")
        if _is_browser_driver_disconnect(exc):
            raise RuntimeError(f"authorize driver disconnected: {exc}") from exc
        return ""


def _validate_browser_email_otp(page, code: str, device_id: str, user_agent: str, referer: str) -> dict:
    headers = _build_browser_headers(
        user_agent=user_agent,
        accept="application/json",
        referer=referer or f"{OPENAI_AUTH}/email-verification",
        origin=OPENAI_AUTH,
        content_type="application/json",
        extra_headers={
            "sec-fetch-site": "same-origin",
            "oai-device-id": device_id,
            **_generate_datadog_trace_headers(),
        },
    )
    sentinel = _build_browser_sentinel_token(page, device_id, "email_otp_validate", user_agent)
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    _browser_pause(page)
    return _browser_fetch(
        page,
        f"{OPENAI_AUTH}/api/accounts/email-otp/validate",
        method="POST",
        headers=headers,
        body=json.dumps({"code": code}),
        redirect="follow",
    )


def _submit_browser_about_you(page, device_id: str, user_agent: str, referer: str) -> dict:
    from .constants import generate_random_user_info

    headers = _build_browser_headers(
        user_agent=user_agent,
        accept="application/json",
        referer=referer or f"{OPENAI_AUTH}/about-you",
        origin=OPENAI_AUTH,
        content_type="application/json",
        extra_headers={
            "sec-fetch-site": "same-origin",
            "oai-device-id": device_id,
            **_generate_datadog_trace_headers(),
        },
    )
    sentinel = _build_browser_sentinel_token(page, device_id, "oauth_create_account", user_agent)
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    user_info = generate_random_user_info()
    _browser_pause(page)
    return _browser_fetch(
        page,
        f"{OPENAI_AUTH}/api/accounts/create_account",
        method="POST",
        headers=headers,
        body=json.dumps(user_info),
        redirect="follow",
    )


def _complete_oauth_in_browser(page, oauth_start, proxy, log) -> dict | None:
    """在浏览器里完成 OAuth consent 流程，多策略重试点击 Continue。

    参考 Chrome 扩展项目的 step9 实现:
    - consent 页面是一个 <form action="/sign-in-with-chatgpt/.../consent">
    - 首选 form.requestSubmit(button) 而非 button.click()
    - 多轮重试: requestSubmit → click → dispatchEvent → 刷新重试
    """
    from .oauth import submit_callback_url

    CONSENT_FORM_SEL = OAUTH_CONSENT_FORM_SELECTOR
    MAX_ROUNDS = 4
    CLICK_EFFECT_TIMEOUT = 30

    def _try_extract_callback(url: str) -> dict | None:
        if not url or "code=" not in url:
            return None
        try:
            return json.loads(submit_callback_url(
                callback_url=url,
                expected_state=oauth_start.state,
                code_verifier=oauth_start.code_verifier,
                redirect_uri=oauth_start.redirect_uri,
                client_id=oauth_start.client_id,
                proxy_url=proxy,
            ))
        except ValueError as ve:
            # state 缺失或不匹配时，如果 URL 确实是我们的 callback，跳过 state 验证直接换 token
            if "state" in str(ve) and "localhost" in url and "code=" in url:
                try:
                    # 手动提取 code，跳过 state 验证
                    from urllib.parse import urlparse, parse_qs
                    parsed = urlparse(url)
                    params = parse_qs(parsed.query)
                    code = (params.get("code") or [""])[0]
                    if code:
                        from .oauth import _post_form, _jwt_claims_no_verify, OAUTH_TOKEN_URL
                        import time as _time
                        token_resp = _post_form(
                            OAUTH_TOKEN_URL,
                            {
                                "grant_type": "authorization_code",
                                "client_id": oauth_start.client_id,
                                "code": code,
                                "redirect_uri": oauth_start.redirect_uri,
                                "code_verifier": oauth_start.code_verifier,
                            },
                            proxy_url=proxy,
                        )
                        normalized = normalize_oauth_token_response(token_resp)
                        access_token = normalized["access_token"]
                        refresh_token = normalized["refresh_token"]
                        id_token = normalized["id_token"]
                        if access_token:
                            claims = _jwt_claims_no_verify(id_token)
                            auth_claims = claims.get("https://api.openai.com/auth") or {}
                            now = int(_time.time())
                            expires_in = normalized["expires_in"]
                            return {
                                "id_token": id_token,
                                "access_token": access_token,
                                "refresh_token": refresh_token,
                                "account_id": str(auth_claims.get("chatgpt_account_id") or ""),
                                "email": str(claims.get("email") or ""),
                                "expired": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now + max(expires_in, 0))),
                                "last_refresh": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now)),
                            }
                except Exception:
                    pass
            return None
        except Exception:
            return None

    def _check_current_url() -> dict | None:
        url = str(page.url or "")
        result = _try_extract_callback(url)
        if result:
            return result
        cb = _extract_callback_url_from_exception(Exception(url))
        return _try_extract_callback(cb) if cb else None

    def _wait_for_callback(timeout_sec: int) -> dict | None:
        deadline = time.time() + timeout_sec
        checked_urls = set()
        while time.time() < deadline:
            try:
                url = str(page.url or "")
            except Exception:
                url = ""
            if url and url not in checked_urls:
                checked_urls.add(url)
                if "code=" in url or "localhost" in url:
                    log(f"  [callback_wait] 检测到 URL 变化: {url[:150]}")
            result = _check_current_url()
            if result:
                return result
            # 也检查是否有导航到 localhost 的请求（即使页面加载失败）
            if "localhost" in url and "code=" in url:
                result = _try_extract_callback(url)
                if result:
                    return result
            time.sleep(0.8)
        # 最后再检查一次
        try:
            final_url = str(page.url or "")
            if "code=" in final_url:
                log(f"  [callback_wait] 超时后最终 URL: {final_url[:150]}")
                result = _try_extract_callback(final_url)
                if result:
                    return result
        except Exception:
            pass
        return None

    def _find_consent_button():
        """按优先级查找 consent 页面的 Continue 按钮"""
        # 策略 1: 在 consent form 内找 submit 按钮
        _sel = CONSENT_FORM_SEL
        btn = page.evaluate("""(sel) => {
            const form = document.querySelector(sel);
            if (!form) return null;
            const buttons = form.querySelectorAll('button[type="submit"], input[type="submit"], [role="button"]');
            for (const el of buttons) {
                if (el.offsetParent === null) continue;
                const text = (el.textContent || '').trim().toLowerCase();
                const ddName = el.getAttribute('data-dd-action-name') || '';
                if (ddName === 'Continue' || /continue|继续|continuar|fortfahren|continuer|続ける/i.test(text)) return 'form-continue';
            }
            const first = Array.from(buttons).find(el => el.offsetParent !== null);
            if (first) return 'form-submit';
            return null;
        }""", _sel)
        if btn:
            return btn
        # 策略 2: 全局查找 Continue 按钮
        for sel in [
            'button[type="submit"][data-dd-action-name="Continue"]',
            'button:has-text("Continue")',
            'button:has-text("继续")',
            'button:has-text("Continuar")',
            'button:has-text("Fortfahren")',
            'button:has-text("Continuer")',
            'button:has-text("Allow")',
            'button:has-text("Authorize")',
            'button:has-text("続ける")',
            'button:has-text("続行")',
            'button:has-text("許可")',
            'button:has-text("認可")',
            'button[type="submit"]',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=500):
                    return sel
            except Exception:
                continue
        return None

    def _click_strategy_request_submit(log_round: int) -> bool:
        """策略 1: form.requestSubmit(button) — 最可靠的表单提交方式"""
        try:
            result = page.evaluate("""(sel) => {
                const form = document.querySelector(sel);
                if (!form) return 'no-form';
                const buttons = form.querySelectorAll('button[type="submit"], input[type="submit"]');
                let target = null;
                for (const el of buttons) {
                    if (el.offsetParent === null) continue;
                    const text = (el.textContent || '').trim().toLowerCase();
                    const ddName = el.getAttribute('data-dd-action-name') || '';
                    if (ddName === 'Continue' || /continue|继续|continuar|fortfahren|continuer|続ける|続行|許可|認可/i.test(text)) { target = el; break; }
                }
                if (!target) target = Array.from(buttons).find(el => el.offsetParent !== null);
                if (!target) return 'no-button';
                if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit(target);
                    return 'requestSubmit';
                }
                target.click();
                return 'click-fallback';
            }""", CONSENT_FORM_SEL)
            log(f"  consent 第{log_round}轮 requestSubmit: {result}")
            return result not in ("no-form", "no-button")
        except Exception as e:
            log(f"  consent requestSubmit 异常: {e}")
            return False

    def _click_strategy_playwright(log_round: int) -> bool:
        """策略 2: Playwright locator.click()"""
        for sel in [
            'button:has-text("Continue")',
            'button:has-text("继续")',
            'button:has-text("Continuar")',
            'button:has-text("Fortfahren")',
            'button:has-text("Continuer")',
            'button:has-text("続ける")',
            'button:has-text("続行")',
            'button:has-text("許可")',
            'button:has-text("認可")',
            'button[type="submit"]',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1500):
                    loc.click()
                    log(f"  consent 第{log_round}轮 playwright click: {sel}")
                    return True
            except Exception:
                continue
        return False

    def _click_strategy_js_dispatch(log_round: int) -> bool:
        """策略 3: JS dispatchEvent 模拟点击"""
        try:
            result = page.evaluate("""() => {
                const buttons = document.querySelectorAll('button, [role="button"]');
                for (const el of buttons) {
                    if (el.offsetParent === null) continue;
                    const text = (el.textContent || '').trim().toLowerCase();
                    const ddName = el.getAttribute('data-dd-action-name') || '';
                    if (ddName === 'Continue' || /continue|继续|continuar|fortfahren|continuer/i.test(text)) {
                        el.focus();
                        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                        return text || 'dispatched';
                    }
                }
                return null;
            }
            """)
            if result:
                log(f"  consent 第{log_round}轮 JS dispatch: {result}")
                return True
            return False
        except Exception:
            return False

    strategies = [
        _click_strategy_request_submit,
        _click_strategy_playwright,
        _click_strategy_js_dispatch,
        _click_strategy_request_submit,
    ]

    try:
        current_url = str(page.url or "")
        log(f"  浏览器 consent 处理: {current_url[:100]}")

        # 先检查当前 URL 是否已经有 code
        result = _check_current_url()
        if result:
            log("  ✓ 页面已在 callback URL")
            return result

        # 等待页面加载
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        time.sleep(1)

        # 检查 "Try again" 按钮
        try:
            try_again = page.query_selector('button:has-text("Try again"), button:has-text("再試行"), button:has-text("もう一度")')
            if try_again and try_again.is_visible():
                log("  consent 页面报错，点击 Try again...")
                try_again.click()
                time.sleep(3)
        except Exception:
            pass

        # 多轮策略重试
        for round_idx in range(MAX_ROUNDS):
            result = _check_current_url()
            if result:
                log("  ✓ 浏览器 OAuth consent 完成")
                return result

            strategy_fn = strategies[min(round_idx, len(strategies) - 1)]
            clicked = strategy_fn(round_idx + 1)

            if clicked:
                # consent 提交后会跳转到 localhost:1455/auth/callback
                # 由于没有本地服务监听，浏览器可能报连接错误，但 URL 已经更新
                try:
                    page.wait_for_url("**/auth/callback*", timeout=15000)
                except Exception:
                    pass  # 超时或导航错误都忽略，下面会检查 URL
                time.sleep(1)
                result = _wait_for_callback(CLICK_EFFECT_TIMEOUT)
                if result:
                    log("  ✓ 浏览器 OAuth consent 完成")
                    return result
                log(f"  consent 第{round_idx + 1}轮点击后页面未跳转")
            else:
                log(f"  consent 第{round_idx + 1}轮未找到按钮")

            # 最后一轮前刷新页面重试
            if round_idx < MAX_ROUNDS - 1:
                log(f"  consent 刷新页面准备第{round_idx + 2}轮...")
                try:
                    _reload_with_retry(page, wait_until="domcontentloaded", timeout=15000, log=log)
                except Exception:
                    pass
                time.sleep(2)

        log(f"  consent {MAX_ROUNDS}轮尝试后仍未完成，当前: {str(page.url or '')[:100]}")
        return None
    except Exception as exc:
        cb = _extract_callback_url_from_exception(exc)
        if cb:
            result = _try_extract_callback(cb)
            if result:
                log("  ✓ 从异常中提取 callback 完成 OAuth")
                return result
        log(f"  浏览器 OAuth consent 异常: {exc}")
        return None


def _submit_oauth_password_direct(page, password: str, log) -> dict:
    """OAuth 流程专用：直接填密码登录，不尝试恢复到注册态。"""
    input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=15)
    if not input_selector:
        # 密码输入框没出现，可能页面还在加载或跳转了
        # 等一下再试
        time.sleep(2)
        input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=10)
    if not input_selector:
        raise RuntimeError("OAuth 密码页未找到输入框")
    if not _fill_input_like_user(page, input_selector, password):
        raise RuntimeError("OAuth 密码页填写失败")
    log(f"  OAuth 密码页输入框: {input_selector}")
    _browser_pause(page)

    submit_selector = _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=8)
    if submit_selector:
        log(f"  OAuth 密码页已点击继续按钮: {submit_selector}")
    elif _submit_form_with_fallback(page, input_selector):
        log("  OAuth 密码页使用表单 fallback 提交")
    else:
        raise RuntimeError("OAuth 密码页未找到 Continue 按钮")

    deadline = time.time() + 20
    while time.time() < deadline:
        current_url = str(page.url or "")
        state = _derive_registration_state_from_page(page)
        page_type = str(state.get("page_type") or "")
        if page_type in {"email_otp_verification", "about_you", "consent", "workspace_selection",
                         "organization_selection", "add_phone", "oauth_callback", "chatgpt_home", "external_url"}:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if "code=" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        error_text = _extract_auth_error_text(page)
        if error_text:
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        time.sleep(0.5)
    return {"ok": False, "status": 0, "url": str(page.url or ""), "data": None, "text": "OAuth 密码提交后未跳转"}


def _submit_password_via_page(page, password: str, log) -> dict:
    if _recover_signup_password_page(page, log):
        time.sleep(1)

    input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=15)
    if not input_selector:
        raise RuntimeError("密码页未找到输入框")
    if not _fill_input_like_user(page, input_selector, password):
        raise RuntimeError("密码页填写失败")
    log(f"密码页输入框: {input_selector}")
    _browser_pause(page)

    start_url = str(page.url or "")
    submit_selector = _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=8)
    if submit_selector:
        log(f"密码页已点击继续按钮: {submit_selector}")
    elif _submit_form_with_fallback(page, input_selector):
        log("密码页未找到可点击 Continue，已使用表单 fallback 提交")
    else:
        raise RuntimeError("密码页未找到 Continue 按钮")

    deadline = time.time() + 20
    last_url = str(page.url or "")
    while time.time() < deadline:
        current_url = str(page.url or "")
        last_url = current_url or last_url
        state = _derive_registration_state_from_page(page)
        page_type = str(state.get("page_type") or "")
        if page_type in {"email_otp_verification", "about_you", "add_phone", "oauth_callback", "chatgpt_home"}:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if current_url != start_url and page_type and page_type not in {"create_account_password", "login_password"}:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if page_type == "login_password" and _recover_signup_password_page(page, log):
            input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=5)
            if not input_selector:
                return {"ok": False, "status": 400, "url": current_url, "data": None, "text": "登录密码页恢复后未找到注册密码输入框"}
            if not _fill_input_like_user(page, input_selector, password):
                return {"ok": False, "status": 400, "url": current_url, "data": None, "text": "登录密码页恢复后密码重新填写失败"}
            submit_selector = _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=5)
            if submit_selector:
                log(f"恢复后重新点击密码提交按钮: {submit_selector}")
                start_url = str(page.url or start_url)
                time.sleep(0.4)
                continue
            if _submit_form_with_fallback(page, input_selector):
                log("恢复后未找到密码提交按钮，已使用表单 fallback 提交")
                start_url = str(page.url or start_url)
                time.sleep(0.4)
                continue
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": "登录密码页恢复后未找到提交方式"}
        error_text = _extract_auth_error_text(page)
        if error_text:
            _dump_debug(page, "chatgpt_password_fail")
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        time.sleep(0.5)
    _dump_debug(page, "chatgpt_password_fail")
    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": "密码页提交后未跳转"}


def _submit_otp_via_page(page, code: str, log) -> dict:
    otp = str(code or "").strip()
    if not otp:
        return {"ok": False, "status": 400, "url": page.url, "data": None, "text": "验证码为空"}

    # 等待页面加载完成，确保 OTP 输入框已渲染
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    time.sleep(1)

    filled = False

    # 先尝试 6 格 OTP 输入框
    try:
        digit_inputs = page.locator(
            "input[inputmode='numeric'], input[autocomplete='one-time-code'], input[type='tel'], input[type='number']"
        )
        count = digit_inputs.count()
        if count >= len(otp):
            done = 0
            for i in range(min(count, len(otp))):
                box = digit_inputs.nth(i)
                try:
                    box.wait_for(state="visible", timeout=800)
                    box.fill("")
                    box.type(otp[i], delay=random.randint(20, 60))
                    done += 1
                except Exception:
                    break
            if done >= len(otp):
                filled = True
                log(f"验证码页已填写 {done} 位分格输入框")
    except Exception:
        pass

    # 再尝试单输入框
    if not filled:
        otp_candidates = [
            page.get_by_label(re.compile(r"verification code|code|otp|認証コード|確認コード|ワンタイムコード", re.IGNORECASE)),
            page.get_by_role("textbox", name=re.compile(r"verification code|code|otp|認証コード|確認コード|ワンタイムコード", re.IGNORECASE)),
            page.locator("input[autocomplete='one-time-code']"),
            page.locator("input[name*='code' i]"),
            page.locator("input[id*='code' i]"),
            page.locator("input[type='text']"),
            page.locator("input"),
        ]
        for candidate in otp_candidates:
            try:
                target = candidate.first
                target.wait_for(state="visible", timeout=1200)
                target.click(timeout=1200)
                target.fill("")
                target.type(otp, delay=random.randint(18, 45))
                final_value = str(target.input_value() or "").strip()
                if final_value:
                    filled = True
                    log("验证码页已填写单输入框")
                    break
            except Exception:
                continue

    if not filled:
        # 再等 3 秒重试一次（页面可能还在渲染）
        time.sleep(3)
        otp_retry_selectors = [
            "input[inputmode='numeric']",
            "input[autocomplete='one-time-code']",
            "input[name*='code' i]",
            "input[type='text']",
        ]
        for sel in otp_retry_selectors:
            try:
                target = page.locator(sel).first
                if target.is_visible(timeout=2000):
                    target.click(timeout=1500)
                    target.fill("")
                    target.type(otp, delay=random.randint(18, 45))
                    if str(target.input_value() or "").strip():
                        filled = True
                        log("验证码页已填写单输入框(重试)")
                        break
            except Exception:
                continue

    if not filled:
        return {"ok": False, "status": 0, "url": page.url, "data": None, "text": "验证码页未找到可填写输入框"}

    _browser_pause(page)
    submit_selector = _click_first(
        page,
        [
            'button[type="submit"]',
            'button[data-testid="continue-button"]',
            'button:has-text("Continue")',
            'button:has-text("continue")',
            'button:has-text("Verify")',
            'button:has-text("verify")',
            'button:has-text("Next")',
            'button:has-text("next")',
            'button:has-text("続ける")',
            'button:has-text("確認")',
            'button:has-text("認証")',
            'button:has-text("次へ")',
        ],
        timeout=8,
    )
    if not submit_selector:
        return {"ok": False, "status": 0, "url": page.url, "data": None, "text": "验证码页未找到 Continue 按钮"}
    log(f"验证码页已点击继续按钮: {submit_selector}")

    deadline = time.time() + 20
    last_url = page.url
    while time.time() < deadline:
        current_url = page.url
        last_url = current_url or last_url
        if "about-you" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if "add-phone" in current_url or "chatgpt.com" in current_url or "code=" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if "consent" in current_url or "sign-in-with-chatgpt" in current_url or "workspace" in current_url or "organization" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        try:
            error_text = page.locator("text=Invalid code").first.text_content(timeout=400)
        except Exception:
            error_text = ""
        if error_text:
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        time.sleep(0.5)
    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": "验证码页提交后未跳转"}


def _submit_about_you_via_page(page, log) -> dict:
    from .constants import generate_random_user_info

    user_info = generate_random_user_info()
    name = str(user_info.get("name") or "").strip()
    birthdate = str(user_info.get("birthdate") or "").strip()
    if not name or not birthdate:
        raise RuntimeError("about_you 数据生成失败")
    date_parts = birthdate.split("-")
    if len(date_parts) == 3:
        yyyy, mm, dd = date_parts
        us_birthdate = f"{mm}/{dd}/{yyyy}"
        cn_birthdate = f"{yyyy}/{mm}/{dd}"
    else:
        us_birthdate = birthdate
        cn_birthdate = birthdate.replace("-", "/")
    log(f"about_you 表单: name={name}, birthdate={birthdate}, ui_birthdate={us_birthdate}, cn_birthdate={cn_birthdate}")

    def _fill_locator(locator, value: str) -> bool:
        try:
            target = locator.first
            target.wait_for(state="visible", timeout=1500)
            target.click(timeout=1500)
            _browser_pause(page, headed=False)
            try:
                applied = bool(
                    target.evaluate(
                        """
                        (input, nextValue) => {
                          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                          if (!setter) return false;
                          setter.call(input, nextValue);
                          input.dispatchEvent(new Event('input', { bubbles: true }));
                          input.dispatchEvent(new Event('change', { bubbles: true }));
                          return String(input.value || '') === String(nextValue || '');
                        }
                        """,
                        value,
                    )
                )
            except Exception:
                applied = False
            if not applied:
                target.fill("")
                target.type(value, delay=random.randint(25, 70))
            try:
                target.dispatch_event("blur")
            except Exception:
                pass
            final_val = str(target.input_value() or "").strip()
            return final_val == str(value).strip()
        except Exception:
            return False

    def _locator_from_visible_input_entry(entry: dict):
        try:
            visible_index = int(entry.get("visibleIndex"))
        except Exception:
            return None
        return page.locator("input:visible:not([type='hidden']):not([disabled]):not([readonly])").nth(visible_index)

    def _fill_visible_input_entry(entry: dict | None, value: str) -> bool:
        if not entry:
            return False
        locator = _locator_from_visible_input_entry(entry)
        if locator is None:
            return False
        return _fill_locator(locator, value)

    def _resolve_visible_input_selector(selectors: list[str]) -> str | None:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=500)
                return selector
            except Exception:
                continue
        return None

    def _fill_second_visible_input(values: list[str], excluded_visible_indices: set[int] | None = None) -> bool:
        """兜底：about_you 卡片一般是 Full name + Birthday/Age 两个输入框。"""
        try:
            locator = page.locator(
                "input:visible:not([type='hidden']):not([disabled]):not([readonly])"
            )
            count = locator.count()
            if count < 2:
                return False
            excluded = {int(value) for value in (excluded_visible_indices or set())}
            target_index = None
            for idx in range(count):
                if idx not in excluded:
                    target_index = idx
                    if idx > 0:
                        break
            if target_index is None:
                return False
            target = locator.nth(target_index)
            target.click(timeout=1200)
            _browser_pause(page, headed=False)
            for value in values:
                try:
                    target.fill("")
                except Exception:
                    pass
                try:
                    target.type(str(value), delay=random.randint(18, 45))
                except Exception:
                    continue
                final_val = str(target.input_value() or "").strip()
                if final_val:
                    return True
            return False
        except Exception:
            return False

    def _has_visible(locator) -> bool:
        try:
            locator.first.wait_for(state="visible", timeout=700)
            return True
        except Exception:
            return False

    def _fill_birthday_selects(yyyy: str, mm: str, dd: str) -> bool:
        """处理 Month/Day/Year 下拉样式的生日控件。"""
        try:
            select_locator = page.locator("select:visible")
            count = select_locator.count()
            if count < 2:
                return False

            month_num = int(mm)
            day_num = int(dd)
            year_num = int(yyyy)
            month_short = time.strftime("%b", time.strptime(str(month_num), "%m"))
            month_full = time.strftime("%B", time.strptime(str(month_num), "%m"))

            assigned = {"month": False, "day": False, "year": False}

            for i in range(count):
                sel = select_locator.nth(i)
                try:
                    options = sel.locator("option")
                    option_count = options.count()
                except Exception:
                    option_count = 0
                if option_count <= 0:
                    continue

                texts: list[str] = []
                for idx in range(min(option_count, 80)):
                    try:
                        texts.append(str(options.nth(idx).inner_text(timeout=300) or "").strip())
                    except Exception:
                        continue
                joined = " ".join(texts).lower()

                try:
                    if (not assigned["month"]) and (
                        "january" in joined or "february" in joined or "march" in joined or "april" in joined
                    ):
                        for candidate in (month_full, month_short, str(month_num), f"{month_num:02d}"):
                            try:
                                sel.select_option(label=candidate, timeout=800)
                                assigned["month"] = True
                                break
                            except Exception:
                                try:
                                    sel.select_option(value=candidate, timeout=800)
                                    assigned["month"] = True
                                    break
                                except Exception:
                                    continue
                        continue

                    if (not assigned["year"]) and any(str(y) in joined for y in (year_num, year_num - 1, year_num + 1, 2026, 2025)):
                        for candidate in (str(year_num),):
                            try:
                                sel.select_option(label=candidate, timeout=800)
                                assigned["year"] = True
                                break
                            except Exception:
                                try:
                                    sel.select_option(value=candidate, timeout=800)
                                    assigned["year"] = True
                                    break
                                except Exception:
                                    continue
                        continue

                    if (not assigned["day"]) and any(str(x) in joined for x in (" 1 ", "2", "30", "31")):
                        for candidate in (str(day_num), f"{day_num:02d}"):
                            try:
                                sel.select_option(label=candidate, timeout=800)
                                assigned["day"] = True
                                break
                            except Exception:
                                try:
                                    sel.select_option(value=candidate, timeout=800)
                                    assigned["day"] = True
                                    break
                                except Exception:
                                    continue
                except Exception:
                    continue

            # 下拉顺序兜底：month/day/year
            if count >= 3:
                try:
                    if not assigned["month"]:
                        select_locator.nth(0).select_option(label=month_short, timeout=800)
                        assigned["month"] = True
                except Exception:
                    pass
                try:
                    if not assigned["day"]:
                        select_locator.nth(1).select_option(label=str(day_num), timeout=800)
                        assigned["day"] = True
                except Exception:
                    pass
                try:
                    if not assigned["year"]:
                        select_locator.nth(2).select_option(label=str(year_num), timeout=800)
                        assigned["year"] = True
                except Exception:
                    pass

            return assigned["month"] and assigned["day"] and assigned["year"]
        except Exception:
            return False

    visible_inputs = _collect_visible_text_inputs(page)
    if visible_inputs:
        log(
            "about_you 可见输入框: "
            + " | ".join(
                f"#{int(item.get('visibleIndex', 0))} {(_about_you_input_hints(item) or '-')[:80]}"
                for item in visible_inputs[:4]
            )
        )
    ordered_visible_entries = sorted(
        [item for item in visible_inputs if str(item.get("visibleIndex", "")).isdigit()],
        key=lambda item: int(item.get("visibleIndex", 0)),
    )
    name_entry = _pick_best_about_you_input(visible_inputs, "name")
    age_entry = _pick_best_about_you_input(
        visible_inputs,
        "age",
        exclude_visible_indices={int(name_entry.get("visibleIndex"))} if name_entry and str(name_entry.get("visibleIndex", "")).isdigit() else set(),
    )

    name_candidates = [
        page.get_by_label(re.compile(r"full\s*name", re.IGNORECASE)),
        page.get_by_label(re.compile(r"全名|姓名|氏名|お名前|フルネーム", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"full\s*name|name", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"全名|姓名|氏名|お名前|フルネーム", re.IGNORECASE)),
        page.locator("input[autocomplete='name']"),
        page.locator("input[name*='name' i]"),
        page.locator("input[id*='name' i]"),
        page.locator("input[name*='姓名']"),
        page.locator("input[id*='姓名']"),
        page.locator(
            "xpath=//*[contains(translate(normalize-space(string(.)),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'full name')]/following::input[1]"
        ),
        page.locator("xpath=//*[contains(normalize-space(string(.)),'全名') or contains(normalize-space(string(.)),'姓名')]/following::input[1]"),
    ]
    birthday_candidates = [
        page.get_by_label(re.compile(r"birthday|date of birth|birth", re.IGNORECASE)),
        page.get_by_label(re.compile(r"生日|出生|生年月日|誕生日", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"birthday|date of birth|birth", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"生日|出生|生年月日|誕生日", re.IGNORECASE)),
        page.get_by_placeholder(re.compile(r"mm.?dd.?yyyy|yyyy.?mm.?dd|birthday|生日|生年月日|誕生日", re.IGNORECASE)),
        page.locator("input[name*='birth' i]"),
        page.locator("input[id*='birth' i]"),
        page.locator("input[placeholder*='MM' i]"),
        page.locator("input[placeholder*='DD' i]"),
        page.locator("input[placeholder*='YYYY' i]"),
        page.locator("input[placeholder*='年']"),
        page.locator("input[placeholder*='月']"),
        page.locator("input[placeholder*='日']"),
        page.locator("input[inputmode='numeric']"),
        page.locator(
            "xpath=//*[contains(translate(normalize-space(string(.)),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'birthday')]/following::input[1]"
        ),
        page.locator("xpath=//*[contains(normalize-space(string(.)),'生日') or contains(normalize-space(string(.)),'出生')]/following::input[1]"),
        page.locator("input[type='date']"),
    ]

    age_years = None
    try:
        birth_year = int(str(birthdate).split("-")[0])
        current_year = int(time.strftime("%Y"))
        age_years = max(25, min(40, current_year - birth_year))
    except Exception:
        age_years = random.randint(25, 35)
    birth_year_text = str(date_parts[0]) if len(date_parts) == 3 else str(birthdate).split("-")[0]

    age_candidates = [
        page.get_by_label(re.compile(r"age", re.IGNORECASE)),
        page.get_by_label(re.compile(r"年龄|年齢", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"age", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"年龄|年齢", re.IGNORECASE)),
        page.locator("input[name*='age' i]"),
        page.locator("input[id*='age' i]"),
        page.locator("input[placeholder*='Age' i]"),
        page.locator("input[placeholder*='年龄']"),
        page.locator("input[placeholder*='年齢']"),
        page.locator(
            "xpath=//*[contains(translate(normalize-space(string(.)),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'age')]/following::input[1]"
        ),
        page.locator("xpath=//*[contains(normalize-space(string(.)),'年龄')]/following::input[1]"),
    ]

    fill_result = {"name": False, "birthdate": False, "age": False, "month": False, "day": False, "year": False}
    if _fill_visible_input_entry(name_entry, name):
        fill_result["name"] = True
    if not fill_result.get("name"):
        for candidate in name_candidates:
            if _fill_locator(candidate, name):
                fill_result["name"] = True
                break
    mode_probe = {}
    try:
        mode_probe = page.evaluate(
            """
            () => {
              const labels = Array.from(document.querySelectorAll('label'))
                .map((n) => String(n.textContent || '').trim().toLowerCase())
                .filter(Boolean);
              const placeholders = Array.from(document.querySelectorAll('input'))
                .map((n) => String(n.placeholder || '').trim().toLowerCase())
                .filter(Boolean);
              const headings = Array.from(document.querySelectorAll('h1,h2,h3'))
                .map((n) => String(n.textContent || '').trim().toLowerCase())
                .filter(Boolean);
              const allText = labels.concat(placeholders).concat(headings);
              const hasYearOfBirth = allText.some((t) =>
                t.includes('year of birth') || t.includes('birth year') || t.includes('birthyear') || t.includes('出生年份') || t.includes('生年')
              );
              const hasAge = allText.some((t) => t === 'age' || t === 'edad' || t === 'âge' || t === 'alter' || t === 'idade' || t === '年齢' || t.includes('how old') || t.includes('年龄') || t.includes('年齢') || t.includes('나이'));
              const hasBirthday = allText.some((t) =>
                t.includes('birthday') || t.includes('date of birth') || (t.includes('birth') && !t.includes('year of birth') && !t.includes('birth year')) || t.includes('生日') || t.includes('出生') || t.includes('生年月日') || t.includes('誕生日') || t.includes('fecha de nacimiento') || t.includes('nascimento') || t.includes('geburtstag') || t.includes('naissance')
              );
              return { labels, placeholders, headings, hasAge, hasBirthday, hasYearOfBirth };
            }
            """
        ) or {}
    except Exception:
        mode_probe = {}

    has_age_label = bool(mode_probe.get("hasAge"))
    has_birthday_label = bool(mode_probe.get("hasBirthday"))
    has_year_of_birth_label = bool(mode_probe.get("hasYearOfBirth"))
    has_age_field = any(_has_visible(candidate) for candidate in age_candidates[:3])
    has_birthday_field = any(_has_visible(candidate) for candidate in birthday_candidates[:3])
    has_birthday_select = False
    try:
        has_birthday_select = page.locator("select:visible").count() >= 2
    except Exception:
        has_birthday_select = False
    if has_year_of_birth_label:
        about_mode = "year_of_birth"
    elif has_birthday_select:
        about_mode = "birthday_select"
    elif (has_age_label and not has_birthday_label) or (has_age_field and not has_birthday_field):
        about_mode = "age"
    else:
        about_mode = "birthday"
    log(f"about_you 页面模式: {about_mode} labels={mode_probe.get('labels', [])[:4]}")
    direct_name_selector = _resolve_visible_input_selector(
        [
            'input[name="name"]',
            'input[name="full_name"]',
            'input[autocomplete="name"]',
            'input[placeholder*="全名"]',
            'input[placeholder*="name" i]',
            'input[id*="name" i]:not([type="hidden"])',
        ]
    )
    direct_age_selector = _resolve_visible_input_selector(
        [
            'input[name="age"]',
            'input[placeholder="Age"]',
            'input[placeholder="age"]',
            'input[placeholder*="年龄"]',
            'input[id*="age" i]',
        ]
    )
    if about_mode in {"age", "year_of_birth"} and len(ordered_visible_entries) >= 2:
        name_entry = ordered_visible_entries[0]
        age_entry = ordered_visible_entries[1]
        log(
            f"about_you {about_mode} 输入框映射: name=#{int(name_entry.get('visibleIndex', 0))}, "
            f"value=#{int(age_entry.get('visibleIndex', 0))}"
        )
    if about_mode in {"age", "year_of_birth"}:
        log(
            f"about_you {about_mode} 直接定位: "
            f"name={direct_name_selector or '-'}, age={direct_age_selector or '-'}"
        )

    def _fill_segmented_date(mm: str, dd: str, yyyy: str) -> bool:
        """处理 MM / DD / YYYY 分段日期输入框（React DateField 样式）。
        特征：一个 Birthday label 下有多个小 input 或 div[data-type] 段。"""
        try:
            # 方式1: div[data-type] 段 (React Aria DateField)
            month_seg = page.locator('div[data-type="month"], input[data-type="month"]')
            day_seg = page.locator('div[data-type="day"], input[data-type="day"]')
            year_seg = page.locator('div[data-type="year"], input[data-type="year"]')
            if month_seg.count() > 0 and day_seg.count() > 0 and year_seg.count() > 0:
                month_seg.first.click(force=True)
                page.keyboard.type(mm, delay=50)
                time.sleep(0.3)
                day_seg.first.click(force=True)
                page.keyboard.type(dd, delay=50)
                time.sleep(0.3)
                year_seg.first.click(force=True)
                page.keyboard.type(yyyy, delay=50)
                return True

            # 方式2: 单个 date input 里有 MM/DD/YYYY 占位符
            # 点击输入框，然后按顺序输入 MM DD YYYY（Tab 切换段）
            date_input = page.locator("input[placeholder*='MM'], input[placeholder*='mm'], input[type='date']")
            if date_input.count() > 0:
                date_input.first.click(force=True)
                time.sleep(0.2)
                page.keyboard.type(mm, delay=50)
                page.keyboard.type(dd, delay=50)
                page.keyboard.type(yyyy, delay=50)
                return True

            # 方式3: Birthday label 下的第二个可见 input，直接点击后按数字键输入
            birthday_input = page.get_by_label(re.compile(r"birthday|birth", re.IGNORECASE))
            if birthday_input.count() > 0:
                birthday_input.first.click(force=True)
                time.sleep(0.2)
                page.keyboard.type(mm, delay=50)
                page.keyboard.type(dd, delay=50)
                page.keyboard.type(yyyy, delay=50)
                return True

            # 方式4: 第二个可见 input（name 是第一个）
            inputs = page.locator("input:visible:not([type='hidden']):not([disabled])")
            if inputs.count() >= 2:
                target = inputs.nth(1)
                target.click(force=True)
                time.sleep(0.3)
                # 先清空
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                time.sleep(0.1)
                # 输入 MM，Tab 到 DD，Tab 到 YYYY
                page.keyboard.type(mm, delay=80)
                time.sleep(0.3)
                page.keyboard.type(dd, delay=80)
                time.sleep(0.3)
                page.keyboard.type(yyyy, delay=80)
                time.sleep(0.3)
                # 验证是否填入了正确的值
                val = str(target.input_value() or "").strip()
                if val and val != target.get_attribute("placeholder"):
                    return True
                # 如果直接输入不行，试 Tab 切换
                target.click(force=True)
                time.sleep(0.2)
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                for i, part in enumerate([mm, dd, yyyy]):
                    page.keyboard.type(part, delay=80)
                    if i < 2:
                        page.keyboard.press("Tab")
                        time.sleep(0.2)
                return True
        except Exception:
            pass
        return False

    if about_mode == "birthday_select":
        if len(date_parts) == 3 and _fill_birthday_selects(yyyy, mm, dd):
            fill_result["month"] = True
            fill_result["day"] = True
            fill_result["year"] = True
            fill_result["birthdate"] = True
    elif about_mode in {"age", "year_of_birth"}:
        value = birth_year_text if about_mode == "year_of_birth" else str(age_years)
        if direct_name_selector and _fill_input_like_user(page, direct_name_selector, name):
            fill_result["name"] = True
        elif _fill_visible_input_entry(name_entry, name):
            fill_result["name"] = True
        if value:
            if direct_age_selector and about_mode == "age" and _fill_input_like_user(page, direct_age_selector, value):
                fill_result["age"] = True
            elif _fill_visible_input_entry(age_entry, value):
                fill_result["age"] = True
            if not fill_result.get("age") and len(ordered_visible_entries) < 2:
                for candidate in age_candidates:
                    if _fill_locator(candidate, value):
                        fill_result["age"] = True
                        break
        # fallback: 直接找 placeholder="Age" 的输入框
        if not fill_result.get("age") and about_mode == "age" and age_years is not None and len(ordered_visible_entries) < 2:
            try:
                age_input = page.locator("input[placeholder='Age'], input[placeholder='age']")
                if age_input.count() > 0:
                    age_input.first.click(force=True)
                    time.sleep(0.2)
                    age_input.first.fill("")
                    age_input.first.type(str(age_years), delay=random.randint(30, 60))
                    fill_result["age"] = True
            except Exception:
                pass
        if not fill_result.get("age") and value:
            excluded_indices = set()
            if name_entry and str(name_entry.get("visibleIndex", "")).isdigit():
                excluded_indices.add(int(name_entry.get("visibleIndex")))
            if _fill_second_visible_input([value], excluded_visible_indices=excluded_indices):
                fill_result["age"] = True
        if len(date_parts) == 3 and _sync_hidden_birthday_input(page, f"{yyyy}-{mm}-{dd}", log):
            fill_result["birthdate"] = True
    elif about_mode == "birthday" or about_mode == "birthday_text":
        # 先尝试分段日期输入（MM / DD / YYYY 格式的 DateField）
        if len(date_parts) == 3 and _fill_segmented_date(mm, dd, yyyy):
            fill_result["birthdate"] = True
            log("about_you 使用分段日期输入成功")
        # 再尝试普通文本输入
        if not fill_result.get("birthdate"):
            for candidate in birthday_candidates:
                if _fill_locator(candidate, cn_birthdate):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, us_birthdate):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, birthdate):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, cn_birthdate.replace("/", "")):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, us_birthdate.replace("/", "")):
                    fill_result["birthdate"] = True
                    break
        if not fill_result.get("birthdate"):
            fallback_values = [cn_birthdate, cn_birthdate.replace("/", " / "), cn_birthdate.replace("/", ""), us_birthdate, us_birthdate.replace("/", " / "), us_birthdate.replace("/", ""), birthdate]
            if _fill_second_visible_input(fallback_values):
                fill_result["birthdate"] = True

    log(f"about_you 填写结果: {fill_result}")
    if not fill_result.get("name"):
        raise RuntimeError("about_you 未成功填写 Full name")
    if not (
        fill_result.get("birthdate")
        or fill_result.get("age")
        or (fill_result.get("month") and fill_result.get("day") and fill_result.get("year"))
    ):
        raise RuntimeError("about_you 未成功填写 Birthday/Age")
    _browser_pause(page)

    submit_selector = _click_about_you_submit(page, timeout=14)
    if not submit_selector:
        button_summary = _visible_button_summary(page)
        detail = f": {button_summary}" if button_summary else ""
        raise RuntimeError(f"about_you 未找到提交按钮{detail}")
    log(f"about_you 已点击继续按钮: {submit_selector}")

    deadline = time.time() + 20
    retried_generic_validation = False
    last_url = page.url
    while time.time() < deadline:
        current_url = page.url
        last_url = current_url or last_url
        if "code=" in current_url or "chatgpt.com" in current_url or "sign-in-with-chatgpt" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if "add-phone" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        try:
            error_text = page.locator("text=Sorry, we cannot create your account").first.text_content(timeout=500)
        except Exception:
            error_text = ""
        if not error_text:
            try:
                error_text = page.locator("text=Enter a valid age to continue").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if not error_text:
            try:
                error_text = page.locator("text=doesn't look right").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if not error_text:
            try:
                error_text = page.locator("[role='alert']").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if not error_text:
            try:
                error_text = page.locator(".error, [class*='error'], [class*='Error']").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if error_text and "oai_log" not in error_text and "SSR_HTML" not in error_text:
            normalized_error = str(error_text).strip().lower()
            if (
                about_mode == "age"
                and not retried_generic_validation
                and ("doesn't look right" in normalized_error or "try again" in normalized_error)
            ):
                retried_generic_validation = True
                log("about_you age 模式提交被拒，重新同步 Full name/Age/hidden birthday 后重试一次...")
                if direct_name_selector and _fill_input_like_user(page, direct_name_selector, name):
                    fill_result["name"] = True
                elif _fill_visible_input_entry(name_entry, name):
                    fill_result["name"] = True
                elif len(ordered_visible_entries) < 2:
                    for candidate in name_candidates:
                        if _fill_locator(candidate, name):
                            fill_result["name"] = True
                            break
                if age_years is not None:
                    if direct_age_selector and _fill_input_like_user(page, direct_age_selector, str(age_years)):
                        fill_result["age"] = True
                    elif _fill_visible_input_entry(age_entry, str(age_years)):
                        fill_result["age"] = True
                    elif len(ordered_visible_entries) < 2:
                        for candidate in age_candidates:
                            if _fill_locator(candidate, str(age_years)):
                                fill_result["age"] = True
                                break
                if len(date_parts) == 3 and _sync_hidden_birthday_input(page, f"{yyyy}-{mm}-{dd}", log):
                    fill_result["birthdate"] = True
                _browser_pause(page)
                retry_submit_selector = _click_about_you_submit(page, timeout=6)
                if retry_submit_selector:
                    log(f"about_you 重试提交按钮: {retry_submit_selector}")
                    time.sleep(0.5)
                    continue
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        time.sleep(0.5)
    _dump_debug(page, "chatgpt_about_you_fail")
    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": "about_you 提交后未跳转"}


def _browser_registration_flow(page, email: str, password: str, otp_callback, phone_callback, log) -> dict:
    device_id = str(uuid.uuid4())
    try:
        user_agent = str(page.evaluate("() => navigator.userAgent") or "").strip() or _random_chrome_ua()
    except Exception:
        user_agent = _random_chrome_ua()

    _seed_browser_device_id(page, device_id)
    try:
        log("使用 ChatGPT NextAuth 注册入口启动浏览器注册")
        state = _start_browser_signup_via_authorize(page, email, device_id, log)
    except Exception as exc:
        log(f"ChatGPT NextAuth 注册入口失败: {exc}")
        if _is_browser_driver_disconnect(exc):
            raise
        log("回退到 OpenAI 页面注册入口...")
        state = _start_browser_signup_via_page(page, email, log)
    auth_cookies = _get_cookies(page)
    log(
        "授权态 cookies: "
        f"login_session={'yes' if auth_cookies.get('login_session') else 'no'}, "
        f"oai-did={'yes' if auth_cookies.get('oai-did') else 'no'}"
    )
    log(f"注册状态起点: page={state.get('page_type') or '-'} url={(state.get('current_url') or '')[:100]}")
    register_submitted = False
    seen_states: dict[str, int] = {}

    for step in range(12):
        signature = "|".join(
            [
                str(state.get("page_type") or ""),
                str(state.get("method") or ""),
                str(state.get("continue_url") or ""),
                str(state.get("current_url") or ""),
            ]
        )
        seen_states[signature] = seen_states.get(signature, 0) + 1
        log(
            f"注册状态推进: step={step+1} page={state.get('page_type') or '-'} "
            f"next={str(state.get('continue_url') or '')[:60]} seen={seen_states[signature]}"
        )
        if seen_states[signature] > 2:
            raise RuntimeError(f"注册状态卡住: page={state.get('page_type') or '-'}")

        if _is_registration_complete(state):
            _handle_post_signup_onboarding(page, log)
            return _extract_flow_state(None, page.url)

        if _is_password_registration(state):
            if register_submitted:
                raise RuntimeError("重复进入密码注册阶段")
            log("提交注册密码...")
            pre_cookies = _get_cookies(page)
            log(
                "密码阶段 cookies: "
                f"login_session={'yes' if pre_cookies.get('login_session') else 'no'}, "
                f"oai-client-auth-session={'yes' if pre_cookies.get('oai-client-auth-session') else 'no'}"
            )
            reg_resp = _submit_password_via_page(page, password, log)
            log(f"密码页提交状态: {reg_resp.get('status', 0)}")
            if not reg_resp.get("ok"):
                raise RuntimeError(f"密码页提交失败: {(reg_resp.get('text') or '')[:300]}")
            register_submitted = True
            state = _extract_flow_state(reg_resp.get("data"), reg_resp.get("url", page.url))
            if not state.get("page_type") or _is_password_registration(state):
                state = _derive_registration_state_from_page(page)
            continue

        if str(state.get("page_type") or "") == "login_password":
            if _recover_signup_password_page(page, log):
                state = _derive_registration_state_from_page(page)
                continue
            log("注册流程落到已有账号登录密码页，按登录流程继续认证...")
            login_resp = _submit_oauth_password_direct(page, password, log)
            log(f"登录密码页提交状态: {login_resp.get('status', 0)}")
            if not login_resp.get("ok"):
                raise RuntimeError(f"登录密码页提交失败: {(login_resp.get('text') or '')[:300]}")
            state = _extract_flow_state(login_resp.get("data"), login_resp.get("url", page.url))
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            continue

        if _is_email_otp(state):
            if not otp_callback:
                raise RuntimeError("ChatGPT 注册需要邮箱验证码但未提供 otp_callback")
            log("等待 ChatGPT 验证码")
            code = otp_callback()
            if not code:
                raise RuntimeError("未获取到验证码")
            otp_resp = _submit_otp_via_page(page, code, log)
            if not otp_resp.get("ok") and int(otp_resp.get("status") or 0) == 0:
                original_text = str(otp_resp.get("text") or "")
                log(f"验证码页 DOM 提交失败，改用浏览器内接口校验: {original_text[:160]}")
                fallback_resp = _validate_browser_email_otp(
                    page,
                    code,
                    device_id=device_id,
                    user_agent=user_agent,
                    referer=str(page.url or f"{OPENAI_AUTH}/email-verification"),
                )
                if fallback_resp.get("ok") or int(fallback_resp.get("status") or 0) in (200, 201, 204):
                    log(f"验证码页接口校验成功: {fallback_resp.get('status', 0)}")
                    otp_resp = fallback_resp
                else:
                    fallback_text = str(fallback_resp.get("text") or "")
                    otp_resp = {
                        **otp_resp,
                        "text": f"{original_text}; fallback validate failed: {fallback_text[:220]}",
                    }
            log(f"验证码页提交状态: {otp_resp.get('status', 0)}")
            if not otp_resp.get("ok"):
                raise RuntimeError(f"验证码校验失败: {(otp_resp.get('text') or '')[:300]}")
            state = _extract_flow_state(otp_resp.get("data"), otp_resp.get("url", page.url))
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            continue

        if _is_about_you(state):
            log("提交 about_you 信息...")
            target_url = _normalize_url(
                str(state.get("current_url") or state.get("continue_url") or f"{OPENAI_AUTH}/about-you"),
                OPENAI_AUTH,
            )
            if "about-you" not in str(page.url):
                log(f"跳转到 about_you 页面: {target_url[:120]}")
                _goto_with_retry(page, target_url, wait_until="domcontentloaded", timeout=30000, log=log)
            about_resp = _submit_about_you_via_page(page, log)
            log(f"about_you 提交状态: {about_resp.get('status', 0)}")
            if not about_resp.get("ok"):
                raise RuntimeError(f"about_you 提交失败: {(about_resp.get('text') or '')[:300]}")
            state = _extract_flow_state(about_resp.get("data"), about_resp.get("url", page.url))
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            if _is_add_phone(state):
                if not phone_callback:
                    log("⚠️ about_you 后进入 add_phone 但未配置 phone_callback，需要人工处理")
                    raise RuntimeError("manual_phone_required: 注册流程在 add_phone 页面中断，未配置接码服务。请在设置中配置 SMSPool 或其他接码 provider 后重试。")
                log("about_you 后进入 add_phone，尝试短信验证...")
                state = _handle_add_phone_challenge(
                    page,
                    phone_callback,
                    device_id=device_id,
                    user_agent=user_agent,
                    log=log,
                    resume_url=f"{CHATGPT_APP}/",
                )
            continue

        if _is_add_phone(state):
            if not phone_callback:
                log("⚠️ 注册流程进入 add_phone 但未配置 phone_callback，需要人工处理")
                raise RuntimeError("manual_phone_required: 未配置接码服务，请在设置中配置 SMSPool 后重试。")
            log("注册流程进入 add_phone，尝试短信验证...")
            state = _handle_add_phone_challenge(
                page,
                phone_callback,
                device_id=device_id,
                user_agent=user_agent,
                log=log,
                resume_url=f"{CHATGPT_APP}/",
            )
            continue

        if _requires_registration_navigation(state):
            target_url = _normalize_url(str(state.get("continue_url") or state.get("current_url") or ""), OPENAI_AUTH)
            if not target_url:
                raise RuntimeError("缺少可跟随的 continue_url")
            _goto_with_retry(page, target_url, wait_until="domcontentloaded", timeout=30000, log=log)
            state = _extract_flow_state(None, page.url)
            continue

        raise RuntimeError(f"未支持的注册状态: page={state.get('page_type') or '-'}")

    raise RuntimeError("注册状态机超出最大步数")


def _phone_signup_entry_status(page) -> dict:
    try:
        result = page.evaluate(
            r"""
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0;
              };
              const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
              const phoneInput = inputs.find((el) => {
                const hint = `${el.type || ''} ${el.name || ''} ${el.id || ''} ${el.placeholder || ''} ${el.autocomplete || ''} ${el.getAttribute('aria-label') || ''}`;
                return /(^|\s)tel(\s|$)|phone|mobile|手机号|手机号码|电话号码/i.test(hint);
              });
              const emailInput = inputs.find((el) => {
                const hint = `${el.type || ''} ${el.name || ''} ${el.id || ''} ${el.placeholder || ''} ${el.autocomplete || ''} ${el.getAttribute('aria-label') || ''}`;
                return /email|username|邮箱|电子邮件/i.test(hint);
              });
              const passwordInput = inputs.find((el) => el.type === 'password' || /password/i.test(`${el.name || ''} ${el.autocomplete || ''}`));
              const verificationForm = document.querySelector('form[action*="/phone-verification" i]');
              const codeInput = verificationForm?.querySelector('input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]');
              const alerts = Array.from(document.querySelectorAll('[role="alert"], [class*="error" i], [slot="errorMessage"]'))
                .filter(visible)
                .map((el) => String(el.textContent || '').replace(/\s+/g, ' ').trim())
                .filter(Boolean);
              return {
                url: location.href,
                phoneReady: Boolean(phoneInput),
                emailReady: Boolean(emailInput),
                passwordReady: Boolean(passwordInput),
                phoneVerificationReady: Boolean(verificationForm && codeInput && visible(codeInput)),
                error: alerts[0] || '',
              };
            }
            """
        )
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _click_phone_signup_action(page, action: str) -> bool:
    patterns = {
        "signup": r"sign\s*up|register|create\s+account|注册|创建账号|新規登録|アカウントを作成|サインアップ",
        "phone": r"continue\s+(?:with|using)\s+(?:a\s+)?phone|use\s+(?:a\s+)?phone|sign\s*(?:in|up)\s+with\s+(?:a\s+)?phone|手机号|手机号码|电话号码|電話番号",
        "more": r"more\s+options|show\s+more|other\s+(?:options|ways)|更多选项|其他方式|其它方式",
    }
    pattern = patterns[action]
    try:
        return bool(
            page.evaluate(
                """
                (pattern) => {
                  const visible = (el) => {
                    if (!el || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden'
                      && rect.width > 0 && rect.height > 0;
                  };
                  const re = new RegExp(pattern, 'i');
                  const candidates = Array.from(document.querySelectorAll('button, a, [role="button"], [role="link"]'));
                  const target = candidates.find((el) => visible(el) && re.test(String(el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim()));
                  if (!target) return false;
                  target.click();
                  return true;
                }
                """,
                pattern,
            )
        )
    except Exception:
        return False


def _open_phone_signup_entry(page, log) -> dict:
    log("打开 ChatGPT 官网手机号注册入口...")
    _goto_with_retry(page, f"{CHATGPT_APP}/", wait_until="domcontentloaded", timeout=30000, log=log)
    deadline = time.time() + 35
    last_action_at = 0.0
    signup_clicked = False
    more_clicked = False
    while time.time() < deadline:
        status = _phone_signup_entry_status(page)
        if status.get("passwordReady") or status.get("phoneVerificationReady"):
            return status
        if status.get("phoneReady"):
            log("手机号注册输入页已就绪")
            return status
        now = time.time()
        if now - last_action_at >= 1.2:
            clicked = False
            if status.get("emailReady"):
                clicked = _click_phone_signup_action(page, "phone")
                if not clicked and not more_clicked:
                    clicked = _click_phone_signup_action(page, "more")
                    more_clicked = more_clicked or clicked
            elif not signup_clicked:
                clicked = _click_phone_signup_action(page, "signup")
                signup_clicked = signup_clicked or clicked
            else:
                clicked = _click_phone_signup_action(page, "phone")
                if not clicked and not more_clicked:
                    clicked = _click_phone_signup_action(page, "more")
                    more_clicked = more_clicked or clicked
            if clicked:
                last_action_at = now
        time.sleep(0.35)
    status = _phone_signup_entry_status(page)
    raise RuntimeError(f"未找到手机号注册入口: url={str(status.get('url') or page.url)[:160]}")


def _submit_signup_phone_identity(page, phone_number: str, log) -> dict:
    dial_code, local_number, country_name = _parse_phone_country_and_local(phone_number)
    country_selected = _select_phone_country_ui(page, dial_code, country_name, log)
    phone_input_sel = _wait_for_any_selector(page, PHONE_INPUT_SELECTORS, timeout=10)
    if not phone_input_sel:
        raise RuntimeError("手机号注册页未找到手机号输入框")
    fill_value = local_number if country_selected else phone_number
    e164 = "+" + str(phone_number or "").lstrip("+").strip()
    if not _fill_signup_phone_input(page, phone_input_sel, fill_value, e164):
        raise RuntimeError("手机号注册页填写手机号失败")
    clicked = _click_first_no_wait(page, PHONE_SEND_SELECTORS, timeout=8)
    if not clicked and not _submit_form_with_fallback(page, phone_input_sel):
        raise RuntimeError("手机号注册页未找到可提交的继续按钮")
    log(f"手机号已提交: {_mask_phone_number(e164)}")

    deadline = time.time() + 35
    last_status = {}
    while time.time() < deadline:
        last_status = _phone_signup_entry_status(page)
        state = _derive_registration_state_from_page(page)
        if last_status.get("passwordReady") or last_status.get("phoneVerificationReady"):
            return state
        if _is_about_you(state) or _is_registration_complete(state):
            return state
        error = str(last_status.get("error") or "").strip()
        if error:
            raise RuntimeError(f"手机号提交失败: {error[:200]}")
        time.sleep(0.4)
    raise RuntimeError(f"手机号提交后页面未推进: {str(last_status.get('url') or page.url)[:160]}")


def _reset_phone_callback_for_retry(phone_callback, reason: str) -> None:
    if hasattr(phone_callback, "mark_send_failed"):
        phone_callback.mark_send_failed(reason)
    if hasattr(phone_callback, "cleanup"):
        phone_callback.cleanup()
    if hasattr(phone_callback, "phase"):
        phone_callback.phase = "need_number"
        phone_callback.activation = None
        phone_callback.completed = False


class _PhoneSignupRetryableError(RuntimeError):
    pass


def _complete_phone_signup_attempt(page, password: str, phone_callback, state: dict, phone_number: str, log) -> dict:
    phone_send_marked = False
    for _step in range(12):
        if _is_registration_complete(state):
            _handle_post_signup_onboarding(page, log)
            final_state = _extract_flow_state(None, page.url)
            final_state["phone_number"] = "+" + phone_number.lstrip("+")
            final_state["register_mode"] = "phone"
            return final_state

        if _is_password_registration(state):
            log("手机号注册：提交账户密码...")
            response = _submit_password_via_page(page, password, log)
            if not response.get("ok"):
                raise RuntimeError(f"手机号注册密码提交失败: {(response.get('text') or '')[:240]}")
            state = _extract_flow_state(response.get("data"), response.get("url", page.url))
            if not state.get("page_type") or _is_password_registration(state):
                state = _derive_registration_state_from_page(page)
            continue

        if str(state.get("page_type") or "") == "phone_otp_verification":
            if not phone_send_marked and hasattr(phone_callback, "mark_send_succeeded"):
                phone_callback.mark_send_succeeded()
                phone_send_marked = True
            code = str(phone_callback() or "").strip()
            if not code:
                raise _PhoneSignupRetryableError("等待短信验证码超时")
            response = _submit_phone_otp_dom(page, code, log)
            if not response.get("ok"):
                error = str(response.get("text") or "短信验证码校验失败")
                if hasattr(phone_callback, "mark_code_failed"):
                    phone_callback.mark_code_failed(error)
                raise _PhoneSignupRetryableError(error[:240])
            if hasattr(phone_callback, "report_success"):
                phone_callback.report_success()
            state = _extract_flow_state(response.get("data"), response.get("url", page.url))
            if not state.get("page_type") or state.get("page_type") == "phone_otp_verification":
                state = _derive_registration_state_from_page(page)
            continue

        if _is_about_you(state):
            log("手机号注册：提交 about_you 信息...")
            response = _submit_about_you_via_page(page, log)
            if not response.get("ok"):
                raise RuntimeError(f"about_you 提交失败: {(response.get('text') or '')[:240]}")
            state = _extract_flow_state(response.get("data"), response.get("url", page.url))
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            continue

        if _requires_registration_navigation(state):
            target_url = _normalize_url(str(state.get("continue_url") or state.get("current_url") or ""), OPENAI_AUTH)
            if not target_url:
                raise RuntimeError("手机号注册缺少 continue_url")
            _goto_with_retry(page, target_url, wait_until="domcontentloaded", timeout=30000, log=log)
            state = _derive_registration_state_from_page(page)
            continue

        state = _derive_registration_state_from_page(page)
        if not state.get("page_type"):
            raise RuntimeError(f"手机号注册遇到未支持页面: {str(page.url)[:160]}")

    raise RuntimeError("手机号注册状态机超出最大步数")


def _browser_phone_registration_flow(page, password: str, phone_callback, log, max_phone_attempts: int = 3) -> dict:
    if not phone_callback:
        raise RuntimeError("手机号注册需要可用的 phone_callback")

    attempts = max(int(max_phone_attempts or 3), 1)
    last_error = ""
    for attempt in range(1, attempts + 1):
        _open_phone_signup_entry(page, log)
        phone_number = str(phone_callback() or "").strip()
        if not phone_number:
            raise RuntimeError("接码平台未返回手机号")
        try:
            state = _submit_signup_phone_identity(page, phone_number, log)
        except Exception as exc:
            raise_error = _PhoneSignupRetryableError(str(exc))
        else:
            try:
                return _complete_phone_signup_attempt(
                    page,
                    password,
                    phone_callback,
                    state,
                    phone_number,
                    log,
                )
            except _PhoneSignupRetryableError as exc:
                raise_error = exc

        last_error = str(raise_error)
        if attempt >= attempts:
            raise RuntimeError(last_error) from raise_error
        log(f"手机号或短信验证不可用，释放号码并重开注册 ({attempt}/{attempts}): {last_error[:180]}")
        _reset_phone_callback_for_retry(phone_callback, last_error)

    raise RuntimeError(last_error or "手机号注册失败")


class ChatGPTBrowserRegister:
    def __init__(
        self,
        *,
        headless: bool,
        proxy: Optional[str] = None,
        otp_callback: Optional[Callable[[], str]] = None,
        phone_callback: Optional[Callable[[], str]] = None,
        log_fn: Callable[[str], None] = print,
        backend_config: Optional[BrowserBackendConfig] = None,
        post_register_in_browser: Optional[Callable[[Any, dict], dict]] = None,
        startup_timeout: int = 45,
    ):
        self.headless = headless
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.phone_callback = phone_callback
        self.log = log_fn
        self.startup_timeout = max(int(startup_timeout or 45), 10)
        # post_register_in_browser(page, session_info) -> dict|None：
        # 注册拿到 session 后、**浏览器还开着**时回调。短链复用流程用它在
        # 同一个浏览器/同一 page 里打开短链并抓 midtrans_url。返回的 dict 会
        # 合并进 run() 的结果（如 {"midtrans_url": "..."}）。回调异常不影响
        # 注册结果本身（只记日志、不抛）。
        self.post_register_in_browser = post_register_in_browser
        # backend_config 为 None 时默认 Camoufox，跟老调用方一致。
        # BitBrowser 路径需要上层 plugin.py 显式传 backend_config。
        self.backend_config = backend_config or BrowserBackendConfig.camoufox(
            headless=bool(headless)
        )
        if self.backend_config.is_bitbrowser:
            log_fn(
                f"ChatGPT 注册使用 BitBrowser backend "
                f"(profile={self.backend_config.bit_profile_id}, "
                f"window_mode={self.backend_config.window_mode})"
            )

    def _open_browser(self, launch_opts: dict):
        """与业务代码代期使用的 ``with Camoufox(**launch_opts) as browser:`` 接口
        保持兑现：按 ``self.backend_config`` 路由到 Camoufox 或 BitBrowser。
        BitBrowser 路径下 launch_opts 里的 proxy/geoip 会被忽略（profile
        自带代理）。"""
        if self.backend_config.is_camoufox:
            patch_playwright_firefox_pageerror_location_bug(log_fn=self.log)
        _apply_camoufox_visible_window_limit(launch_opts, self.backend_config)
        return open_browser_backend(
            launch_opts=launch_opts,
            config=self.backend_config,
            camoufox_class=Camoufox,
            log=self.log,
        )

    def run(self, email: str, password: str, register_mode: str = "email") -> dict:
        register_mode = "phone" if str(register_mode or "").strip().lower() == "phone" else "email"
        if self.backend_config.is_bitbrowser:
            # BitBrowser 路径：profile 已配代理/指纹，launch_opts 不传这些。
            launch_opts = {"headless": self.backend_config.is_headless}
            self.log("准备启动 ChatGPT 浏览器注册流程")
            return self._run_once(email=email, password=password, register_mode=register_mode, launch_opts=launch_opts)

        # GuJumpgate 使用现有 Chrome 标签页执行注册，关键点是避开
        # Camoufox browser.new_page()。桌面端没有扩展消息通道，因此这里用
        # Playwright 驱动系统 Chrome 保持同样的 Chromium 页面环境。
        self.log("准备启动 ChatGPT 浏览器注册流程（优先系统 Chrome）")
        try:
            return self._run_once_chromium(email=email, password=password, register_mode=register_mode)
        except Exception as chrome_exc:
            if register_mode == "phone":
                raise
            self.log(f"系统 Chrome 注册流程失败，回退 Camoufox: {str(chrome_exc)[:240]}")
            proxy = _build_proxy_config(self.proxy)
            launch_opts = {"headless": self.headless}
            if proxy:
                launch_opts["proxy"] = proxy
                launch_opts["geoip"] = True
            return self._run_once(email=email, password=password, register_mode=register_mode, launch_opts=launch_opts)

    def _run_flow_and_collect(self, page, *, email: str, password: str, register_mode: str) -> dict:
        if register_mode == "phone":
            final_state = _browser_phone_registration_flow(
                page,
                password,
                self.phone_callback,
                self.log,
            )
            phone_number = str(final_state.get("phone_number") or "").strip()
            if not phone_number:
                raise RuntimeError("手机号注册完成但未保留手机号标识")
            account_identifier = f"phone:{phone_number}"
        else:
            final_state = _browser_registration_flow(
                page,
                email,
                password,
                self.otp_callback,
                self.phone_callback,
                self.log,
            )
            phone_number = ""
            account_identifier = email
        self.log(f"注册流程完成: page={final_state.get('page_type') or '-'}")

        cookies_dict = _get_cookies(page)
        session_info = _fetch_chatgpt_session_from_page(page, cookies_dict, self.log)
        account_id = str(session_info.get("account_id") or "").strip()
        access_token = str(session_info.get("access_token") or "").strip()
        if not account_id or not access_token:
            raise RuntimeError("注册完成但 session 缺少 account_id 或 accessToken，账号未入库")
        result = {
            "email": account_identifier,
            "password": password,
            "account_id": account_id,
            "access_token": access_token,
            "refresh_token": session_info.get("refresh_token", ""),
            "id_token": session_info.get("id_token", ""),
            "session_token": session_info.get("session_token", ""),
            "workspace_id": session_info.get("workspace_id", ""),
            "cookies": session_info.get("cookies", "") or _cookies_to_header(cookies_dict),
            "profile": session_info.get("profile", {}),
            "expires_at": session_info.get("expires_at", ""),
            "session": session_info.get("session", {}),
            "registration_state": final_state,
            "register_mode": register_mode,
            "phone_number": phone_number,
        }

        if callable(self.post_register_in_browser):
            try:
                self.log("注册完成，浏览器保持打开，继续在同一浏览器里执行后续流程...")
                extra = self.post_register_in_browser(page, dict(result))
                if isinstance(extra, dict):
                    result.update(extra)
            except Exception as exc:
                self.log(f"浏览器内后续流程异常（不影响注册结果）: {exc}")
        return result

    def _run_once(self, *, email: str, password: str, register_mode: str, launch_opts: dict) -> dict:
        backend_name = "bitbrowser" if self.backend_config.is_bitbrowser else "camoufox"
        self.log(f"正在打开浏览器后端: {backend_name}, headless={bool(launch_opts.get('headless'))}")
        browser_cm = self._open_browser(launch_opts)
        entered = False
        exit_exc_info = (None, None, None)
        try:
            browser = browser_cm.__enter__()
            entered = True
            self.log("浏览器后端已打开，准备创建页面")
            page = browser.new_page()
            self.log("启动浏览器上下文注册状态机")
            return self._run_flow_and_collect(
                page,
                email=email,
                password=password,
                register_mode=register_mode,
            )
        except Exception:
            exit_exc_info = sys.exc_info()
            raise
        finally:
            if entered:
                try:
                    browser_cm.__exit__(*exit_exc_info)
                except Exception as close_exc:
                    if exit_exc_info[1] is not None:
                        self.log(f"浏览器关闭异常（已保留原始错误）: {close_exc}")
                    else:
                        self.log(f"浏览器关闭异常（已忽略，注册结果已生成）: {close_exc}")

    def _run_once_chromium(self, *, email: str, password: str, register_mode: str) -> dict:
        from playwright.sync_api import sync_playwright

        proxy = _build_proxy_config(self.proxy)
        launch_opts = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        }
        if proxy:
            launch_opts["proxy"] = proxy

        self.log(f"正在打开浏览器后端: playwright-chromium, headless={bool(launch_opts.get('headless'))}")
        with sync_playwright() as pw:
            browser = None
            context = None
            try:
                try:
                    browser = pw.chromium.launch(channel="chrome", **launch_opts)
                    self.log("Playwright Chromium 使用系统 Chrome 启动成功")
                except Exception as chrome_exc:
                    self.log(f"系统 Chrome 启动失败，改用 Playwright 自带 Chromium: {str(chrome_exc)[:160]}")
                    browser = pw.chromium.launch(**launch_opts)

                context = browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    user_agent=_random_chrome_ua(),
                )
                context.set_default_timeout(90000)
                self.log("Playwright Chromium 上下文已打开，准备创建页面")
                page = context.new_page()
                self.log("启动浏览器上下文注册状态机")
                return self._run_flow_and_collect(
                    page,
                    email=email,
                    password=password,
                    register_mode=register_mode,
                )
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception as exc:
                        self.log(f"Playwright Chromium context 关闭异常: {exc}")
                if browser is not None:
                    try:
                        browser.close()
                    except Exception as exc:
                        self.log(f"Playwright Chromium browser 关闭异常: {exc}")

    def _retry_oauth_fresh_browser(self, email, password):
        """在全新浏览器 context 里做 Codex OAuth（绕过 add_phone session）。"""
        if self.backend_config.is_bitbrowser:
            launch_opts = {"headless": self.backend_config.is_headless}
        else:
            proxy = _build_proxy_config(self.proxy)
            launch_opts = {"headless": self.headless}
            if proxy:
                launch_opts["proxy"] = proxy
        try:
            with self._open_browser(launch_opts) as browser:
                page = browser.new_page()
                self.log("  全新浏览器 OAuth 开始...")
                result = _do_codex_oauth(
                    page, {}, email, password,
                    self.otp_callback, self.phone_callback, self.proxy, self.log,
                )
                return result
        except Exception as e:
            self.log(f"  全新浏览器 OAuth 异常: {e}")
            return None

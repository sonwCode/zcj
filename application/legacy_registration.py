from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import threading
from typing import Any

from core.base_platform import RegisterConfig
from core.db import record_registered_email, save_account
from core.proxy_utils import infer_proxy_region
from core.registry import LEGACY_REGISTRATION_PLATFORMS, get


TASK_TYPE_LEGACY_REGISTER = "legacy_register"

PROXY_STRATEGIES = {"auto", "polling", "mihomo", "direct"}
FAILURE_POLICIES = {"retry_then_continue", "continue", "stop_on_failure"}


def _is_resource_exhaustion(error: str) -> bool:
    text = str(error or "").lower()
    resource_words = ("邮箱", "mailbox", "代理池", "proxy pool", "mihomo")
    exhaustion_words = ("没有可用", "已用完", "耗尽", "库存不足", "未配置", "不存在", "no available", "exhausted", "empty")
    return any(word in text for word in resource_words) and any(word in text for word in exhaustion_words)


def _is_global_sms_pool_exhausted(error: str) -> bool:
    return "SMS_POOL_EXHAUSTED" in str(error or "")


def _is_current_sms_phone_exhausted(error: str) -> bool:
    return "SMS_PHONE_EXHAUSTED" in str(error or "")


def _is_cloudflare_challenge(error: str) -> bool:
    return "cloudflare challenge" in str(error or "").lower()


def _is_terminal_chatgpt_free_failure(error: str) -> bool:
    """Return True for failures where another email/account cannot help.

    These responses describe an OpenAI account/session or registration cohort
    rejection. Retrying the outer legacy task would create more mailboxes and
    consume SMS inventory without changing the rejected state.
    """
    text = str(error or "").strip().lower()
    return any(
        marker in text
        for marker in (
            "phone_risk_rejected",
            "phone_account_rate_limited",
            "account_deactivated",
            "phone_proxy_country_mismatch",
        )
    )


def _is_chatgpt_email_otp_timeout(error: str) -> bool:
    text = str(error or "").lower()
    return any(
        marker in text
        for marker in (
            "等待验证码超时",
            "未收到验证码",
            "未获取到邮箱验证码",
            "email otp timeout",
        )
    )


def _is_remote_email_identity_rejected(error: str) -> bool:
    text = str(error or "").strip().lower()
    return any(marker in text for marker in (
        "user_already_exists",
        "account already exists for this email",
        "email_already_exists",
        "email_account_deactivated",
        "account_deactivated",
    ))


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def create_legacy_register_task(payload: dict[str, Any]) -> dict[str, Any]:
    from application.tasks import create_task

    platform = str(payload.get("platform") or "").strip().lower()
    if platform not in LEGACY_REGISTRATION_PLATFORMS:
        raise ValueError(f"不支持的其他注册平台: {platform or '(empty)'}")
    count = _bounded_int(payload.get("count"), default=1, minimum=1, maximum=100)
    concurrency = _bounded_int(payload.get("concurrency"), default=1, minimum=1, maximum=20)
    executor_type = str(payload.get("executor_type") or "protocol").strip().lower()
    platform_cls = get(platform)
    if executor_type not in list(getattr(platform_cls, "supported_executors", []) or []):
        raise ValueError(f"{platform} 不支持执行器 {executor_type}")
    extra = dict(payload.get("extra") or {})
    identity_provider = str(extra.get("identity_provider") or "mailbox").strip().lower()
    if identity_provider not in list(getattr(platform_cls, "supported_identity_modes", []) or []):
        raise ValueError(f"{platform} 不支持注册方式 {identity_provider}")
    if identity_provider == "mailbox" and not str(extra.get("mail_provider") or "").strip():
        raise ValueError("请选择邮箱服务")
    oauth_provider = str(extra.get("oauth_provider") or "").strip().lower()
    if identity_provider == "oauth_browser":
        if oauth_provider not in list(getattr(platform_cls, "supported_oauth_providers", []) or []):
            raise ValueError(f"{platform} 不支持 OAuth 提供方 {oauth_provider or '(empty)'}")
        if executor_type != "headed":
            raise ValueError(f"{platform} 的 OAuth 注册必须使用 headed 执行器")
        if platform == "tavily" and not (
            str(extra.get("chrome_user_data_dir") or "").strip()
            or str(extra.get("chrome_cdp_url") or "").strip()
        ):
            raise ValueError("Tavily OAuth 需要 Chrome 用户目录或 CDP 地址")
    if count > 1 and str(payload.get("email") or "").strip():
        raise ValueError("批量注册不能固定复用同一个邮箱")
    proxy_strategy = str(extra.get("proxy_strategy") or "auto").strip().lower()
    if proxy_strategy not in PROXY_STRATEGIES:
        raise ValueError(f"未知代理策略: {proxy_strategy}")
    if proxy_strategy == "mihomo":
        from core.mihomo_config import mihomo_config_manager

        if not mihomo_config_manager.config_path.is_file():
            raise ValueError("Mihomo 尚未配置，请先在代理节点页保存订阅，或改用手动代理池/直连")
    failure_policy = str(extra.get("failure_policy") or "retry_then_continue").strip().lower()
    if failure_policy not in FAILURE_POLICIES:
        raise ValueError(f"未知失败策略: {failure_policy}")
    if platform == "chatgpt_free":
        # The isolated Free entrypoint always reuses the legacy
        # email -> add_phone -> Codex PKCE sequence.  Keep these server-side
        # gates authoritative even if an older frontend omits them.
        extra["require_phone_verification"] = True
        extra["require_codex_refresh_token"] = True
        extra["register_mode"] = "email_then_phone"
    extra["proxy_strategy"] = proxy_strategy
    extra["failure_policy"] = failure_policy
    max_attempts = (
        _bounded_int(extra.get("max_attempts_per_account"), default=2, minimum=1, maximum=5)
        if failure_policy == "retry_then_continue"
        else 1
    )
    # The Free flow has three external resources (route, mailbox and SMS).
    # Two attempts are too easy to consume on one no-mail route plus one edge
    # challenge, so make five attempts authoritative for old and new clients.
    if platform == "chatgpt_free" and failure_policy == "retry_then_continue":
        max_attempts = 5
    extra["max_attempts_per_account"] = max_attempts
    normalized = {
        "platform": platform,
        "count": count,
        "concurrency": min(concurrency, count),
        "executor_type": executor_type,
        "captcha_solver": str(payload.get("captcha_solver") or "auto"),
        "email": str(payload.get("email") or "").strip() or None,
        "password": str(payload.get("password") or "") or None,
        "proxy": str(payload.get("proxy") or "").strip() or None,
        "extra": extra,
    }
    return create_task(
        task_type=TASK_TYPE_LEGACY_REGISTER,
        platform=platform,
        payload=normalized,
        progress_total=count,
    )


def execute_legacy_register_task(payload: dict[str, Any], logger) -> None:
    from application.tasks import (
        TASK_STATUS_CANCELLED,
        TASK_STATUS_FAILED,
        TASK_STATUS_SUCCEEDED,
    )
    from core.base_mailbox import create_mailbox
    from core.mihomo_client import MihomoError, mihomo_client
    from core.mihomo_config import mihomo_config_manager
    from core.proxy_pool import proxy_pool

    platform_name = str(payload.get("platform") or "").strip().lower()
    if platform_name not in LEGACY_REGISTRATION_PLATFORMS:
        logger.finish(TASK_STATUS_FAILED, error=f"不支持的其他注册平台: {platform_name}")
        return
    count = _bounded_int(payload.get("count"), default=1, minimum=1, maximum=100)
    concurrency = _bounded_int(payload.get("concurrency"), default=1, minimum=1, maximum=min(count, 20))
    executor_type = str(payload.get("executor_type") or "protocol")
    captcha_solver = str(payload.get("captcha_solver") or "auto")
    requested_email = str(payload.get("email") or "").strip() or None
    requested_password = str(payload.get("password") or "") or None
    explicit_proxy = str(payload.get("proxy") or "").strip() or None
    extra = dict(payload.get("extra") or {})
    identity_provider = str(extra.get("identity_provider") or "mailbox").strip().lower()
    mail_provider = str(extra.get("mail_provider") or "").strip()
    proxy_strategy = str(extra.get("proxy_strategy") or "auto").strip().lower()
    failure_policy = str(extra.get("failure_policy") or "retry_then_continue").strip().lower()
    max_attempts = (
        _bounded_int(extra.get("max_attempts_per_account"), default=2, minimum=1, maximum=5)
        if failure_policy == "retry_then_continue"
        else 1
    )
    # Normalize tasks queued by older frontends before this fix as well.
    if platform_name == "chatgpt_free" and failure_policy == "retry_then_continue":
        max_attempts = 5
    verify_after_registration = bool(extra.get("verify_after_registration", True))
    platform_cls = get(platform_name)
    logger.log(
        f"其他注册工作台启动: platform={platform_name}, count={count}, "
        f"concurrency={concurrency}, executor={executor_type}, proxy={proxy_strategy}, "
        f"failure={failure_policy}, max_attempts={max_attempts}, "
        f"verify={'on' if verify_after_registration else 'off'}"
    )
    logger.set_progress(0, count)
    halt_submissions = threading.Event()

    mihomo_allocator = None
    mihomo_configured = mihomo_config_manager.config_path.is_file()
    if not explicit_proxy and proxy_strategy == "mihomo" and not mihomo_configured:
        logger.finish(TASK_STATUS_FAILED, error="Mihomo 尚未配置，请先保存订阅配置")
        return
    if not explicit_proxy and proxy_strategy in {"auto", "mihomo"} and mihomo_configured:
        try:
            strict_chatgpt_preflight = platform_name == "chatgpt_free" and executor_type == "protocol"
            if strict_chatgpt_preflight:
                logger.log("正在预检 Mihomo 线路对 ChatGPT 协议注册的可访问性...")
            mihomo_allocator = mihomo_client.create_registration_allocator(
                preflight=strict_chatgpt_preflight,
                # A JavaScript challenge may be usable in a real browser, but
                # the Free entrypoint is protocol-only and cannot complete it.
                allow_cloudflare_challenge=not strict_chatgpt_preflight,
            )
            logger.log(
                f"Mihomo 线路池可用: nodes={mihomo_allocator.node_count}, "
                f"slots={mihomo_allocator.slot_count}"
            )
        except MihomoError as exc:
            if proxy_strategy == "mihomo":
                logger.finish(TASK_STATUS_FAILED, error=f"Mihomo 线路不可用: {exc}")
                return
            logger.log(f"Mihomo 不可用，智能分配回退到手动代理池/直连: {exc}", level="warning")
        except Exception as exc:
            if proxy_strategy == "mihomo":
                logger.finish(TASK_STATUS_FAILED, error=f"Mihomo 线路不可用: {exc}")
                return
            logger.log(f"Mihomo 初始化失败，智能分配回退: {exc}", level="warning")

    def resolve_worker_proxy():
        if explicit_proxy:
            return explicit_proxy, None
        if proxy_strategy == "direct":
            return None, None
        if mihomo_allocator is not None:
            lease = mihomo_allocator.acquire()
            return lease.proxy, lease
        proxy = proxy_pool.get_next()
        if proxy_strategy == "polling" and not proxy:
            raise RuntimeError("代理池没有可用代理；请添加代理、改用智能分配或选择直连")
        return proxy, None

    def run_one(attempt_index: int) -> dict[str, Any]:
        if logger.is_cancel_requested():
            return {"cancelled": True}
        logger.set_subtask(f"legacy_attempt_{attempt_index}", f"尝试 #{attempt_index}")
        mailbox = None
        platform = None
        identity_finalized = False
        worker_proxy = None
        proxy_lease = None
        try:
            worker_proxy, proxy_lease = resolve_worker_proxy()
            worker_extra = dict(extra)
            worker_proxy_region = ""
            if worker_proxy:
                # Mihomo and 711Proxy encode the country in the URL, while
                # static pool rows keep it in the database. Prefer the URL so
                # a rotated/pinned route wins, then fall back to row metadata.
                worker_proxy_region = infer_proxy_region(worker_proxy)
                if not worker_proxy_region:
                    worker_proxy_region = proxy_pool.get_region(worker_proxy)
            if worker_proxy_region:
                worker_extra["proxy_country"] = worker_proxy_region
                logger.log(f"注册 worker 代理国家: {worker_proxy_region}")
            if proxy_lease is not None:
                logger.log(
                    f"尝试 #{attempt_index} 固定 Mihomo slot {proxy_lease.slot:02d}: "
                    f"node={proxy_lease.node}"
                )
            if identity_provider == "mailbox":
                mailbox = create_mailbox(
                    provider=mail_provider,
                    extra=worker_extra,
                    proxy=worker_proxy,
                )
            config = RegisterConfig(
                executor_type=executor_type,
                captcha_solver=captcha_solver,
                proxy=worker_proxy,
                extra=worker_extra,
            )
            platform = platform_cls(config=config, mailbox=mailbox)
            platform.set_logger(logger.log)
            platform.set_cancel_checker(logger.is_cancel_requested)
            proxy_rotator = getattr(platform, "set_proxy_rotate_callback", None)
            if proxy_lease is not None and callable(proxy_rotator):
                def rotate_worker_proxy() -> str:
                    old_node = proxy_lease.node
                    rotated_proxy = proxy_lease.rotate()
                    logger.log(
                        f"Mihomo 线路已轮换: {old_node} -> {proxy_lease.node}"
                    )
                    return rotated_proxy

                proxy_rotator(rotate_worker_proxy)
            logger.log(f"尝试 #{attempt_index}/{count * max_attempts} 开始")
            account = platform.register(email=requested_email, password=requested_password)
            if platform_name == "chatgpt_free":
                if executor_type != "protocol":
                    raise RuntimeError("ChatGPT Free 手机接码流程仅支持 protocol 执行器")
                from application.tasks import _complete_required_chatgpt_phone_verification

                _complete_required_chatgpt_phone_verification(
                    platform=platform,
                    account=account,
                    extra=worker_extra,
                    logger=logger,
                    country_offset=max(attempt_index - 1, 0),
                )
                account_extra = dict(getattr(account, "extra", {}) or {})
                if not str(account_extra.get("refresh_token") or "").strip():
                    raise RuntimeError("CODEX_RT_MISSING: ChatGPT Free 手机流程缺少 refresh_token，账号未保存")
                logger.log("ChatGPT Free 手机接码及 Codex AT/RT 完成")
            if logger.is_cancel_requested():
                return {"cancelled": True}
            if str(getattr(account, "platform", "") or "") != platform_name:
                raise RuntimeError("平台注册结果与任务平台不一致")
            if not str(getattr(account, "email", "") or "").strip():
                raise RuntimeError("平台注册结果缺少账号标识")
            if verify_after_registration:
                logger.log(f"注册后验证: {account.email}")
                if not bool(platform.check_valid(account)):
                    raise RuntimeError("注册后账号验证未通过")
            saved = save_account(account)
            record_registered_email(account.platform, account.email)
            committer = getattr(platform, "commit_registration_identity", None)
            if callable(committer):
                committer()
                identity_finalized = True
            if worker_proxy and not explicit_proxy and proxy_lease is None:
                proxy_pool.report_success(worker_proxy)
            if proxy_lease is not None:
                mihomo_allocator.record_success(proxy_lease.node)
            logger.record_success()
            logger.log(f"注册成功: {account.email}")
            return {
                "account_id": int(saved.id or 0),
                "platform": account.platform,
                "email": account.email,
                "attempt": attempt_index,
                "verified": verify_after_registration,
            }
        except Exception as exc:
            error = str(exc).strip() or exc.__class__.__name__
            if platform_name == "chatgpt_free" and _is_remote_email_identity_rejected(error):
                identity = getattr(platform, "_last_identity", None) if platform is not None else None
                mailbox_account = getattr(identity, "mailbox_account", None)
                account_extra = dict(getattr(mailbox_account, "extra", {}) or {})
                exclusion = account_extra.get("_mailbox_registration_excluded")
                if mailbox_account is not None and not isinstance(exclusion, dict):
                    marker = getattr(mailbox, "mark_registration_failure", None)
                    if callable(marker):
                        try:
                            marker(mailbox_account, error)
                        except Exception as mark_exc:
                            logger.log(f"邮箱排除状态写入失败: {mark_exc}", level="warning")
                        account_extra = dict(getattr(mailbox_account, "extra", {}) or {})
                        exclusion = account_extra.get("_mailbox_registration_excluded")
                if isinstance(exclusion, dict):
                    identity_finalized = True
                    logger.log(
                        "OpenAI 已占用该邮箱身份，已从微软邮箱池排除主邮箱及全部 +tag 子地址: "
                        f"{str(exclusion.get('parent_email') or mailbox_account.email)}",
                        level="warning",
                    )
            if worker_proxy and not explicit_proxy and proxy_lease is None:
                proxy_pool.report_fail(worker_proxy)
            if proxy_lease is not None:
                failed_node = proxy_lease.node
                blocked_current_node = False
                if _is_cloudflare_challenge(error):
                    mihomo_allocator.mark_blocked(failed_node)
                    blocked_current_node = True
                    logger.log(
                        f"Mihomo 节点 {failed_node} 触发 Cloudflare 挑战，"
                        "本任务内立即淘汰并在下一次尝试换线",
                        level="warning",
                    )
                elif (
                    platform_name == "chatgpt_free"
                    and _is_chatgpt_email_otp_timeout(error)
                ):
                    no_email_count = mihomo_allocator.record_no_email(failed_node)
                    mihomo_allocator.mark_blocked(failed_node)
                    blocked_current_node = True
                    logger.log(
                        f"Mihomo 节点 {failed_node} 未收到邮箱验证码 "
                        f"(no-email={no_email_count})，本任务内暂停该线路；"
                        "下一次尝试将重新分配邮箱并更换节点",
                        level="warning",
                    )
                if blocked_current_node and mihomo_allocator.all_blocked():
                    halt_submissions.set()
            logger.record_error(error)
            logger.log(f"尝试 #{attempt_index} 失败: {error}", level="error")
            if failure_policy == "stop_on_failure":
                halt_submissions.set()
            elif platform_name == "chatgpt_free" and _is_terminal_chatgpt_free_failure(error):
                halt_submissions.set()
                logger.log(
                    "ChatGPT Free 收到账号/手机号级终止错误，已停止继续创建新邮箱和消耗短信资源",
                    level="warning",
                )
            if _is_global_sms_pool_exhausted(error) or _is_resource_exhaustion(error):
                halt_submissions.set()
            elif _is_current_sms_phone_exhausted(error):
                logger.log(
                    "当前接码号码资源不可用；下一次尝试将轮换国家池并重新租号",
                    level="warning",
                )
            return {"error": error, "attempt": attempt_index}
        finally:
            if platform is not None and not identity_finalized:
                releaser = getattr(platform, "release_registration_identity", None)
                if callable(releaser):
                    try:
                        releaser()
                    except Exception:
                        pass
            if proxy_lease is not None:
                try:
                    proxy_lease.release()
                except Exception:
                    pass
            logger.clear_subtask()

    attempt_budget = count * max_attempts
    attempts_submitted = 0
    attempts_completed = 0
    accounts: list[dict[str, Any]] = []
    errors: list[str] = []
    cancelled = False
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = set()
        while True:
            remaining_successes = count - len(accounts)
            while (
                remaining_successes > len(futures)
                and len(futures) < concurrency
                and attempts_submitted < attempt_budget
                and not halt_submissions.is_set()
                and not logger.is_cancel_requested()
            ):
                attempts_submitted += 1
                futures.add(pool.submit(run_one, attempts_submitted))
            if not futures:
                break
            done, futures = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                attempts_completed += 1
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"error": str(exc).strip() or exc.__class__.__name__}
                if result.get("cancelled"):
                    cancelled = True
                elif result.get("account_id"):
                    accounts.append(result)
                elif result.get("error"):
                    errors.append(str(result["error"]))
                logger.set_progress(min(len(accounts), count), count)
            if len(accounts) >= count:
                break
            if logger.is_cancel_requested():
                cancelled = True
                halt_submissions.set()

    success_count = len(accounts)
    failed_attempts = len(errors)

    logger.set_result_data({
        "registration_engine": "legacy_isolated",
        "platform": platform_name,
        "target_count": count,
        "success_count": success_count,
        "failed_attempts": failed_attempts,
        "attempts_submitted": attempts_submitted,
        "attempts_completed": attempts_completed,
        "target_reached": success_count >= count,
        "accounts": accounts,
        "mihomo_blocked_nodes": (
            mihomo_allocator.banned_nodes() if mihomo_allocator is not None else []
        ),
    })
    if cancelled or logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
    elif success_count >= count:
        logger.finish(TASK_STATUS_SUCCEEDED)
    else:
        reason = "首次失败后已停止投放" if halt_submissions.is_set() else "尝试预算已用尽"
        detail = errors[-1] if errors else "没有成功账号"
        logger.finish(
            TASK_STATUS_FAILED,
            error=f"成功目标未完成: {success_count}/{count}（{reason}；最后错误: {detail}）",
        )

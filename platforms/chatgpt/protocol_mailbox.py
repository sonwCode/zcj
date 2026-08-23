"""ChatGPT 协议邮箱注册 worker。"""

from __future__ import annotations



from typing import Callable



from platforms.chatgpt.register import RegistrationEngine





_SAME_MAILBOX_RETRY_CODES = {"oauth_invalid_state", "wrong_or_expired_otp"}
_PROXY_FAILURE_CODES = {"proxy_network_error", "proxy_or_access_blocked", "unsupported_region"}


class ChatGPTProtocolRegistrationError(RuntimeError):
    def __init__(self, message: str, *, code: str = "registration_failed"):
        super().__init__(message)
        self.code = str(code or "registration_failed")
        self.proxy_failure = self.code in _PROXY_FAILURE_CODES





class _MailboxEmailService:

    def __init__(self, *, mailbox, mailbox_account, provider: str, cancel_check=None):

        self.service_type = type("ST", (), {"value": provider})()

        self._mailbox = mailbox

        self._mailbox_account = mailbox_account

        self._acct = None

        self._before_ids = None

        self._cancel_check = cancel_check if callable(cancel_check) else (lambda: False)



    def _raise_if_cancelled(self) -> None:

        if self._cancel_check():

            raise RuntimeError("任务已取消")



    def create_email(self, config=None):

        self._raise_if_cancelled()

        self._acct = self._mailbox_account

        try:

            self._before_ids = self._mailbox.get_current_ids(self._mailbox_account)

        except Exception:

            self._before_ids = set()

        return {

            "email": self._mailbox_account.email,

            "service_id": getattr(self._mailbox_account, "account_id", ""),

            "token": getattr(self._mailbox_account, "account_id", ""),

        }



    def get_verification_code(self, email=None, email_id=None, timeout=120, pattern=None, otp_sent_at=None):

        import time as _time

        acct = self._acct or self._mailbox_account

        self._raise_if_cancelled()

        mailbox_type = type(self._mailbox).__name__



        # 如果知道 OTP 发送时间，先等邮件投递完成再开始轮询

        effective_timeout = timeout

        if otp_sent_at is not None:

            elapsed = _time.time() - otp_sent_at

            delivery_delay = 8

            if elapsed < delivery_delay:

                wait_remaining = delivery_delay - elapsed

                print(f"[Mailbox:{mailbox_type}] OTP 发送 {elapsed:.0f}s 前，等待 {wait_remaining:.0f}s 后开始轮询（让邮件到达）")

                deadline = _time.time() + wait_remaining

                while _time.time() < deadline:

                    self._raise_if_cancelled()

                    _time.sleep(min(0.5, max(deadline - _time.time(), 0)))

                effective_timeout = max(30, timeout - int(wait_remaining))



        before_count = len(self._before_ids) if self._before_ids else 0

        print(f"[Mailbox:{mailbox_type}] 开始等待验证码 email={acct.email} timeout={effective_timeout}s before_ids={before_count}")



        deadline = _time.time() + effective_timeout

        while _time.time() < deadline:

            self._raise_if_cancelled()

            slice_timeout = max(1, min(5, int(deadline - _time.time()) or 1))

            try:

                code = self._mailbox.wait_for_code(

                    acct, keyword="", timeout=slice_timeout,

                    code_pattern=pattern,

                    before_ids=self._before_ids or None,

                )

                print(f"[Mailbox:{mailbox_type}] 轮询成功，获取到验证码")

                return code

            except TimeoutError:

                continue

        print(f"[Mailbox:{mailbox_type}] 轮询超时 ({effective_timeout}s)，未收到验证码")

        raise TimeoutError(f"等待验证码超时 ({effective_timeout}s)")

    def begin_new_otp_wait(self) -> None:
        """Reset the message baseline before a second auth transaction."""
        self._raise_if_cancelled()
        try:
            self._before_ids = self._mailbox.get_current_ids(self._mailbox_account)
        except Exception:
            self._before_ids = set()



    def update_status(self, success, error=None):

        return None



    @property

    def status(self):

        return None





class ChatGPTProtocolMailboxWorker:

    def __init__(

        self,

        *,

        mailbox,

        mailbox_account,

        provider: str,

        proxy_url: str | None = None,

        proxy_country: str = "",

        log_fn: Callable[[str], None] = print,

        cancel_check=None,

    ):

        if not mailbox or not mailbox_account:

            raise ValueError("ChatGPT 注册流程依赖 mailbox provider，当前未获取到邮箱账号")

        self.mailbox = mailbox

        self.mailbox_account = mailbox_account

        self.proxy_url = proxy_url

        self.proxy_country = str(proxy_country or "").strip().upper()

        self.log_fn = log_fn

        self.cancel_check = cancel_check if callable(cancel_check) else (lambda: False)

        self.email_service = _MailboxEmailService(

            mailbox=mailbox,

            mailbox_account=mailbox_account,

            provider=provider,

            cancel_check=self.cancel_check,

        )

        self.engine = self._new_engine()


    def _new_engine(self) -> RegistrationEngine:
        engine = RegistrationEngine(
            email_service=self.email_service,
            proxy_url=self.proxy_url,
            callback_logger=self.log_fn,
            preflight_location=self.proxy_country,
        )
        # The passwordless authorize request owns the one-time state through
        # OTP validation. A retry therefore creates a completely new engine.
        engine.email_otp_first = True
        engine.otp_submit_delay = 2.0
        return engine



    def _log(self, message: str) -> None:

        try:

            self.log_fn(message)

        except Exception:

            pass



    def run(self, *, email: str, password: str):
        for attempt in range(1, 3):
            if self.cancel_check():
                raise RuntimeError("任务已取消")
            if attempt > 1:
                self.engine = self._new_engine()
            self.engine.email = email
            self.engine.password = password
            result = self.engine.run()
            if self.cancel_check():
                raise RuntimeError("任务已取消")
            if result and result.success:
                return result

            code = str(getattr(result, "error_code", "") or "registration_failed")
            message = str(getattr(result, "error_message", "") or "注册失败")
            if attempt == 1 and code in _SAME_MAILBOX_RETRY_CODES:
                self._log(f"协议会话失败: code={code}，同邮箱重建会话后重试一次")
                continue
            raise ChatGPTProtocolRegistrationError(message, code=code)

        raise ChatGPTProtocolRegistrationError("注册失败", code="registration_failed")


from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool

from core.local_ms_mailbox import LocalMicrosoftMailboxPool, parse_local_ms_pool_rows
from infrastructure.microsoft_mailbox_repository import MicrosoftMailboxRepository


router = APIRouter(prefix="/microsoft-mailboxes", tags=["microsoft-mailboxes"])
repository = MicrosoftMailboxRepository()

MAX_IMPORT_BYTES = 100 * 1024 * 1024
MAX_IMPORT_FILES = 100


def _decode_text(payload: bytes, filename: str) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise HTTPException(400, f"TXT 文件编码无法识别: {filename}")


@router.get("/stats")
def mailbox_stats():
    return repository.stats()


@router.get("/{email}/messages")
async def mailbox_messages(email: str, limit: int = Query(default=20, ge=1, le=50)):
    """Read the latest inbox messages for a Microsoft mailbox (Graph or IMAP).

    Accepts either the parent mailbox address or one of its split sub-addresses
    (``xxx+sub-1@outlook.com``) — sub-addresses resolve to the parent mailbox.
    """
    import urllib.parse

    decoded = urllib.parse.unquote(email.strip())
    pool = LocalMicrosoftMailboxPool()
    parent_key = pool._parent_email_key(decoded)
    record = await run_in_threadpool(repository.get_by_parent_email, parent_key)
    if record is None:
        raise HTTPException(404, f"未找到邮箱 {decoded}")
    entry = pool._entry_from_record(record)
    try:
        if entry.graph_ready:
            messages = await run_in_threadpool(pool._graph_messages, entry, limit=limit)
            source = "graph"
        elif entry.imap_ready:
            messages = await run_in_threadpool(pool._imap_messages, entry, limit=limit)
            source = "imap"
        else:
            raise HTTPException(
                400,
                f"邮箱 {decoded} 没有可用的 Graph token，也没有 IMAP 收件配置",
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"读取邮件失败: {str(exc)[:200]}") from exc
    return {
        "email": decoded,
        "parent_email": record.email,
        "source": source,
        "status": record.status,
        "use_count": record.use_count,
        "max_uses": record.max_uses,
        "messages": messages,
    }


@router.get("")
def list_mailboxes(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status: str = Query(default=""),
    search: str = Query(default="", max_length=200),
):
    normalized_status = status.strip().lower()
    if normalized_status and normalized_status not in {"available", "exhausted", "disabled"}:
        raise HTTPException(400, "邮箱状态无效")
    return repository.list_page(
        page=page,
        page_size=page_size,
        status=normalized_status,
        search=search,
    )


@router.post("/import")
async def import_mailboxes(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(400, "请选择至少一个 TXT 文件")
    if len(files) > MAX_IMPORT_FILES:
        raise HTTPException(400, f"单次最多导入 {MAX_IMPORT_FILES} 个文件")

    entries_by_email: dict[str, object] = {}
    total_bytes = 0
    nonempty_lines = 0
    parsed_rows = 0
    for upload in files:
        remaining_bytes = MAX_IMPORT_BYTES - total_bytes
        payload = await upload.read(remaining_bytes + 1)
        total_bytes += len(payload)
        if total_bytes > MAX_IMPORT_BYTES:
            raise HTTPException(413, "单次导入文件总大小不能超过 100 MB")
        text = _decode_text(payload, upload.filename or "mailboxes.txt")
        nonempty_lines += sum(
            1
            for line in text.splitlines()
            if line.strip()
            and not line.lstrip().startswith(("#", "//", "'"))
        )
        parsed = parse_local_ms_pool_rows(text)
        parsed_rows += len(parsed)
        for entry in parsed:
            entries_by_email[entry.key] = entry

    if not entries_by_email:
        raise HTTPException(400, "TXT 文件中没有解析到有效的微软邮箱")

    result = await run_in_threadpool(
        repository.import_entries,
        entries_by_email.values(),
        max_uses=6,
    )
    return {
        "ok": True,
        "files": len(files),
        "bytes": total_bytes,
        "lines": nonempty_lines,
        "parsed": parsed_rows,
        "unique": len(entries_by_email),
        "invalid": max(nonempty_lines - parsed_rows, 0),
        "duplicates_in_upload": max(parsed_rows - len(entries_by_email), 0),
        **result,
    }

"""Self-hosted catch-all IMAP mailbox provider registration."""

from core.domain_imap_mailbox import DomainImapCatchallMailbox
from providers.registry import register_provider


register_provider("mailbox", "domain_imap_catchall")(DomainImapCatchallMailbox)

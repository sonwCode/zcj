"""Inbucket domain-mailbox provider registration."""

from core.inbucket_domain_mailbox import InbucketDomainMailbox
from providers.registry import register_provider


register_provider("mailbox", "domain_inbucket")(InbucketDomainMailbox)

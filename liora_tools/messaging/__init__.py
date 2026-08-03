"""Template-first outbound Weave SMS + optional sibling messaging primitives."""

from __future__ import annotations

from liora_tools.messaging.idempotency import IdempotencyStore, make_idempotency_key
from liora_tools.messaging.outbound import (
    OutboundSmsSender,
    SendResult,
    env_sms_dry_run,
    env_sms_go_live,
    env_sms_staged_mock,
)
from liora_tools.messaging.phi import (
    mask_email,
    mask_name,
    mask_phone,
    redact_error,
    summarize_for_log,
)
from liora_tools.messaging.templates import (
    TemplateSpec,
    get_template,
    list_routes,
    render_template,
)

__all__ = [
    "IdempotencyStore",
    "OutboundSmsSender",
    "SendResult",
    "TemplateSpec",
    "env_sms_dry_run",
    "env_sms_go_live",
    "env_sms_staged_mock",
    "get_template",
    "list_routes",
    "make_idempotency_key",
    "mask_email",
    "mask_name",
    "mask_phone",
    "redact_error",
    "render_template",
    "summarize_for_log",
]

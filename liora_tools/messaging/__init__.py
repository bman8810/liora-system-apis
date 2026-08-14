"""Weave messaging worker primitives (classify/route, shared shapes).

Inbound poll + outbound send live in sibling modules; this package owns the
pure route decision surface so handlers stay decoupled from Weave I/O.

Optional sibling modules (``templates``, ``phi``, ``idempotency``, ``outbound``)
may land in this package from parallel cards — import them directly when present.
"""

from __future__ import annotations

from liora_tools.messaging.classify import (
    DEFAULT_ROUTES,
    HANDLER_ESCALATE,
    HANDLER_REFILL,
    HANDLER_SCHEDULE,
    HANDLER_ZOCDOC_NP,
    ROUTE_ESCALATE,
    ROUTE_REFILL,
    ROUTE_SCHEDULE,
    ROUTE_ZOCDOC_NP,
    RouteDecision,
    classify_inbound,
    decision_log_dict,
)
from liora_tools.messaging.types import NormalizedInboundMessage

__all__ = [
    "DEFAULT_ROUTES",
    "HANDLER_ESCALATE",
    "HANDLER_REFILL",
    "HANDLER_SCHEDULE",
    "HANDLER_ZOCDOC_NP",
    "NormalizedInboundMessage",
    "ROUTE_ESCALATE",
    "ROUTE_REFILL",
    "ROUTE_SCHEDULE",
    "ROUTE_ZOCDOC_NP",
    "RouteDecision",
    "classify_inbound",
    "decision_log_dict",
]

try:
    from liora_tools.messaging.idempotency import (  # noqa: F401
        IdempotencyStore,
        make_idempotency_key,
    )
    from liora_tools.messaging.outbound import (  # noqa: F401
        OutboundSmsSender,
        SendResult,
        env_sms_dry_run,
        env_sms_go_live,
        env_sms_staged_mock,
    )
    from liora_tools.messaging.phi import (  # noqa: F401
        mask_email,
        mask_name,
        mask_phone,
        redact_error,
        summarize_for_log,
    )
    from liora_tools.messaging.templates import (  # noqa: F401
        TemplateSpec,
        get_template,
        list_routes,
        render_template,
    )

    __all__ += [
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
except ImportError:  # pragma: no cover
    pass

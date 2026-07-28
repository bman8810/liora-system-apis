"""Template-first outbound Weave SMS sender.

Dry-run by default. Live Weave ``send_message`` only when
``LIORA_SMS_GO_LIVE`` is truthy AND ``LIORA_SMS_DRY_RUN`` is false.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from liora_tools.messaging.idempotency import IdempotencyStore, make_idempotency_key
from liora_tools.messaging.phi import mask_phone, redact_error, summarize_for_log
from liora_tools.messaging.templates import TemplateSpec, get_template, render_template
from liora_tools.utils import normalize_phone_e164

log = logging.getLogger("liora_tools.messaging.outbound")

# Status values for SendResult
STATUS_DRY_RUN = "dry_run"
STATUS_STAGED_MOCK = "staged_mock"
STATUS_SENT = "sent"
STATUS_SKIPPED_IDEMPOTENT = "skipped_idempotent"
STATUS_BLOCKED = "blocked_no_go_live"
STATUS_ESCALATE = "escalate_to_staff"
STATUS_ERROR = "error"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str, default: str = "false") -> bool:
    """Parse a boolean env flag (1/true/yes/on)."""
    raw = os.environ.get(name, default)
    if raw is None:
        raw = default
    return str(raw).strip().lower() in _TRUTHY


def env_sms_dry_run() -> bool:
    """LIORA_SMS_DRY_RUN — default true (safe)."""
    return _env_flag("LIORA_SMS_DRY_RUN", "true")


def env_sms_go_live() -> bool:
    """LIORA_SMS_GO_LIVE — default false (hard gate)."""
    return _env_flag("LIORA_SMS_GO_LIVE", "false")


def env_sms_staged_mock() -> bool:
    """LIORA_SMS_STAGED_MOCK — default false."""
    return _env_flag("LIORA_SMS_STAGED_MOCK", "false")


@dataclass
class SendResult:
    """Outbound send outcome (asdict-friendly, no PHI body)."""

    status: str
    mode: str
    route: str | None = None
    template_id: str | None = None
    template_name: str | None = None
    template_version: str | None = None
    idempotency_key: str | None = None
    correlation_id: str | None = None
    weave_ids: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    phone_masked: str = "(none)"
    body_len: int | None = None
    draft_preview_redacted: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class OutboundSmsSender:
    """Template-first Weave SMS sender with dry-run default and idempotency."""

    def __init__(
        self,
        weave_client: Any = None,
        store: IdempotencyStore | None = None,
        dry_run: bool | None = None,
        go_live: bool | None = None,
        staged_mock: bool | None = None,
    ):
        self.weave = weave_client
        self.store = store if store is not None else IdempotencyStore()
        self.dry_run = env_sms_dry_run() if dry_run is None else bool(dry_run)
        self.go_live = env_sms_go_live() if go_live is None else bool(go_live)
        self.staged_mock = (
            env_sms_staged_mock() if staged_mock is None else bool(staged_mock)
        )

    def _resolve_mode(self) -> str:
        """Resolve send mode: dry_run | staged_mock | blocked_no_go_live | live."""
        if self.dry_run:
            return "dry_run"
        if not self.go_live:
            if self.staged_mock:
                return "staged_mock"
            return "blocked_no_go_live"
        return "live"

    def send(
        self,
        *,
        route: str,
        vars: Mapping[str, str] | None = None,
        phone: str,
        thread_id: str | None = None,
        message_id: str | None = None,
        correlation_id: str | None = None,
        person_id: str | None = None,
        allow_ai_draft: bool = False,
        ai_draft_fn: Callable[..., str] | None = None,
        phi_safe_for_ai: bool = False,
    ) -> SendResult:
        """Send (or dry-run / mock / block) a template SMS.

        Template miss never sends free-form. AI draft never auto-sends.
        """
        phone_masked = mask_phone(phone)
        mode = self._resolve_mode()
        corr = (correlation_id or "").strip() or None
        route_key = (route or "").strip()

        try:
            phone_e164 = normalize_phone_e164(phone)
        except Exception as e:
            result = SendResult(
                status=STATUS_ERROR,
                mode=mode,
                route=route_key or None,
                correlation_id=corr,
                reason=f"invalid_phone: {redact_error(e)}",
                phone_masked=phone_masked,
            )
            self._log_result(result)
            return result

        phone_masked = mask_phone(phone_e164)
        spec = get_template(route_key)

        # ── Template miss: never send free-form ────────────────────────────
        if spec is None:
            return self._template_miss(
                route=route_key,
                mode=mode,
                corr=corr,
                phone_masked=phone_masked,
                allow_ai_draft=allow_ai_draft,
                ai_draft_fn=ai_draft_fn,
                phi_safe_for_ai=phi_safe_for_ai,
            )

        # ── Render approved template ───────────────────────────────────────
        try:
            body = render_template(spec, dict(vars or {}))
        except Exception as e:
            result = SendResult(
                status=STATUS_ERROR,
                mode=mode,
                route=spec.route,
                template_id=spec.template_id,
                template_name=spec.template_name,
                template_version=spec.version,
                correlation_id=corr,
                reason=f"render_failed: {redact_error(e)}",
                phone_masked=phone_masked,
            )
            self._log_result(result)
            return result

        idem_key = make_idempotency_key(
            route=spec.route,
            template_id=spec.template_id,
            template_version=spec.version,
            thread_id=thread_id,
            message_id=message_id,
            correlation_id=corr,
            person_phone_e164=phone_e164,
        )

        if self.store.seen(idem_key):
            prior = self.store.get(idem_key) or {}
            weave_ids = {}
            for k in ("smsId", "threadId", "personId"):
                if prior.get(k) is not None:
                    weave_ids[k] = prior[k]
            result = SendResult(
                status=STATUS_SKIPPED_IDEMPOTENT,
                mode=mode,
                route=spec.route,
                template_id=spec.template_id,
                template_name=spec.template_name,
                template_version=spec.version,
                idempotency_key=idem_key,
                correlation_id=corr,
                weave_ids=weave_ids,
                reason="already_sent",
                phone_masked=phone_masked,
                body_len=len(body),
            )
            self._log_result(result)
            return result

        base_meta = {
            "route": spec.route,
            "template_id": spec.template_id,
            "template_name": spec.template_name,
            "template_version": spec.version,
            "idempotency_key": idem_key,
            "correlation_id": corr,
            "phone_masked": phone_masked,
            "body_len": len(body),
        }

        # ── dry_run: check idempotency only; do not mark sent ─────────────
        if mode == "dry_run":
            result = SendResult(
                status=STATUS_DRY_RUN,
                mode=mode,
                route=spec.route,
                template_id=spec.template_id,
                template_name=spec.template_name,
                template_version=spec.version,
                idempotency_key=idem_key,
                correlation_id=corr,
                reason="dry_run_no_send",
                phone_masked=phone_masked,
                body_len=len(body),
            )
            self._log_result(result)
            return result

        # ── blocked without go_live ────────────────────────────────────────
        if mode == "blocked_no_go_live":
            result = SendResult(
                status=STATUS_BLOCKED,
                mode=mode,
                route=spec.route,
                template_id=spec.template_id,
                template_name=spec.template_name,
                template_version=spec.version,
                idempotency_key=idem_key,
                correlation_id=corr,
                reason="go_live_false",
                phone_masked=phone_masked,
                body_len=len(body),
            )
            self._log_result(result)
            return result

        # ── staged_mock: pretend success, record sent ─────────────────────
        if mode == "staged_mock":
            fake_ids = {
                "smsId": f"mock-sms-{idem_key[:12]}",
                "threadId": thread_id or f"mock-thread-{idem_key[:8]}",
            }
            if person_id:
                fake_ids["personId"] = person_id
            self._record_sent(idem_key, base_meta, fake_ids, mode=mode)
            result = SendResult(
                status=STATUS_STAGED_MOCK,
                mode=mode,
                route=spec.route,
                template_id=spec.template_id,
                template_name=spec.template_name,
                template_version=spec.version,
                idempotency_key=idem_key,
                correlation_id=corr,
                weave_ids=fake_ids,
                reason="staged_mock_success",
                phone_masked=phone_masked,
                body_len=len(body),
            )
            self._log_result(result)
            return result

        # ── live Weave send ────────────────────────────────────────────────
        return self._live_send(
            spec=spec,
            body=body,
            phone_e164=phone_e164,
            phone_masked=phone_masked,
            person_id=person_id,
            correlation_id=corr,
            idem_key=idem_key,
            base_meta=base_meta,
            mode=mode,
        )

    def _template_miss(
        self,
        *,
        route: str,
        mode: str,
        corr: str | None,
        phone_masked: str,
        allow_ai_draft: bool,
        ai_draft_fn: Callable[..., str] | None,
        phi_safe_for_ai: bool,
    ) -> SendResult:
        """Never send on unknown route. Optionally attach redacted draft meta."""
        draft_meta: str | None = None
        reason = "template_miss"
        if allow_ai_draft and phi_safe_for_ai and callable(ai_draft_fn):
            try:
                # Only non-PHI context to the draft fn
                draft = ai_draft_fn(
                    route=route,
                    intent_labels=["template_miss", "staff_review"],
                )
                text = draft if isinstance(draft, str) else str(draft or "")
                # Never return body — length + short hash only
                h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
                draft_meta = f"len={len(text)} hash={h}"
                reason = "template_miss_ai_draft_suggested"
            except Exception as e:
                reason = f"template_miss_ai_draft_error: {redact_error(e)}"
        elif allow_ai_draft and not phi_safe_for_ai:
            reason = "template_miss_not_phi_safe"
        result = SendResult(
            status=STATUS_ESCALATE,
            mode=mode,
            route=route or None,
            correlation_id=corr,
            reason=reason,
            phone_masked=phone_masked,
            draft_preview_redacted=draft_meta,
        )
        self._log_result(result)
        return result

    def _live_send(
        self,
        *,
        spec: TemplateSpec,
        body: str,
        phone_e164: str,
        phone_masked: str,
        person_id: str | None,
        correlation_id: str | None,
        idem_key: str,
        base_meta: dict[str, Any],
        mode: str,
    ) -> SendResult:
        if self.weave is None:
            result = SendResult(
                status=STATUS_ERROR,
                mode=mode,
                route=spec.route,
                template_id=spec.template_id,
                template_name=spec.template_name,
                template_version=spec.version,
                idempotency_key=idem_key,
                correlation_id=correlation_id,
                reason="weave_client_missing",
                phone_masked=phone_masked,
                body_len=len(body),
            )
            self._log_result(result)
            return result
        try:
            resp = self.weave.send_message(
                phone_e164,
                body,
                person_id=person_id,
                correlation_id=correlation_id,
            )
        except Exception as e:
            result = SendResult(
                status=STATUS_ERROR,
                mode=mode,
                route=spec.route,
                template_id=spec.template_id,
                template_name=spec.template_name,
                template_version=spec.version,
                idempotency_key=idem_key,
                correlation_id=correlation_id,
                reason=f"send_failed: {redact_error(e)}",
                phone_masked=phone_masked,
                body_len=len(body),
            )
            self._log_result(result)
            return result

        weave_ids = _extract_weave_ids(resp, person_id=person_id)
        self._record_sent(idem_key, base_meta, weave_ids, mode=mode)
        result = SendResult(
            status=STATUS_SENT,
            mode=mode,
            route=spec.route,
            template_id=spec.template_id,
            template_name=spec.template_name,
            template_version=spec.version,
            idempotency_key=idem_key,
            correlation_id=correlation_id,
            weave_ids=weave_ids,
            reason="sent",
            phone_masked=phone_masked,
            body_len=len(body),
        )
        self._log_result(result)
        return result

    def _record_sent(
        self,
        key: str,
        base_meta: Mapping[str, Any],
        weave_ids: Mapping[str, Any],
        *,
        mode: str,
    ) -> None:
        meta = summarize_for_log({**dict(base_meta), **dict(weave_ids), "mode": mode, "status": "sent"})
        self.store.record(key, meta)

    def _log_result(self, result: SendResult) -> None:
        payload = summarize_for_log(result.as_dict())
        log.info("outbound_sms %s", payload)


def _extract_weave_ids(
    resp: Any,
    *,
    person_id: str | None = None,
) -> dict[str, Any]:
    """Pull smsId / threadId / personId from Weave response if present."""
    out: dict[str, Any] = {}
    if not isinstance(resp, Mapping):
        if person_id:
            out["personId"] = person_id
        return out
    # Common shapes: top-level or nested under data
    candidates = [resp]
    data = resp.get("data")
    if isinstance(data, Mapping):
        candidates.append(data)
    for src in candidates:
        for key in ("smsId", "threadId", "personId", "id"):
            if src.get(key) is not None:
                mapped = "smsId" if key == "id" and "smsId" not in out else key
                if mapped == "id":
                    mapped = "smsId"
                out.setdefault(mapped if key != "id" else "smsId", src[key])
    if person_id and "personId" not in out:
        out["personId"] = person_id
    return out

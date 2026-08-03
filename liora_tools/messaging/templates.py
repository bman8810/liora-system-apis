"""Approved SMS template registry + render (template-first, no AI).

Mirrors Zocdoc NP constants from ``zocdoc_new_booking`` so the outbound
sender and the production job stay aligned without coupling the job to this
module yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

# ── Zocdoc new-patient (same SoT as zocdoc_new_booking) ─────────────────────

ZOCDOC_NP_ROUTE = "zocdoc_new_patient"
ZOCDOC_NP_TEMPLATE_ID = "00914ffc-ae68-49c8-a76d-a0d78a5d5d21"
ZOCDOC_NP_TEMPLATE_NAME = "Genie - New Zocdoc Patient"
ZOCDOC_NP_VERSION = "1"
ZOCDOC_NP_ALLOWED_VARS = frozenset({"FIRST_NAME"})
ZOCDOC_NP_FINGERPRINT = "booking cost of $100"
ZOCDOC_NP_BODY = (
    "Hello {{FIRST_NAME}} ,\n"
    "\n"
    "Thanks for scheduling with us at Liora.\n"
    "\n"
    "In order to confirm your appointment, please log into the portal "
    "(link just sent) and complete the registration, including adding a "
    "credit card on file (securely encrypted).\n"
    "\n"
    "Because appointments scheduled through Zocdoc reserve dedicated provider "
    "time and incur a booking cost of $100 to the practice, we require all new "
    "patients to complete registration and maintain a card on file prior to "
    "confirming the visit. If the registration is not completed, we may need "
    "to release the appointment so it can be offered to another patient in "
    "need of care.\n"
    "\n"
    "Please let us know if you need the portal link resent or if we can assist "
    "you in any way.\n"
    "\n"
    "We look forward to hearing from you soon!"
)


@dataclass(frozen=True)
class TemplateSpec:
    """Approved outbound SMS template definition."""

    route: str
    template_id: str
    template_name: str
    version: str
    body: str
    allowed_vars: frozenset[str]
    fingerprint: str
    description: str = ""


_REGISTRY: dict[str, TemplateSpec] = {}


def register_template(spec: TemplateSpec) -> TemplateSpec:
    """Validate and register a template. Returns the validated spec."""
    validate_template(spec)
    _REGISTRY[spec.route] = spec
    return spec


def get_template(route: str) -> TemplateSpec | None:
    """Return registered template for route, or None."""
    if not route:
        return None
    return _REGISTRY.get(str(route).strip())


def list_routes() -> list[str]:
    """Sorted list of registered route keys."""
    return sorted(_REGISTRY.keys())


def validate_template(
    spec: TemplateSpec | None = None,
    *,
    body: str | None = None,
    allowed_vars: frozenset[str] | None = None,
    fingerprint: str | None = None,
) -> str:
    """Validate template body against fingerprint + allowed vars.

    Accepts a ``TemplateSpec`` or explicit body/allowed_vars/fingerprint.
    Returns the stripped body or raises ValueError (no PHI in message).
    """
    if spec is not None:
        body = spec.body
        allowed_vars = spec.allowed_vars
        fingerprint = spec.fingerprint
    text = (body or "").strip()
    if not text:
        raise ValueError("sms template empty")
    fp = (fingerprint or "").strip()
    if not fp:
        raise ValueError("sms template missing required fingerprint")
    if fp not in text:
        raise ValueError("sms template missing required fingerprint")
    allowed = allowed_vars if allowed_vars is not None else frozenset()
    vars_found = _TEMPLATE_VAR_RE.findall(text)
    unknown = sorted({v for v in vars_found if v not in allowed})
    if unknown:
        raise ValueError(f"sms template has disallowed vars: {', '.join(unknown)}")
    # Required vars: every allowed var must appear (callers declare what they need)
    missing = sorted(v for v in allowed if v not in vars_found)
    if missing:
        raise ValueError(
            "sms template missing required vars: "
            + ", ".join(f"{{{{{v}}}}}" for v in missing)
        )
    return text


def render_template(spec: TemplateSpec, vars: Mapping[str, str]) -> str:
    """Substitute ``{{VAR}}`` / ``{{ VAR }}`` from *vars*.

    Refuses unsubstituted placeholders, lost fingerprint, empty body, or
    free-form bypass (body always comes from the approved spec).
    """
    if spec is None:
        raise ValueError("template spec required")
    body = validate_template(spec)
    rendered = body
    for key, value in (vars or {}).items():
        name = str(key).strip()
        if name not in spec.allowed_vars:
            raise ValueError(f"disallowed template var: {name}")
        # Empty allowed vars fall back to a neutral placeholder (match job)
        sub = (str(value) if value is not None else "").strip() or "there"
        rendered = rendered.replace("{{" + name + "}}", sub)
        rendered = re.sub(
            r"\{\{\s*" + re.escape(name) + r"\s*\}\}",
            sub,
            rendered,
        )
    if "{{" in rendered or "}}" in rendered:
        raise RuntimeError("refusing to send unsubstituted SMS template vars")
    if spec.fingerprint not in rendered:
        raise RuntimeError("refusing to send non-template SMS (fingerprint lost)")
    if not rendered.strip():
        raise RuntimeError("refusing to send empty SMS body")
    return rendered


# Register built-in NP template at import time.
register_template(
    TemplateSpec(
        route=ZOCDOC_NP_ROUTE,
        template_id=ZOCDOC_NP_TEMPLATE_ID,
        template_name=ZOCDOC_NP_TEMPLATE_NAME,
        version=ZOCDOC_NP_VERSION,
        body=ZOCDOC_NP_BODY,
        allowed_vars=ZOCDOC_NP_ALLOWED_VARS,
        fingerprint=ZOCDOC_NP_FINGERPRINT,
        description="Genie new Zocdoc patient welcome / registration SMS",
    )
)

"""Unit tests for static clinic FAQ (hours / address / parking)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from voice_agent.clinic_facts import (
    FAQ_TOPICS,
    clear_facts_cache,
    load_clinic_facts,
    topic_payload,
)
from voice_agent.faq_tools import (
    FAQ_TOOL_DEFINITIONS,
    FAQ_TOOL_NAMES,
    clinic_faq,
    handle_faq_tool,
)


@pytest.fixture(autouse=True)
def _reset_facts_cache(monkeypatch):
    # Prefer packaged JSON; clear env override between tests
    monkeypatch.delenv("LIORA_CLINIC_FACTS_PATH", raising=False)
    clear_facts_cache()
    yield
    clear_facts_cache()


def _parse(s: str) -> dict:
    return json.loads(s)


def test_tool_schema_present():
    assert "clinic_faq" in FAQ_TOOL_NAMES
    names = [t["name"] for t in FAQ_TOOL_DEFINITIONS]
    assert names == ["clinic_faq"]
    props = FAQ_TOOL_DEFINITIONS[0]["parameters"]["properties"]
    assert "topic" in props
    assert "dry_run" in props


def test_hours_address_parking_covered():
    for topic in ("hours", "address", "parking"):
        data = _parse(clinic_faq({"topic": topic}))
        assert data["status"] == "ok"
        assert data["topic"] == topic
        assert data["speak"]
        assert data["message"]
        assert data["writes_performed"] is False
        assert data["read_only"] is True


def test_hours_content_matches_config():
    facts = load_clinic_facts()
    data = _parse(clinic_faq({"topic": "hours"}))
    assert data["hours"]["Mon-Thu"] == facts["hours"]["Mon-Thu"]
    assert "9" in data["speak"] and "6" in data["speak"]
    assert "Friday" in data["speak"] or "friday" in data["speak"].lower()


def test_address_is_60th_suite_800():
    data = _parse(clinic_faq({"topic": "address"}))
    speak = data["speak"]
    assert "60th" in speak
    assert "800" in speak
    assert "10022" in speak or "New York" in speak


def test_parking_no_false_garage_name():
    data = _parse(clinic_faq({"topic": "parking"}))
    speak = data["speak"].lower()
    assert "parking" in speak
    # Do not invent a branded garage
    for banned in ("icon parking", "spothero membership", "free valet"):
        assert banned not in speak


def test_all_topic_combines_three_only():
    data = _parse(clinic_faq({"topic": "all"}))
    assert data["status"] == "ok"
    assert data["topic"] == "all"
    speak = data["speak"].lower()
    assert "60th" in speak
    assert "parking" in speak
    # No clinical / insurance / results leakage keys
    for banned in (
        "clinical",
        "diagnosis",
        "lab_results",
        "results",
        "insurance",
        "eligibility",
        "balance",
        "copay",
        "card_number",
        "pan",
    ):
        assert banned not in data


def test_unknown_topic_safe():
    data = _parse(clinic_faq({"topic": "copay"}))
    assert data["status"] == "unknown_topic"
    assert set(data["allowed_topics"]) == set(FAQ_TOPICS)
    assert data["writes_performed"] is False
    assert "hours" in data["speak"].lower() or "address" in data["speak"].lower()


def test_dry_run_uniform_contract():
    live = _parse(clinic_faq({"topic": "hours", "dry_run": False}))
    dry = _parse(clinic_faq({"topic": "hours", "dry_run": True}))
    assert dry["dry_run"] is True
    assert dry["writes_performed"] is False
    assert live["writes_performed"] is False
    # Same patient-facing content whether dry-run or not
    assert dry["speak"] == live["speak"]
    assert dry["hours"] == live["hours"]


def test_dry_run_string_truthy():
    data = _parse(clinic_faq({"topic": "address", "dry_run": "true"}))
    assert data["dry_run"] is True
    assert data["writes_performed"] is False


def test_config_override_via_env(tmp_path: Path, monkeypatch):
    custom = {
        "address_speak": "999 Test Ave, Suite 1, New York, NY 10001",
        "hours_speak": "Weekdays 10 AM to 2 PM only for test.",
        "parking_speak": "Test lot only — unit test override.",
        "hours": {"Mon-Thu": "10:00 AM – 2:00 PM", "Fri": "Closed", "Sat": "Closed", "Sun": "Closed"},
        "as_of": "2099-01-01",
        "barric_confirmed": True,
    }
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(custom), encoding="utf-8")
    monkeypatch.setenv("LIORA_CLINIC_FACTS_PATH", str(path))
    clear_facts_cache()

    addr = _parse(clinic_faq({"topic": "address"}))
    assert "999 Test Ave" in addr["speak"]
    assert addr["barric_confirmed"] is True

    hours = _parse(clinic_faq({"topic": "hours"}))
    assert "10 AM to 2 PM" in hours["speak"]

    park = _parse(clinic_faq({"topic": "parking"}))
    assert "Test lot only" in park["speak"]


def test_handle_faq_tool_routes():
    out = _parse(handle_faq_tool("clinic_faq", {"topic": "parking"}))
    assert out["status"] == "ok"
    bad = _parse(handle_faq_tool("not_a_tool", {}))
    assert bad["status"] == "unknown_tool"


def test_topic_payload_default_all():
    data = topic_payload("")
    assert data["topic"] == "all"
    assert data["status"] == "ok"

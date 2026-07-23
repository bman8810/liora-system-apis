"""Platform configuration dataclasses with Liora Dermatology defaults."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Set

# Consolidated credential storage
# Windows: C:\Users\barri\.liora\credentials
# WSL2:   set LIORA_CREDENTIALS_DIR=/mnt/c/Users/barri/.liora/credentials
CREDENTIALS_DIR = Path(
    os.environ.get("LIORA_CREDENTIALS_DIR",
                   os.path.expanduser("~/.liora/credentials"))
)
CREDENTIAL_FILES = {
    "weave": "weave_token.json",
    "ema": "ema_cookies.json",
    "zocdoc": "zocdoc_cookies.json",
}


@dataclass
class WeaveConfig:
    api_base: str = "https://api.weaveconnect.com"
    location_id: str = "d8508d79-c71c-4678-b139-eaedb19c2159"
    tenant_id: str = "1cdad4ca-9dbe-45f2-8263-c998c1dfec98"
    # Barric Reed (voice product B). Genie Bot user id is not used on this path.
    user_id: str = "2a3680fd-7e02-49f2-8bff-506fe1e54a0f"
    location_phone: str = "+12124334569"
    # Barric R softphone 7002 (not Genie 7018)
    softphone_id: str = "fab463cd-fc4e-406c-8779-d6c5cf8807e5"
    sip_profile_id: str = "2d99f557-a65a-4148-9a72-9c645017eeda"
    from_number: str = "2124334569"
    from_name: str = "Liora Dermatology & Aesthetics"
    allowed_send_phones: Set[str] = field(
        default_factory=lambda: {"+13302067819", "+19179401010", "+19179415577"}
    )
    # Includes Twilio sink C DID for voice B→C lab dials
    allowed_dial_phones: Set[str] = field(
        default_factory=lambda: {
            "+13302067819",
            "+18885270186",
            "+19179401010",
            "+19179415577",
        }
    )


@dataclass
class EmaConfig:
    base_url: str = "https://lioraderm.ema.md"
    cookie_file: str = "ema_cookies.json"
    facility_id: str = "2040"


@dataclass
class ZocdocConfig:
    gql_url: str = "https://api2.zocdoc.com/provider/v1/gql"
    rest_base: str = "https://www.zocdoc.com"
    practice_id: str = "pt_FMyrNSVN50CbgjEI0NcL9h"
    provider_id: str = "pr_eTTyn6m-e0y7oL1yjr9JQB"
    cookie_file: str = "zocdoc_cookies.json"


@dataclass
class GenieBottleConfig:
    base_url: str = "https://genies-bottle.vercel.app"
    agent_id: str = "claude-code"

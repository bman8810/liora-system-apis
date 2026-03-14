"""Platform configuration dataclasses with Liora Dermatology defaults."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Set

# Consolidated credential storage
CREDENTIALS_DIR = Path(
    os.environ.get("LIORA_CREDENTIALS_DIR",
                   os.path.expanduser("~/.openclaw/credentials/liora"))
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
    user_id: str = "8b835d4b-d6b3-4e81-a204-6ac39835ba2b"
    location_phone: str = "+12124334569"
    softphone_id: str = "dd2b2484-f5f0-43d2-8029-9a140f958fed"
    sip_profile_id: str = "c6d657dc-fbdd-47bd-b6e6-bc055dcd3346"
    from_number: str = "2124334569"
    from_name: str = "Liora Dermatology & Aesthetics"
    allowed_send_phones: Set[str] = field(
        default_factory=lambda: {"+13302067819", "+19179401010", "+19179415577"}
    )
    allowed_dial_phones: Set[str] = field(
        default_factory=lambda: {"+13302067819", "+19179401010", "+19179415577"}
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

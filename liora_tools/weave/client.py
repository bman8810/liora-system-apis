"""WeaveClient — unified client for Weave messaging, contacts, calls, and SIP APIs."""

import uuid

import requests

from liora_tools.config import WeaveConfig
from liora_tools.exceptions import AuthenticationError, LioraAPIError, RateLimitError
from liora_tools.utils import normalize_phone_e164


class WeaveClient:
    """Client for the Weave (getweave.com) API.

    Auth: JWT Bearer token via Authorization header + Location-Id header.
    """

    def __init__(self, session: requests.Session, config: WeaveConfig = None):
        self._s = session
        self._cfg = config or WeaveConfig()

    @classmethod
    def from_token(cls, token: str, config: WeaveConfig = None):
        """Create a WeaveClient from a JWT token."""
        from liora_tools.auth.weave import get_session
        config = config or WeaveConfig()
        session = get_session(token, config)
        return cls(session, config)

    @classmethod
    def connect(cls, config: WeaveConfig = None):
        """Create a WeaveClient with automatic auth (env token -> browser login)."""
        from liora_tools.auth.weave import ensure_session
        config = config or WeaveConfig()
        session, _token = ensure_session(config)
        return cls(session, config)

    # -- Internal helpers --

    def _get(self, path: str, params: dict = None) -> dict:
        r = self._s.get(f"{self._cfg.api_base}{path}", params=params)
        self._check_response(r)
        return r.json()

    def _post(self, path: str, json: dict = None) -> dict:
        r = self._s.post(f"{self._cfg.api_base}{path}", json=json)
        self._check_response(r)
        return r.json() if r.text else {"status": r.status_code}

    def _check_response(self, r: requests.Response) -> None:
        if r.status_code == 401:
            raise AuthenticationError("Weave token expired or invalid", status_code=401, response=r)
        if r.status_code == 429:
            raise RateLimitError("Weave rate limit exceeded", status_code=429, response=r)
        if not r.ok:
            raise LioraAPIError(f"Weave API error: {r.status_code} {r.text[:200]}", status_code=r.status_code, response=r)

    # -- Messaging --

    def list_threads(self, page_size: int = 25) -> dict:
        return self._get("/sms/data/v4/threads", {
            "locationIds": self._cfg.location_id,
            "pageSize": str(page_size),
        })

    def get_thread(self, thread_id: str, page_size: int = 25) -> dict:
        return self._get(f"/sms/data/v4/unified/threads/{thread_id}", {
            "locationId": self._cfg.location_id,
            "pageSize": str(page_size),
        })

    def search_messages(self, query: str, page_size: int = 25) -> dict:
        """Search messages and contacts by text query.

        Uses the /sms/search/v2 endpoint (backed by a search index, NOT
        Firestore) so it bypasses the ~100-thread limitation of list_threads.

        Searches both message body text and contact names. Returns threads
        with matching message snippets, person details, and threadIds.

        Returns dict with keys: threads, numResults, nextPageToken.
        Each thread has: threadId, personPhone, person, messages (snippets),
        resultType (RESULT_TYPE_THREAD for body match, RESULT_TYPE_PERSON
        for name match).
        """
        return self._get("/sms/search/v2", {
            "locationId": self._cfg.location_id,
            "groupIds": self._cfg.location_id,
            "query": query,
            "pageSize": str(page_size),
        })

    def send_message(self, person_phone: str, body: str, person_id: str = None) -> dict:
        """Send an SMS."""
        phone = normalize_phone_e164(person_phone)

        payload = {
            "locationId": self._cfg.location_id,
            "locationPhone": self._cfg.location_phone,
            "personPhone": phone,
            "programSlugId": "manual-messages",
            "createdBy": self._cfg.user_id,
            "shortenUrls": True,
            "messageType": "MESSAGING_MANUAL",
            "body": body,
            "media": [],
            "relatedIds": [],
            "id": str(uuid.uuid4()),
        }
        if person_id:
            payload["personId"] = person_id

        return self._post("/sms/send/v3", payload)

    def save_draft(self, thread_id: str, body: str, person_phone: str) -> dict:
        phone = normalize_phone_e164(person_phone)
        return self._post(f"/sms/data/v4/threads/{thread_id}/draft", {
            "body": body,
            "personPhone": phone,
            "locationId": self._cfg.location_id,
        })

    def get_draft(self, thread_id: str) -> dict:
        return self._get(f"/sms/data/v4/threads/{thread_id}/draft", {
            "locationId": self._cfg.location_id,
        })

    def indicate_typing(self, thread_id: str, person_phone: str, is_typing: bool = True) -> dict:
        phone = normalize_phone_e164(person_phone)
        return self._post(f"/sms/data/v4/threads/{thread_id}/typing", {
            "personPhone": phone,
            "locationId": self._cfg.location_id,
            "isTyping": is_typing,
        })

    # -- Contacts --

    def search_persons(self, query: str, page_size: int = 25) -> dict:
        return self._post("/persons/v3/persons/search", {
            "query": query,
            "locationIds": [self._cfg.location_id],
            "pageSize": page_size,
        })

    def lookup_by_phone(self, phone: str) -> dict:
        phone_e164 = normalize_phone_e164(phone)
        return self._get(f"/persons/v3/locations/{self._cfg.location_id}/primary-contact", {
            "phoneNumber": phone_e164,
        })

    def get_person(self, person_id: str) -> dict:
        return self._get(f"/persons/v3/persons/{person_id}")

    # -- Call Records --

    def list_call_records(self, page_size: int = 25) -> dict:
        return self._get("/phone-exp/phone-records/v1/call-records", {
            "locationIds": self._cfg.location_id,
            "pageSize": str(page_size),
        })

    def list_hydrated_call_records(self, page_size: int = 10) -> dict:
        return self._get("/phone-exp/phone-records/v1/hydrated-call-records", {
            "locationIds": self._cfg.location_id,
            "pageSize": str(page_size),
        })

    def get_call_records_by_person(self, person_ids: list) -> dict:
        return self._get("/phone-exp/phone-records/v1/call-records-by-person-ids", {
            "locationIds": self._cfg.location_id,
            "personIds": person_ids,
        })

    # -- Voicemail --

    def list_voicemails(self, page_size: int = 25) -> dict:
        return self._get("/phone-exp/phone-records/v1/voicemails", {
            "locationIds": self._cfg.location_id,
            "pageSize": str(page_size),
        })

    def list_voicemail_messages(self, page_size: int = 25) -> dict:
        return self._get("/phone-exp/phone-records/v1/voicemail-messages", {
            "locationIds": self._cfg.location_id,
            "pageSize": str(page_size),
        })

    def count_unread_voicemails(self) -> dict:
        return self._get("/phone-exp/phone-records/v1/count-unread-voicemails", {
            "locationIds": self._cfg.location_id,
        })

    def list_voicemail_boxes(self) -> dict:
        return self._get("/phone-exp/phone-records/v1/voicemail-mailboxes", {
            "locationIds": self._cfg.location_id,
        })

    # -- SIP / Phone --

    def get_softphone_settings(self) -> dict:
        return self._get("/phone/softphones/settings", {
            "locationIds": self._cfg.location_id,
        })

    def fetch_sip_credentials(
        self,
        *,
        extension: int | None = None,
        sip_profile_id: str | None = None,
        softphone_id: str | None = None,
    ) -> dict:
        """Extract SIP credentials from softphone settings.

        Prefers Barric ext 7002 / configured sip_profile_id over first list entry
        (Genie 7018 was a historical hardcode).

        Returns dict with keys: username, password, domain, proxy, extension,
        sip_profile_id, softphone_id, softphone_name.
        """
        data = self.get_softphone_settings()
        proxy = data["proxy"]
        softphones = data.get("softphones") or []
        if not softphones:
            raise RuntimeError("No softphones returned by Weave softphones/settings")

        want_ext = extension if extension is not None else getattr(
            self._cfg, "sip_extension", 7002
        )
        want_profile = sip_profile_id or self._cfg.sip_profile_id
        want_soft = softphone_id or self._cfg.softphone_id

        chosen_sp = None
        chosen_sip = None

        for sp in softphones:
            for sip in sp.get("sipProfiles") or []:
                if sip.get("id") == want_profile:
                    chosen_sp, chosen_sip = sp, sip
                    break
            if chosen_sip:
                break

        if not chosen_sip:
            for sp in softphones:
                if sp.get("id") == want_soft and (sp.get("sipProfiles") or []):
                    chosen_sp, chosen_sip = sp, sp["sipProfiles"][0]
                    break

        if not chosen_sip:
            for sp in softphones:
                for sip in sp.get("sipProfiles") or []:
                    if sip.get("extensionNumber") == want_ext:
                        chosen_sp, chosen_sip = sp, sip
                        break
                if chosen_sip:
                    break

        if not chosen_sip:
            for sp in softphones:
                name = (sp.get("name") or "").lower()
                if "barric" in name or "7002" in name:
                    sips = sp.get("sipProfiles") or []
                    if sips:
                        chosen_sp, chosen_sip = sp, sips[0]
                        break

        if not chosen_sip:
            chosen_sp = softphones[0]
            sips = chosen_sp.get("sipProfiles") or []
            if not sips:
                raise RuntimeError("Selected softphone has no sipProfiles")
            chosen_sip = sips[0]

        assert chosen_sp is not None and chosen_sip is not None
        return {
            "username": chosen_sip["username"],
            "password": chosen_sip["password"],
            "domain": chosen_sip["domain"],
            "proxy": proxy,
            "extension": chosen_sip["extensionNumber"],
            "sip_profile_id": chosen_sip["id"],
            "softphone_id": chosen_sp.get("id"),
            "softphone_name": chosen_sp.get("name"),
        }

    def list_sip_profiles(self) -> dict:
        return self._get("/phone/sip-profiles/v1", {
            "tenantId": self._cfg.tenant_id,
        })

    def get_tenants(self) -> dict:
        return self._get("/phone/tenant/tenants", {
            "orgId": self._cfg.location_id,
        })

    def dial(self, destination: str) -> dict:
        """Initiate an outbound call."""
        phone = normalize_phone_e164(destination)

        # Weave dial API expects 10-digit number without country code
        digits = phone[2:]  # strip +1

        return self._post("/phone-exp/phone-call/v1/dial", {
            "fromName": self._cfg.from_name,
            "fromNumber": self._cfg.from_number,
            "toNumber": digits,
            "sipProfileId": self._cfg.sip_profile_id,
        })

    def list_call_queues(self) -> dict:
        return self._post("/phone-exp/phone-call/v1/call-queues", {
            "locationId": self._cfg.location_id,
        })

    def get_call_queue_metrics(self) -> dict:
        return self._post("/phone-exp/phone-call/v1/call-queues/metrics", {
            "locationId": self._cfg.location_id,
        })

    def check_registration(self, sip_profile_id: str | None = None) -> dict:
        pid = sip_profile_id or self._cfg.sip_profile_id
        return self._get(f"/phone/sip-profiles/v1/{pid}/registration")

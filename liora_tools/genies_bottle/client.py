"""GenieBottleClient — client for the Genies Bottle ops dashboard API."""

import os

import requests

from liora_tools.config import GenieBottleConfig
from liora_tools.exceptions import AuthenticationError, LioraAPIError, RateLimitError


class GenieBottleClient:
    """Client for the Genies Bottle ops dashboard (genies-bottle.vercel.app).

    Auth: X-API-Key header for write (webhook) endpoints.
    Read (query) endpoints require JWT — they will raise AuthenticationError
    when called with only an API key. A future JWT auth path can be added.
    """

    def __init__(self, session: requests.Session, config: GenieBottleConfig = None):
        self._s = session
        self._cfg = config or GenieBottleConfig()

    @classmethod
    def from_api_key(cls, api_key: str = None, config: GenieBottleConfig = None):
        """Create a GenieBottleClient from an API key.

        Falls back to GENIE_BOTTLE_API_KEY env var if api_key is not provided.
        """
        api_key = api_key or os.environ.get("GENIE_BOTTLE_API_KEY")
        if not api_key:
            raise ValueError("api_key required — pass it or set GENIE_BOTTLE_API_KEY")

        config = config or GenieBottleConfig()
        session = requests.Session()
        session.headers.update({
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        })
        return cls(session, config)

    # -- Internal helpers --

    def _get(self, path: str, params: dict = None) -> dict:
        r = self._s.get(f"{self._cfg.base_url}{path}", params=params)
        self._check_response(r)
        return r.json()

    def _post(self, path: str, json: dict = None) -> dict:
        r = self._s.post(f"{self._cfg.base_url}{path}", json=json)
        self._check_response(r)
        return r.json() if r.text else {"status": r.status_code}

    def _check_response(self, r: requests.Response) -> None:
        if r.status_code == 401:
            raise AuthenticationError("Genies Bottle API key invalid", status_code=401, response=r)
        if r.status_code == 429:
            raise RateLimitError("Genies Bottle rate limit exceeded", status_code=429, response=r)
        if not r.ok:
            raise LioraAPIError(f"Genies Bottle API error: {r.status_code} {r.text[:200]}", status_code=r.status_code, response=r)

    # -- Write methods (webhook endpoints) --

    def report_process(self, task_slug: str, status: str = "running", *,
                       correlation_id: str = None,
                       trigger_type: str = None, trigger_source: str = None,
                       patient: dict = None, appointment: dict = None,
                       policy: dict = None, prior_auth: dict = None,
                       cosmetic_lead: dict = None, financing: dict = None,
                       steps: list = None, outcome_summary: str = None,
                       error_message: str = None,
                       started_at: str = None, completed_at: str = None,
                       duration_ms: int = None, metadata: dict = None) -> dict:
        """Report a process execution (task run).

        Args:
            task_slug: Slug of the task definition (e.g. 'zocdoc-new-booking').
            status: One of 'running', 'completed', 'failed', 'needs_review'.
            correlation_id: Your own ID to correlate updates.
            trigger_type: e.g. 'cron', 'webhook', 'manual'.
            trigger_source: e.g. 'zocdoc', 'modmed'.
            patient: Patient info — include 'mrn' key to auto-link patient record.
            appointment: Appointment details if relevant.
            policy: Insurance policy details if relevant.
            prior_auth: Prior authorization details if relevant.
            cosmetic_lead: Cosmetic lead details if relevant.
            financing: Financing details if relevant.
            steps: List of step objects, e.g. [{"name": "lookup", "status": "done", "detail": "Found record"}].
            outcome_summary: Human-readable summary of the outcome.
            error_message: Error details if status is 'failed'.
            started_at: ISO 8601 timestamp.
            completed_at: ISO 8601 timestamp.
            duration_ms: Execution duration in milliseconds.
            metadata: Any extra key-value data.
        """
        payload = {"task_slug": task_slug, "status": status}
        _optionals = {
            "correlation_id": correlation_id, "trigger_type": trigger_type,
            "trigger_source": trigger_source, "patient": patient,
            "appointment": appointment, "policy": policy,
            "prior_auth": prior_auth, "cosmetic_lead": cosmetic_lead,
            "financing": financing, "steps": steps,
            "outcome_summary": outcome_summary, "error_message": error_message,
            "started_at": started_at, "completed_at": completed_at,
            "duration_ms": duration_ms, "metadata": metadata,
        }
        payload.update({k: v for k, v in _optionals.items() if v is not None})
        return self._post("/api/webhooks/process", payload)

    def log_activity(self, action: str, description: str, *,
                     source: str = None, payload: dict = None,
                     patient: dict = None) -> dict:
        """Log a miscellaneous activity (not tied to a specific task).

        Args:
            action: Short action name, e.g. 'sms_sent', 'form_filled'.
            description: Human-readable description of what happened.
            source: System that generated this activity.
            payload: Any structured data.
            patient: Patient info — include 'mrn' key to auto-link patient record.
        """
        body = {
            "agent_id": self._cfg.agent_id,
            "action": action,
            "description": description,
        }
        if source is not None:
            body["source"] = source
        if payload is not None:
            body["payload"] = payload
        if patient is not None:
            body["patient"] = patient
        return self._post("/api/webhooks/activity", body)

    def request_feedback(self, title: str, description: str = None, *,
                         priority: str = "normal",
                         process_execution_id: str = None,
                         bot_context: dict = None,
                         patient: dict = None) -> dict:
        """Request human feedback or flag something for review.

        Args:
            title: Short title for the feedback request.
            description: Detailed description of what needs attention.
            priority: One of 'urgent', 'high', 'normal', 'low'.
            process_execution_id: Link to a specific execution UUID.
            bot_context: Context for the human reviewer.
            patient: Patient info — include 'mrn' key to auto-link patient record.
        """
        body = {
            "agent_id": self._cfg.agent_id,
            "title": title,
            "priority": priority,
        }
        if description is not None:
            body["description"] = description
        if process_execution_id is not None:
            body["process_execution_id"] = process_execution_id
        if bot_context is not None:
            body["bot_context"] = bot_context
        if patient is not None:
            body["patient"] = patient
        return self._post("/api/webhooks/feedback", body)

    def heartbeat(self, **data) -> dict:
        """Send an agent heartbeat. Accepts any JSON structure."""
        body = {"agent_id": self._cfg.agent_id}
        body.update(data)
        return self._post("/api/openclaw/heartbeat", body)

    # -- Reference --

    def get_integration_guide(self) -> dict:
        """Get the full API integration guide (endpoints, payload shapes, available tasks/skills)."""
        return self._get("/api/integration")

    # -- Read methods (query endpoints, require JWT) --

    def get_dashboard(self) -> dict:
        """Get aggregated dashboard stats."""
        return self._get("/api/dashboard")

    def list_executions(self, *, task_slug: str = None, status: str = None,
                        limit: int = None, offset: int = None) -> dict:
        """List task executions."""
        params = {}
        if task_slug is not None:
            params["task_slug"] = task_slug
        if status is not None:
            params["status"] = status
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return self._get("/api/executions", params or None)

    def list_activities(self, *, agent_id: str = None, action: str = None,
                        limit: int = None, offset: int = None) -> dict:
        """List logged activities."""
        params = {}
        if agent_id is not None:
            params["agent_id"] = agent_id
        if action is not None:
            params["action"] = action
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return self._get("/api/activities", params or None)

    def list_feedback(self, *, status: str = None, priority: str = None,
                      limit: int = None, offset: int = None) -> dict:
        """List feedback requests."""
        params = {}
        if status is not None:
            params["status"] = status
        if priority is not None:
            params["priority"] = priority
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return self._get("/api/feedback", params or None)

    def list_tasks(self) -> dict:
        """List all task definitions."""
        return self._get("/api/tasks")

    def search_patients(self, query: str) -> dict:
        """Search patients by name or identifier."""
        return self._get("/api/patients", {"query": query})

    def get_patient_timeline(self, patient_id: str) -> dict:
        """Get a patient's event timeline."""
        return self._get(f"/api/patients/{patient_id}/timeline")

    def get_skills_manifest(self, agent_id: str = None) -> dict:
        """Get lightweight skill manifest for diffing (id, slug, version, updated_at)."""
        params = {}
        if agent_id is not None:
            params["agent_id"] = agent_id
        return self._get("/api/skills/sync/manifest", params or None)

    def get_skills_batch(self, since: str = None, agent_id: str = None) -> dict:
        """Get full skill documents modified after a given timestamp.

        Args:
            since: ISO 8601 timestamp — only return skills updated after this time.
            agent_id: Filter by agent.
        """
        params = {}
        if since is not None:
            params["since"] = since
        if agent_id is not None:
            params["agent_id"] = agent_id
        return self._get("/api/skills/sync/batch", params or None)

    def get_skills_deleted(self, since: str = None, agent_id: str = None) -> dict:
        """Get skills unpublished since a given timestamp (for cleanup).

        Args:
            since: ISO 8601 timestamp.
            agent_id: Filter by agent.
        """
        params = {}
        if since is not None:
            params["since"] = since
        if agent_id is not None:
            params["agent_id"] = agent_id
        return self._get("/api/skills/sync/deleted", params or None)

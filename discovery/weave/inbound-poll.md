# Weave inbound poll/search module

SoT module: `liora_tools/weave/inbound.py`  
Card: `t_f36e8d01` (Phase 2 messaging worker)

## Why `search_messages` is primary

| Path | Backend | Coverage | Use |
|------|---------|----------|-----|
| **`search_messages`** (`GET /sms/search/v2`) | Search index | Full history matching query | **Default durable poll** |
| **`list_threads`** (`GET /sms/data/v4/threads`) | Firestore inbox | ~recent **100** threads | Explicit fallback only |
| **`get_thread`** | Thread detail | Full messages in one thread | Optional hydrate after search |

`list_threads` misses older patient SMS. Do not build always-on inbound on it alone.

## Pagination / cursors

- **Search:** response `nextPageToken` → next call `pageToken` (client arg `page_token`).  
  Poller stores per-query cursors in `InboundPollResult.next_cursors` (`{query: token}`).
- **List fallback:** response `olderPageToken` → next call `olderPageToken` (client arg `page_token` on `list_threads`). Cursor key: `list_threads`.
- Safety: `max_pages` cap (default 10 search / 3 list).

Search **requires a non-empty query** — there is no API “all inbound since T”. Use topic queries (`default_topic_queries()`), fingerprints (e.g. Zocdoc `$100` phrase), or targeted phone last-10 / name (PHI-sensitive).

## Normalized event shape

`InboundMessage`:

- `thread_id`, `message_id`, `timestamp`
- `direction`: `inbound` | `outbound` | `unknown`
- `participant_phone`, `participant_name`, `person_id`
- `body_preview` (truncated), optional full `body`
- `result_type`, `source` (`search` | `thread` | `list_threads` | `fixture`)
- `raw_refs` (ids + query/fixture name only)

Logs/Telegram: use `to_safe_dict()` / `InboundPollResult.to_safe_summary()` (masked phone, initials, short preview).

Search snippets often omit `direction` → `unknown` until `hydrate=True` pulls `get_thread`.

## Auth

Reuse existing Weave JWT path (Kernel Liora profile):

```python
from liora_tools.auth.session_manager import get_client
from liora_tools.weave.inbound import poll_inbound, default_topic_queries

weave = get_client("weave")  # token file → kernel-sync refresh
result = poll_inbound(weave, queries=default_topic_queries())
```

Ops user context: **barric.reed** (not Genie Bot). Twilio DID messaging is **not** the product path.

## Fixture / dry-run (no network)

```bash
python -m liora_tools weave inbound-poll \
  --fixture tests/fixtures/weave_inbound/search_sample.json
```

Or:

```python
from liora_tools.weave.inbound import poll_inbound
r = poll_inbound(fixture_path="tests/fixtures/weave_inbound/search_sample.json")
for m in r.messages:
    ...  # classify/route — no send
```

Fixtures live under `tests/fixtures/weave_inbound/`. Accepts search payload, list payload, get_thread payload, or pre-normalized `messages[]`.

## Live poll (read-only)

```bash
# Requires valid weave_token.json / kernel-sync — still no SMS send
python -m liora_tools weave inbound-poll --query appointment --query refill
python -m liora_tools weave inbound-poll --topics --hydrate
# Opt-in capped list fallback only if needed:
python -m liora_tools weave inbound-poll --topics --allow-list-fallback
```

## Safety

- Module **never** calls `send_message`.
- No always-on cron in this card (worker loop / double-gated job = sibling `t_facec990`).
- No live patient SMS until Barric go-live flag on the full worker.

## Tests

```bash
.venv/bin/python -m pytest tests/test_weave_inbound.py -q
```

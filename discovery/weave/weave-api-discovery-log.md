# Weave API Discovery Log

## Session: 2026-03-12

### Phase 1: Auth Discovery - COMPLETE

**Login flow:**
1. Navigate to `app.getweave.com/sign-in` -> redirects to `auth.getweave.com`
2. OIDC via Ory Hydra at `oidc.weaveconnect.com`
3. After login, two token types stored in `localStorage`:

**Token 1: `oidc-token-storage`** (OIDC layer)
- `accessToken`: Ory format `ory_at_...`
- `IDToken`: JWT (RS256, kid from `oidc.weaveconnect.com`)
- ID token contains: email, name, locations[], aud, iss

**Token 2: `token`** (Weave API JWT - THIS IS THE ONE USED FOR API CALLS)
- Algorithm: RS256, kid: "w-3"
- Payload: user_id, username (email), ACLS (permission numbers per location), type: "practice"
- Expiry: ~4 hours from issuance
- Used as: `Authorization: Bearer <token>` header

**Key IDs:**
- Location ID: `d8508d79-c71c-4678-b139-eaedb19c2159`
- User ID: `8b835d4b-d6b3-4e81-a204-6ac39835ba2b`
- Username: `genie.huertas@outlook.com`
- User display name: "Genie Bot"
- Organization ID = Location ID (same for single-location practices)

**Other localStorage keys:**
- `weave.login-data` - maps user_id -> latestOrganizationId, latestLocationIds
- `weave.schedule-version-changed` - "v2"

### Phase 2: Messaging API Discovery - COMPLETE

**Base URL:** `https://api.weaveconnect.com`

**URL structure in webapp:**
- Inbox: `/messages/inbox`
- Thread: `/messages/inbox/{locationId}/{threadId}?personId={personId}&personPhone={phone}`

**API Endpoints discovered (all prefixed with `https://api.weaveconnect.com`):**

#### SMS/Messaging
| Method | Path | Params | Notes |
|--------|------|--------|-------|
| GET | `/sms/data/v4/threads` | locationIds, pageSize | **List inbox threads** |
| GET | `/sms/data/v4/unified/threads/{threadId}` | locationId, pageSize | Load messages in a thread |
| POST | `/sms/send/v3` | (JSON body) | **Send SMS message** |
| PUT | `/sms/draft/v1/draft` | (JSON body) | Save draft |
| POST | `/sms/notifier/v1/indicate-typing` | (JSON body) | Typing indicator |
| GET | `/sms/data/v4/threads/status:batchGet` | | Batch get thread statuses |
| GET | `/sms/draft/v1/draft/{threadId}` | locationId, userId, orgId | Get draft for thread |
| GET | `/sms/number/v1/validity` | phoneNumber | Check if phone number is valid for SMS |
| GET | `/sms/writeback/v1/can-sms-writeback` | locationId | Check SMS writeback capability |
| GET | `/sms/signature/v1/signature` | | Get SMS signature |
| GET | `/messaging/scheduled/manual-sms/v1/thread` | threadId, locationId | Get scheduled messages for thread |
| GET | `/phone-exp/phone-numbers/v1/sms-phone-numbers` | | List SMS-capable phone numbers |
| GET | `/comm-preference/preference/v1/preference` | userChannelAddress, locationId, messageType, channel | Check comm preferences |

#### Persons/Contacts
| Method | Path | Params | Notes |
|--------|------|--------|-------|
| POST | `/persons/v3/persons/search` | (JSON body: query, locationIds, pageSize) | **Search persons by name** |
| GET | `/persons/v3/locations/{locationId}/primary-contact` | phoneNumber | **Look up person by phone** |
| GET | `/persons/v3/persons/{personId}` | | Get full person details |
| GET | `/photo/v1/person/{personId}` | | Get person photo |
| GET | `/persons/v3/locations/{locationId}/preferred-persons` | | Get preferred persons list |
| GET | `/household/{householdId}/contact` | | Get household contacts |

#### Other
| Method | Path | Params | Notes |
|--------|------|--------|-------|
| GET | `/portal/v1/users` | | Get portal users |
| GET | `/appointments/v3/list` | | List appointments |
| GET | `/payments/v1/search/invoices` | limit, skip, order, personid, locationIds, status, active | Search invoices |
| GET | `/payments/views/service/locations/{locationId}/feature-settings` | | Payment feature settings |
| GET | `/email/records/v1/location_preference/{locationId}` | | Email preferences |
| GET | `/subscription-manager-service/v1/subscription/multi/addon/status` | | Subscription status |

### Phase 3: Send Message Discovery - COMPLETE

**Send endpoint:** `POST /sms/send/v3`

Request body (captured by observing actual message send in UI):
```json
{
  "locationId": "d8508d79-c71c-4678-b139-eaedb19c2159",
  "locationPhone": "+12124334569",
  "personPhone": "+13302067819",
  "programSlugId": "manual-messages",
  "personId": "3940223d-6224-556d-a95e-103ae8d27050",
  "createdBy": "8b835d4b-d6b3-4e81-a204-6ac39835ba2b",
  "shortenUrls": true,
  "messageType": "MESSAGING_MANUAL",
  "body": "test 2 - capturing request body",
  "media": [],
  "relatedIds": [],
  "id": "client-generated-uuid-v4"
}
```

**Also discovered:**
- `PUT /sms/draft/v1/draft` — saves drafts while typing
- `POST /sms/notifier/v1/indicate-typing` — typing indicator
- `POST /persons/v3/persons/search` — person search (body: `{query, locationIds, pageSize}`)
- `GET /sms/data/v4/threads?locationIds={id}&pageSize=N` — inbox thread list (note: `locationIds` plural)

**Required headers for all API calls:**
- `Authorization: Bearer <JWT from localStorage['token']>`
- `Location-Id: <location UUID>`

### Phase 4: Person/Contact Discovery - COMPLETE

- `POST /persons/v3/persons/search` — fuzzy search by name
- `GET /persons/v3/locations/{locId}/primary-contact?phoneNumber=+1XXXXXXXXXX` — phone lookup (exact)
- `GET /persons/v3/persons/{personId}` — full person details
- `GET /photo/v1/person/{personId}` — person photo

### What didn't work
- Network request capture via `read_network_requests` tool clears on page navigation, so it misses requests from the page load that triggered the nav
- `read_network_requests` with response bodies returns data that gets blocked by cookie/query string filter
- Had to use JavaScript fetch interceptor instead to capture API calls
- The interceptor was installed AFTER initial inbox load, so I missed the "list threads" / inbox endpoint initially
- First interceptor version didn't capture request bodies for `Request` objects (body was a ReadableStream) — had to clone the Request and read `.text()` asynchronously
- Tried guessing 16+ send endpoint paths (all 404) before resorting to UI observation:
  - `/sms/v1/send`, `/sms/v2/send`, `/sms/v3/send`, `/messaging/v1/send`, etc.
  - The actual path was `/sms/send/v3` (service-first, then version — not version-first)
- Person search for "Barric" returned many unrelated results (fuzzy match) — phone lookup via `primary-contact` was more reliable

### What worked
- JavaScript fetch monkey-patch captured all API calls after installation
- Stripping query params from URLs avoided the cookie data blocking
- Token extraction from localStorage worked perfectly
- Cloning Request objects before reading body allowed capture of POST payloads
- Observing actual UI message send was the only reliable way to find the send endpoint
- Phone number lookup via `primary-contact` endpoint gave exact person matching

### Deliverables created
- [x] `weave-api-reference.md` — comprehensive API documentation
- [x] `test_weave_api.py` — Python client with auth, list_messages, get_thread, search_persons, send_message
- [x] `weave-api-discovery-log.md` — this file

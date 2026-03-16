# Weave API Reference

> Reverse-engineered from `app.getweave.com` (Weave web app)
> Generated: 2026-03-12

## Base URL

```
https://api.weaveconnect.com
```

## Architecture

- **gRPC backend** with HTTP REST gateway (grpc-gateway annotations)
- JWT auth with RS256 signing
- gRPC-style action suffixes: `:batchGet`, `:count`, `:details`
- Service paths follow pattern: `/<service-name>/<version>/`

## Authentication

### Token Types

Two tokens are stored in `localStorage` after login:

**1. OIDC Token (`oidc-token-storage`)**
- Access token: Ory format `ory_at_...`
- ID token: JWT (RS256) from `oidc.weaveconnect.com`
- Contains: email, name, locations[], aud, iss
- Not used directly for API calls

**2. Weave API Token (`token`)** — **THIS IS THE ONE USED FOR API CALLS**
- Algorithm: RS256, kid: `w-3`
- Payload: `user_id`, `username` (email), `ACLS` (permission numbers per location), `type: "practice"`
- Expiry: ~4 hours from issuance
- Used as: `Authorization: Bearer <token>` header

### Login Flow (OIDC via Ory Hydra)

```
1. Navigate to app.getweave.com/sign-in
   → Redirects to auth.getweave.com

2. OIDC flow via Ory Hydra at oidc.weaveconnect.com
   → User enters email + password

3. After login, two tokens stored in localStorage:
   - oidc-token-storage (OIDC layer)
   - token (Weave API JWT)
```

### Required Headers

All API calls require:

| Header | Value |
|--------|-------|
| `Authorization` | `Bearer <token from localStorage>` |
| `Location-Id` | Location UUID (e.g., `d8508d79-c71c-4678-b139-eaedb19c2159`) |

### Key IDs (Liora Dermatology)

| ID | Value |
|----|-------|
| Location ID | `d8508d79-c71c-4678-b139-eaedb19c2159` |
| Organization ID | `d8508d79-c71c-4678-b139-eaedb19c2159` (same as location for single-location) |
| User ID (Genie Bot) | `8b835d4b-d6b3-4e81-a204-6ac39835ba2b` |
| Username | `genie.huertas@outlook.com` |
| Location Phone | `+12124334569` |

---

## Messaging (SMS) Endpoints

### List Threads (Inbox)

```
GET /sms/data/v4/threads?locationIds={locationId}&pageSize={n}
```

**Query Parameters:**
| Param | Required | Description |
|-------|----------|-------------|
| `locationIds` | Yes | Location UUID (note: plural `locationIds`, not `locationId`) |
| `pageSize` | No | Number of threads to return |

**Response:**
```json
{
  "threads": [
    {
      "id": "thread-uuid",
      "locationId": "location-uuid",
      "messages": [
        {
          "id": "msg-uuid",
          "threadId": "thread-uuid",
          "locationPhone": "+12124334569",
          "personPhone": "+1XXXXXXXXXX",
          "direction": "DIRECTION_OUTBOUND",
          "body": "message text",
          "status": "STATUS_DELIVERED",
          "createdAt": "2026-03-12T..."
        }
      ],
      "person": {
        "personId": "person-uuid",
        "firstName": "John",
        "lastName": "Doe",
        "preferredName": ""
      },
      "status": "STATUS_NEW",
      "actionable": true,
      "isArchived": false
    }
  ],
  "olderPageToken": "token-for-next-page"
}
```

### Get Thread Detail

```
GET /sms/data/v4/unified/threads/{threadId}?locationId={locationId}&pageSize={n}
```

**Response:**
```json
{
  "thread": {
    "id": "thread-uuid",
    "locationId": "location-uuid",
    "items": [
      {
        "smsMessage": {
          "id": "msg-uuid",
          "body": "message text",
          "direction": "DIRECTION_OUTBOUND",
          "status": "STATUS_DELIVERED",
          "createdAt": "2026-03-12T...",
          "personPhone": "+1XXXXXXXXXX",
          "locationPhone": "+12124334569"
        }
      }
    ],
    "person": { "personId": "...", "firstName": "...", "lastName": "..." },
    "status": "STATUS_NEW",
    "actionable": true,
    "isBlocked": false,
    "personPhone": "+1XXXXXXXXXX",
    "locationPhone": "+12124334569",
    "aiStatus": "..."
  },
  "olderPageToken": "...",
  "newerPageToken": "..."
}
```

### Send Message

```
POST /sms/send/v3
```

**Request Body:**
```json
{
  "locationId": "d8508d79-c71c-4678-b139-eaedb19c2159",
  "locationPhone": "+12124334569",
  "personPhone": "+13302067819",
  "programSlugId": "manual-messages",
  "personId": "person-uuid",
  "createdBy": "user-uuid",
  "shortenUrls": true,
  "messageType": "MESSAGING_MANUAL",
  "body": "Your message text here",
  "media": [],
  "relatedIds": [],
  "id": "client-generated-uuid-v4"
}
```

**Field Notes:**
| Field | Description |
|-------|-------------|
| `locationId` | Your practice location UUID |
| `locationPhone` | Your Weave phone number (E.164 format) |
| `personPhone` | Recipient phone (E.164 format, e.g., `+13302067819`) |
| `programSlugId` | Always `"manual-messages"` for manual sends |
| `personId` | The person/contact UUID (from person search or thread) |
| `createdBy` | Your user UUID |
| `messageType` | `"MESSAGING_MANUAL"` for manual messages |
| `id` | Client-generated UUID v4 (idempotency key) |
| `media` | Array of media attachments (empty for text-only) |
| `relatedIds` | Array of related entity IDs (usually empty) |
| `shortenUrls` | Whether to shorten URLs in the message body |

### Save Draft

```
PUT /sms/draft/v1/draft
```

**Request Body:**
```json
{
  "draft": {
    "medias": [],
    "relatedIds": [],
    "body": "draft text"
  },
  "orgId": "location-uuid",
  "threadId": "thread-uuid",
  "locationId": "location-uuid",
  "locationPhone": "+12124334569",
  "userId": "user-uuid",
  "personPhone": "+1XXXXXXXXXX"
}
```

### Get Draft

```
GET /sms/draft/v1/draft/{threadId}?locationId={locationId}&userId={userId}&orgId={orgId}
```

### Indicate Typing

```
POST /sms/notifier/v1/indicate-typing
```

**Request Body:**
```json
{
  "groupId": "location-uuid",
  "threadId": "thread-uuid",
  "userId": "user-uuid",
  "isTyping": true,
  "personPhone": "3302067819"
}
```

### Search Messages

```
GET /sms/search/v2?locationId={locationId}&groupIds={locationId}&query={text}&pageSize={n}
```

Full-text search across message bodies and contact names. Backed by a proper
search index — **bypasses the ~100-thread Firestore limitation** of
`list_threads` / `get_thread`.

**Query Parameters:**
| Param | Required | Description |
|-------|----------|-------------|
| `locationId` | Yes | Location UUID |
| `groupIds` | Yes | Same as locationId (for single-location practices) |
| `query` | Yes | Search text (searches message body + contact names) |
| `pageSize` | No | Max results (default 25) |

**Response:**
```json
{
  "threads": [
    {
      "threadId": "thread-uuid",
      "locationId": "location-uuid",
      "personId": "person-uuid",
      "personPhone": "+1XXXXXXXXXX",
      "person": {
        "firstName": "Jane",
        "lastName": "Doe",
        "status": "ACTIVE",
        "contactInfo": [ ... ]
      },
      "messages": [
        {
          "smsId": "msg-uuid",
          "timestamp": "2026-03-06T16:13:33Z",
          "fragment": "matching message snippet text..."
        }
      ],
      "resultType": "RESULT_TYPE_THREAD"
    }
  ],
  "numResults": 23,
  "nextPageToken": ""
}
```

**Result Types:**
| `resultType` | Meaning |
|--------------|---------|
| `RESULT_TYPE_THREAD` | Query matched message body text |
| `RESULT_TYPE_PERSON` | Query matched contact name (returns latest message as snippet) |

**Notes:**
- Each thread returns only 1 message snippet (the best match or most recent)
- To search for a specific topic across all patients, search for the topic (e.g., `"blue cross"`)
- To find a patient's thread, search by their name (e.g., `"James Kepner"`)
- Returns full person details including contactInfo, address, etc.

### Other SMS Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/sms/data/v4/threads/status:batchGet` | Batch get thread statuses |
| GET | `/sms/number/v1/validity?phoneNumber={phone}` | Check if phone is SMS-capable |
| GET | `/sms/writeback/v1/can-sms-writeback?locationId={id}` | Check writeback capability |
| GET | `/sms/signature/v1/signature` | Get SMS signature |
| GET | `/messaging/scheduled/manual-sms/v1/thread?threadId={id}&locationId={id}` | Get scheduled messages |
| GET | `/phone-exp/phone-numbers/v1/sms-phone-numbers` | List SMS-capable phone numbers |
| GET | `/comm-preference/preference/v1/preference` | Check communication preferences |

---

## Persons/Contacts Endpoints

### Search Persons

```
POST /persons/v3/persons/search
```

**Request Body:**
```json
{
  "query": "search term",
  "locationIds": ["location-uuid"],
  "pageSize": 25
}
```

**Response:**
```json
{
  "persons": [
    {
      "personId": "person-uuid",
      "firstName": "Barric",
      "lastName": "Reed",
      "preferredName": "",
      "status": "Active",
      "gender": "Male",
      "birthdate": "1989-01-15",
      "contactInfo": { ... }
    }
  ],
  "nextPageToken": "..."
}
```

### Lookup Person by Phone

```
GET /persons/v3/locations/{locationId}/primary-contact?phoneNumber={e164Phone}
```

Phone must be URL-encoded E.164 format (e.g., `%2B13302067819`).

**Response:**
```json
{
  "locationId": "location-uuid",
  "id": "contact-uuid",
  "personId": "person-uuid",
  "phoneNumber": "+13302067819",
  "createdAt": "...",
  "updatedAt": "..."
}
```

### Get Person Details

```
GET /persons/v3/persons/{personId}
```

### Get Person Photo

```
GET /photo/v1/person/{personId}
```

### Other Contact Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/persons/v3/locations/{locationId}/preferred-persons` | Get preferred persons list |
| GET | `/household/{householdId}/contact` | Get household contacts |

---

## Other Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/portal/v1/users` | Get portal users |
| POST | `/appointments/v3/list` | List appointments |
| GET | `/payments/v1/search/invoices` | Search invoices |
| GET | `/payments/views/service/locations/{locationId}/feature-settings` | Payment feature settings |
| GET | `/email/records/v1/location_preference/{locationId}` | Email preferences |
| GET | `/subscription-manager-service/v1/subscription/multi/addon/status` | Subscription status |
| GET | `/messaging/templator/v2/template-types` | Message template types |
| GET | `/messaging/templator/v2/templates` | Message templates |
| GET | `/platform-location-feature/v1/locations/features` | Location features |
| GET | `/salesforce/v1/account` | Salesforce account info |

> **Phone/Calling APIs** are documented separately in `weave-phone-api-reference.md`.
> Covers: call history, voicemail, softphone/SIP, call initiation, call queues, call groups.

---

## URL Structure in Web App

| Page | URL Pattern |
|------|-------------|
| Inbox | `/messages/inbox` |
| Thread | `/messages/inbox/{locationId}/{threadId}?personId={personId}&personPhone={phone}&locationPhone={phone}` |
| New Message | `/messages/inbox/new` |
| Contacts | `/contacts/all-contacts` |
| Call History | `/calls/call-history` |
| Call Queue Stats | `/calls/call-queue-stats` |
| Voicemail | `/calls/voicemail` |

---

## Programmatic Auth with Playwright

> Token is a JWT stored in `localStorage['token']` — extract it after browser login.

```python
from playwright.sync_api import sync_playwright

def get_weave_token():
    """Login to Weave and return the API JWT token."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://app.getweave.com/sign-in", wait_until="networkidle")

        # Wait for user to complete login
        page.wait_for_url("**/home/**", timeout=120000)

        # Extract tokens from localStorage
        token = page.evaluate("localStorage.getItem('token')")
        login_data = page.evaluate("localStorage.getItem('weave.login-data')")

        browser.close()
        return token, login_data
```

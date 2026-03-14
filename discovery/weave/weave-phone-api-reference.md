# Weave Phone/Calling API Reference

> Reverse-engineered from `app.getweave.com` (Weave web app)
> Generated: 2026-03-13

## Base URL

```
https://api.weaveconnect.com
```

## Required Headers

| Header | Value |
|--------|-------|
| `Authorization` | `Bearer <token from localStorage>` |
| `Location-Id` | Location UUID |

## Key IDs (Liora Dermatology)

| ID | Value |
|----|-------|
| Location ID | `d8508d79-c71c-4678-b139-eaedb19c2159` |
| Tenant ID | `1cdad4ca-9dbe-45f2-8263-c998c1dfec98` |
| User ID (Genie Bot) | `8b835d4b-d6b3-4e81-a204-6ac39835ba2b` |
| Genie's Softphone ID | `dd2b2484-f5f0-43d2-8029-9a140f958fed` |
| Genie's SIP Profile ID | `c6d657dc-fbdd-47bd-b6e6-bc055dcd3346` |
| Voicemail Box ID | `97db8842-a469-4d87-8371-a08bd923bd9d` |
| Location Phone | `+12124334569` |
| SIP Domain | `s00448454.getweave.io` |
| SIP WebSocket Proxy | `sip-websockets-glb.us1.weavephone.net` |

---

## Call Records (History) — `/phone-exp/phone-records/v1/`

### List Call Records

```
GET /phone-exp/phone-records/v1/call-records?locationIds={locationId}&pageSize={n}
```

**Query Parameters:**
| Param | Required | Description |
|-------|----------|-------------|
| `locationIds` | Yes | Location UUID |
| `pageSize` | No | Number of records (default 25) |
| `startDate` | No | ISO 8601 start filter |
| `endDate` | No | ISO 8601 end filter |

**Response:**
```json
{
  "records": [
    {
      "id": "uuid",
      "channelId": "uuid",
      "locationId": "uuid",
      "direction": "inbound|outbound",
      "startedAt": "2026-03-13T17:11:48Z",
      "answeredAt": "2026-03-13T17:12:19Z",
      "endedAt": "2026-03-13T17:13:21Z",
      "duration": "0.000093040s",
      "dialedNumber": "2124334569",
      "callerName": "4088357576",
      "callerNumber": "4088357576",
      "personId": "uuid",
      "userId": "",
      "sipId": "",
      "sipName": "",
      "status": "missed|answered|abandoned|voicemail",
      "voicemailBox": "",
      "forwardedTo": "",
      "viewedBy": "",
      "mosScore": 4.5,
      "departmentId": "uuid",
      "recordingFilename": "channelId.mp3",
      "recordingPath": "complete/tenantId",
      "cdrCreatedTime": null,
      "cdrBridgedTime": null,
      "tagIds": [],
      "aiMetadata": null
    }
  ],
  "limit": 25,
  "lastId": "uuid",
  "endDate": "2026-03-13T04:42:40Z"
}
```

**Pagination:** Use `lastId` from response as cursor for next page.

### Get Hydrated Call Records

Returns call records enriched with person/contact data.

```
GET /phone-exp/phone-records/v1/hydrated-call-records?locationIds={locationId}&pageSize={n}
```

### Get Single Hydrated Call Record

```
GET /phone-exp/phone-records/v1/hydrated-call-record?callRecordId={id}&locationId={locationId}
```

### Get Call Records by Person IDs

```
GET /phone-exp/phone-records/v1/call-records-by-person-ids?personIds={id1}&personIds={id2}&locationIds={locationId}
```

### Mark All Call Records Viewed

```
POST /phone-exp/phone-records/v1/call-records/mark-all-viewed
```

**Request Body:**
```json
{
  "locationId": "uuid"
}
```

### Apply Call Record Tags

```
POST /phone-exp/phone-records/v1/apply-call-record-tags
```

**Request Body:**
```json
{
  "callRecordId": "uuid",
  "tagIds": ["uuid"],
  "locationId": "uuid"
}
```

### Remove Call Record Tags

```
DELETE /phone-exp/phone-records/v1/remove-call-record-tags?callRecordId={id}&tagIds={tagId}&locationId={locationId}
```

### Get Call Recording Signed URL

> Requires ACL permission for call recording access.

```
GET /phone-exp/phone-records/v1/call-recording/signed-url?channelId={channelId}&locationId={locationId}
```

### Delete Call Recordings

```
DELETE /phone-exp/phone-records/v1/call-recordings?channelId={channelId}&locationId={locationId}
```

### Export Call Records

```
POST /phone-exp/phone-records/v1/export/call-records
```

```
GET /phone-exp/phone-records/v1/export/call-records/{exportId}/status
```

```
GET /phone-exp/phone-records/v1/exports/all-call-records?locationIds={locationId}
```

```
GET /phone-exp/phone-records/v1/exports/call-records-signed-url?exportId={exportId}
```

### Widget Endpoints

```
GET /phone-exp/phone-records/v1/widget/call-results?locationIds={locationId}
GET /phone-exp/phone-records/v1/widget/phone-snapshot?locationIds={locationId}
```

---

## Voicemail — `/phone-exp/phone-records/v1/`

### List Voicemails (Hydrated)

```
GET /phone-exp/phone-records/v1/voicemails?locationIds={locationId}&pageSize={n}
```

**Response:**
```json
{
  "hydratedVoicemails": [
    {
      "message": {
        "channelId": "uuid",
        "mailboxId": "uuid",
        "mediaId": "uuid",
        "locationId": "uuid",
        "playLength": "30s",
        "personId": "uuid",
        "callerName": "John Doe",
        "callerNumber": "2125551234",
        "forwardedBy": "",
        "markedUrgent": false,
        "departmentId": "uuid",
        "callRecordId": "uuid",
        "startedAt": "2026-03-13T...",
        "createdAt": "2026-03-13T...",
        "readAt": null,
        "updatedAt": "2026-03-13T...",
        "deletedAt": null,
        "forwardedMessageId": "",
        "voicemailId": "uuid",
        "isInternal": false
      },
      "person": {
        "personId": "uuid",
        "personPmId": "",
        "firstName": "John",
        "lastName": "Doe",
        "preferredName": "",
        "status": "Active",
        "gender": "",
        "sourceId": "",
        "birthdate": "",
        "entryDate": "",
        "contactInfo": {}
      },
      "tagIds": []
    }
  ]
}
```

### List Voicemail Messages (Raw)

```
GET /phone-exp/phone-records/v1/voicemail-messages?locationIds={locationId}&pageSize={n}
```

### Count Unread Voicemails

```
GET /phone-exp/phone-records/v1/count-unread-voicemails?locationIds={locationId}
```

**Response:**
```json
{
  "countPerMailbox": {
    "97db8842-a469-4d87-8371-a08bd923bd9d": 63
  }
}
```

### List Voicemail Boxes

```
GET /phone-exp/phone-records/v1/voicemail-mailboxes?locationIds={locationId}
```

**Response:**
```json
{
  "voicemailBoxes": [
    {
      "mailbox": {
        "id": "97db8842-a469-4d87-8371-a08bd923bd9d",
        "name": "9000 General & Front Desk VM",
        "sendNotification": true,
        "email": "hello@lioradermatology.com,breed@lioradermatology.com",
        "attachFile": false,
        "pin": 4569,
        "number": 9000,
        "tenantId": "1cdad4ca-9dbe-45f2-8263-c998c1dfec98",
        "playMessageDate": true,
        "sharedAccess": true
      },
      "mailboxType": "MAILBOX_TYPE_SHARED",
      "locationIds": ["d8508d79-c71c-4678-b139-eaedb19c2159"]
    }
  ]
}
```

### Voicemail Audio Download

> Discovered via UI observation — uses query-string auth, not header auth.

```
GET /phone-exp/voicemail/download/{voicemailId}?token={JWT}&location_id={locationId}
```

### Apply/Remove Voicemail Tags

```
POST /phone-exp/phone-records/v1/apply-voicemail-message-tags
```

```json
{
  "voicemailId": "uuid",
  "tagIds": ["uuid"],
  "locationId": "uuid"
}
```

```
POST /phone-exp/phone-records/v1/remove-voicemail-message-tags
```

---

## Call Initiation — `/phone-exp/phone-call/v1/`

### Dial

> Initiates an outbound call from a softphone/device (click-to-call).
> The server handles SIP signaling — no WebSocket/SIP connection is needed from the client.

```
POST /phone-exp/phone-call/v1/dial
```

**Request Body:**
```json
{
  "fromName": "Liora Dermatology & Aesthetics",
  "fromNumber": "2124334569",
  "toNumber": "3302067819",
  "sipProfileId": "c6d657dc-fbdd-47bd-b6e6-bc055dcd3346"
}
```

**Field Notes:**
| Field | Description |
|-------|-------------|
| `fromName` | Caller ID name (practice name) |
| `fromNumber` | Caller ID number — digits only, no `+1` prefix |
| `toNumber` | Destination number — digits only, no `+1` prefix |
| `sipProfileId` | The SIP profile UUID of the originating device/softphone |

**Response:**
```json
{
  "callId": "c75fe52d-1d5c-416d-8202-f61035f1cb3e"
}
```

**Post-dial behavior:**
- The call is initiated server-side; no browser WebSocket/SIP is involved
- Firestore pushes a real-time `CALL_RECORD` notification via the Listen channel
- The call appears in `hydrated-call-records` within seconds
- The web app also fetches person photo/details and phone numbers for the contact flyout

### Health Check

```
GET /phone-exp/phone-call/v1/health-check?locationIds={locationId}
```

**Response:** `{"message":"Success"}`

---

## Call Queues — `/phone-exp/phone-call/v1/`

### List Call Queues

```
POST /phone-exp/phone-call/v1/call-queues
```

### Get Call Queue Metrics

```
POST /phone-exp/phone-call/v1/call-queues/metrics
```

### Subscribe User to Queues

```
POST /phone-exp/phone-call/v1/call-queues/subscribe-to-queues
```

### Call Queue Performance Analytics

```
GET /phone-exp/phone-call/v1/call-queues/performance-analytics-data?locationIds={locationId}
```

```
POST /phone-exp/phone-call/v1/call-queues/performance-analytics-data
```

### Portal Gateway (Legacy?)

```
GET /portal/gateway/phone/callqueue/{queueId}
```

---

## Softphone & SIP — `/phone/`

### Get Softphone Settings

```
GET /phone/softphones/settings?locationIds={locationId}
```

**Response:**
```json
{
  "proxy": "sip-websockets-glb.us1.weavephone.net",
  "softphones": [
    {
      "id": "dd2b2484-f5f0-43d2-8029-9a140f958fed",
      "name": "7018 Genie's Softphone",
      "metricsUrl": "https://events.wprov.net/weave/.../ws",
      "sipProfiles": [
        {
          "id": "c6d657dc-fbdd-47bd-b6e6-bc055dcd3346",
          "name": "7018 Genie's Softphone",
          "username": "phone_7018_57b6",
          "domain": "s00448454.getweave.io",
          "password": "...",
          "e911AddressId": "uuid",
          "extensionNumber": 7018,
          "doNotDisturb": false
        }
      ],
      "parkSlots": [
        {"name": "Hold 1", "number": 1, "uri": "park+6001@s00448454.getweave.io"},
        {"name": "Hold 2", "number": 2, "uri": "park+6002@s00448454.getweave.io"},
        {"name": "Hold 3", "number": 3, "uri": "park+6003@s00448454.getweave.io"}
      ],
      "extensions": [
        {
          "id": "uuid",
          "name": "101 Front Desk #1",
          "number": 101,
          "presenceUri": "phone_101_3675@s00448454.getweave.io",
          "sipProfileId": "uuid"
        }
      ],
      "callWaitingIndicatorBeep": true
    }
  ]
}
```

### SIP Profiles

```
GET /phone/sip-profiles/v1?tenantId={tenantId}
```

**Response includes per-profile:**
- `sipProfileId`, `name`, `device` (type, MAC, model)
- `registration` (active, userAgent, region, lastConnected, expires)
- `extension` (extensionId, extensionNumber)
- `personalVoicemailBoxId`, `parkRingbackEnabled`

```
PUT /phone/sip-profiles/v1/{sipProfileId}
GET /phone/sip-profiles/v1/{sipProfileId}/call-groups
PUT /phone/sip-profiles/v1/{sipProfileId}/call-groups
GET /phone/sip-profiles/v1/{sipProfileId}/registration
GET /phone/sip-profiles/v1/{sipProfileId}/dnd
PUT /phone/sip-profiles/v1/{sipProfileId}/dnd
```

### Tenants

```
GET /phone/tenant/tenants?orgId={locationId}
```

**Response:**
```json
{
  "tenants": [
    {
      "id": "1cdad4ca-9dbe-45f2-8263-c998c1dfec98",
      "name": "Liora Dermatology & Aesthetics",
      "locations": [
        {"id": "d8508d79-c71c-4678-b139-eaedb19c2159", "name": "Liora Dermatology & Aesthetics"}
      ]
    }
  ]
}
```

### User Device Management

```
POST /phone/user/v1/set-active-device
POST /phone/user/v1/deactivate-device
POST /phone/user/v1/forget-device
```

---

## Call Config — `/phone/call-config/v2/`

### Get/Update Call Recording Settings

> Requires admin ACL.

```
GET /phone/call-config/v2/{tenantId}/call-recording
PUT /phone/call-config/v2/{tenantId}/call-recording/update
```

---

## Call Groups — `/phone/callgroup/v1/`

```
POST /phone/callgroup/v1                              — Create
GET  /phone/callgroup/v1/{callGroupId}                — Read
GET  /phone/callgroup/v1                              — List
PUT  /phone/callgroup/v1/{callGroupId}                — Update
DELETE /phone/callgroup/v1/{callGroupId}              — Delete
POST /phone/callgroup/v1/device                       — Assign Device
PUT  /phone/callgroup/v1/device                       — Update Device
PUT  /phone/callgroup/v1/devices                      — Update Devices
DELETE /phone/callgroup/v1/device/{callLegId}         — Remove Device
GET  /phone/callgroup/v1/{callGroupId}/usage          — Usage
```

---

## Phone Numbers — `/phone-exp/phone-numbers/v1/`

```
GET /phone-exp/phone-numbers/v1/sms-phone-numbers?locationIds={locationId}
PUT /phone-exp/phone-numbers/v1/sms-phone-number/{phoneNumberId}
GET /phone-exp/phone-numbers/user-accessible?locationIds={locationId}
GET /phone-exp/phone-numbers/outbound-sms-numbers?locationIds={locationId}
```

---

## Departments — `/phone-exp/departments/v2/`

```
GET /phone-exp/departments/v2/default-sms?locationId={locationId}
```

---

## Real-Time Data: Firestore

Call history data is also pushed in real-time via **Google Firestore** listeners.

- **Project:** `wsf-notification-api-prod`
- **Database:** `(default)`
- **Transport:** Firestore Listen channel over HTTP long-poll
- **Endpoint:** `https://firestore.googleapis.com/google.firestore.v1.Firestore/Listen/channel`
- **Auth:** Firebase auth token (separate from Weave JWT, stored in IndexedDB `firebaseLocalStorageDb`)
- **Firebase API Key:** `AIzaSyBNOMdSSwl1NVkpnbCOQiXNxpGHoJS9noU`
- **Firebase Auth Domain:** `wsf-notification-api-prod.firebaseapp.com`

The webapp subscribes to Firestore for real-time call record updates, then enriches each record by fetching person details via REST API (`GET /persons/v3/persons/{personId}`).

**Notification types pushed via Firestore:**
| Type | Enum Value |
|------|------------|
| `PHONE_CALL` | 1 |
| `SMS` | 2 |
| `FOLLOW_UP` | 3 |
| `VOICEMAIL` | 4 |
| `MISSED_CALL` | 5 |
| `FAX` | 6 |
| `CALL_RECORD` | 27 |
| `VOICEMAIL_TAG` | 28 |
| `CALL_RECORD_TAG` | 29 |

---

## SIP/WebRTC Architecture

The Weave softphone is a **SIP over WebSocket** client:

- **WebSocket Proxy:** `wss://sip-websockets-glb.us1.weavephone.net`
- **SIP Domain:** `s00448454.getweave.io`
- **SIP Username pattern:** `phone_{ext}_{hash}@{domain}`
- **Metrics endpoint:** `https://events.wprov.net/weave/{tenantSlug}/{softphoneId}/ws`
- **Park/Hold URIs:** `park+600{n}@{domain}`
- **Presence URIs:** `phone_{ext}_{hash}@{domain}`

**Device types:**
- `DEVICE_TYPE_DESK_PHONE` (Yealink SIP-T54W, etc.)
- `DEVICE_TYPE_SOFTPHONE` (browser-based)
- `DEVICE_TYPE_MOBILE_APP`

**Device statuses:**
- `DEVICE_STATUS_AVAILABLE`
- `DEVICE_STATUS_OFFLINE`
- `DEVICE_STATUS_ONCALL`
- `DEVICE_STATUS_RINGING`

---

## Call Statuses

| Status | Description |
|--------|-------------|
| `answered` | Call was picked up |
| `missed` | Call rang but no one answered |
| `abandoned` | Caller hung up before answer |
| `voicemail` | Call went to voicemail |
| `forwarded` | Call was forwarded |

---

## Endpoint Summary

### PhoneRecordsAPI (`/phone-exp/phone-records/v1/`)

| Method | Path | Function |
|--------|------|----------|
| GET | `/call-records` | List call history |
| GET | `/hydrated-call-records` | List call history with person data |
| GET | `/hydrated-call-record` | Single hydrated call record |
| GET | `/call-records-by-person-ids` | Call records for specific persons |
| POST | `/call-records/mark-all-viewed` | Mark all viewed |
| POST | `/apply-call-record-tags` | Tag a call record |
| DELETE | `/remove-call-record-tags` | Remove tags |
| GET | `/call-recording/signed-url` | Get recording download URL (ACL required) |
| DELETE | `/call-recordings` | Delete recording |
| GET | `/voicemails` | List voicemails (hydrated) |
| GET | `/voicemail-messages` | List voicemail messages (raw) |
| GET | `/count-unread-voicemails` | Count unread per mailbox |
| GET | `/voicemail-mailboxes` | List voicemail boxes |
| POST | `/apply-voicemail-message-tags` | Tag a voicemail |
| POST | `/remove-voicemail-message-tags` | Remove voicemail tags |
| POST | `/export/call-records` | Start export |
| GET | `/export/call-records/{id}/status` | Export status |
| GET | `/exports/all-call-records` | Export all |
| GET | `/exports/call-records-signed-url` | Export download URL |
| GET | `/widget/call-results` | Call results widget data |
| GET | `/widget/phone-snapshot` | Phone snapshot widget data |

### PhoneCallsAPI (`/phone-exp/phone-call/v1/`)

| Method | Path | Function |
|--------|------|----------|
| GET | `/health-check` | Health check |
| POST | `/dial` | Initiate outbound call |
| POST | `/collect-spans` | Collect telemetry spans |
| POST | `/call-queues` | List call queues |
| POST | `/call-queues/metrics` | Queue metrics |
| POST | `/call-queues/subscribe-to-queues` | Subscribe to queues |
| GET/POST | `/call-queues/performance-analytics-data` | Analytics |

### SipProfileAPI (`/phone/sip-profiles/v1/`)

| Method | Path | Function |
|--------|------|----------|
| GET | `/` | List SIP profiles |
| GET | `/list_by_tenants` | List by tenants |
| PUT | `/{sipProfileId}` | Update profile |
| GET | `/{sipProfileId}/call-groups` | Get call groups |
| PUT | `/{sipProfileId}/call-groups` | Replace call groups |
| GET | `/{sipProfileId}/registration` | Get registration status |
| GET | `/{sipProfileId}/dnd` | Get DND status |
| PUT | `/{sipProfileId}/dnd` | Update DND |

### SoftphonesAPI (`/phone/softphones/`)

| Method | Path | Function |
|--------|------|----------|
| GET | `/settings` | Get softphone config + SIP credentials |

### Other Phone APIs

| Method | Path | Function |
|--------|------|----------|
| GET | `/phone/tenant/tenants?orgId={id}` | Get tenants |
| GET | `/phone/call-config/v2/{tenantId}/call-recording` | Recording config (admin) |
| POST | `/phone/user/v1/set-active-device` | Set active device |
| POST | `/phone/user/v1/deactivate-device` | Deactivate device |
| POST | `/phone/user/v1/forget-device` | Forget device |
| GET | `/phone-exp/voicemail/download/{id}?token={JWT}&location_id={id}` | Download voicemail audio |

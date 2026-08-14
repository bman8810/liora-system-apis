# correlation_id SoT (pointer)

**Canonical doc (Genies Bottle):**  
`../genies-bottle/docs/CORRELATION-ID-SOT.md` (sibling clone under `liora/`)  
or upstream: https://github.com/bman8810/genies-bottle/blob/master/docs/CORRELATION-ID-SOT.md

## Locked rule (Zocdoc new-booking)

```
correlation_id = zocdoc-{appointmentId}
```

Fallback only if appointment id missing: `zocdoc-{mrn}-{YYYY-MM-DD}`.

- Same id on every `report_process` / StepLedger / Weave `relatedIds`
- Messaging/calls hooks: **same root** + step name (no second root id)
- No PHI in the id; never put id in SMS body
- Job: `build_correlation_id` + `validate_correlation_id` in
  `liora_tools/scripts/zocdoc_new_booking.py`

Card: `t_71b53094`

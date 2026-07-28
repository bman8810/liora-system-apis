# After-hours + provider-unavailable scripts (t_7b4b8fc5)

## Tools (Grok Realtime)

| Tool | When |
|------|------|
| `check_office_hours` | Start of after-hours handling; caller asks if open |
| `after_hours_script` | Outside Mon–Sat front-desk hours (or forced path) |
| `provider_unavailable_script` | Requested provider has no useful slots / off / teaching / booked out |

## Hours (America/New_York)

- Mon–Thu 9:00 AM – 6:00 PM  
- Fri 9:00 AM – 4:00 PM  
- Sat 10:00 AM – 4:00 PM  
- Sun Closed  

## After-hours next-best

1. Leave message (confirmed → staff queue `after_hours_message`)  
2. Schedule callback for next open (`after_hours_callback`)  
3. Hours FAQ  
4. **No** staff transfer after hours; **no** clinical advice / same-day MD promise  

## Provider unavailable next-best

1. Alternate provider (filter `zzz*`) → `find_open_slots`  
2. Other times same provider  
3. Callback / leave message (confirmed → queue)  
4. Transfer/hold **only if office open** (`transfer_allowed`)  

## Multi-intent

Always pass `parked_intents`. Tools return `preserve_parked`, `reoffer_speak`.  
Queue payload includes parked list for staff handoff.

## Files

- `voice_agent/clinic_hours.py`  
- `voice_agent/call_scripts.py`  
- `voice_agent/staff_queue.py`  
- Instructions: `SYSTEM_INSTRUCTIONS_SCHEDULING` in `config.py`  
- Wired in `grok_bridge.py`  
- Tests: `tests/test_call_scripts.py`  

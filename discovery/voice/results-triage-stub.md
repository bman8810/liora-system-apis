# Results request triage stub (voice ops)

SoT card: `t_5710abab`

## Goal

Patient asks for lab/test **results** → Genie queues **message MD** or **staff callback**.  
**Never** return raw result values on the voice path.

## Tool

`triage_lab_results` (`voice_agent/ops_tools.py` → `ResultsFlow.request_results_triage`)

| Param | |
|-------|--|
| `patient_id` | optional, from lookup |
| `reason` | caller words (not values) |
| `preferred_callback` | phone/window for callback route |
| `route` | `message_md` (default) \| `callback` |
| `confirmed` | required verbal yes before queue |

## Policy

| Rule | |
|------|--|
| No raw disclosure | default; `LIORA_LAB_RESULTS_DISCLOSE` stays off (out of scope) |
| Writes gate | `EMA_WRITES_ENABLED` |
| Dry-run | `LIORA_VOICE_DRY_RUN=1` → log `intended_queue`, no JSONL write |
| Non-goals | no clinical advice, no billing invent, no PAN |

## Queue

`StaffMessageQueue` → JSONL (`LIORA_STAFF_MESSAGE_QUEUE` or `LIORA_STAFF_QUEUE_PATH`).

| route | kind | audience |
|-------|------|----------|
| message_md | `results` | provider |
| callback | `results_callback` | staff |

## Tests

```bash
cd /path/to/worktree
.venv/bin/pytest tests/test_results_triage.py -q
```

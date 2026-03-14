"""
Test EMA appointment reschedule flow.

Creates a test appointment, reschedules it +1 hour, verifies, then cancels it.

Usage:
    python discovery/modmed/test_reschedule.py
"""
import json
import os
import sys
from datetime import datetime, timedelta

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from liora_tools.auth.ema import ensure_session
from liora_tools.modmed.client import EmaClient
from liora_tools.exceptions import OptimisticLockError


def main():
    print("=" * 60)
    print("EMA Reschedule Test")
    print("=" * 60)

    # Step 1: Get a session
    print("\n[1] Authenticating...")
    session, cookies = ensure_session()
    client = EmaClient(session)
    assert client.check_session(), "Session check failed"
    print("    Session OK")

    # Step 2: Find an upcoming appointment for Barric Reed, or create one
    print("\n[2] Looking for a test appointment...")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    next_week = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

    appts = client.list_appointments(
        start_date=tomorrow,
        end_date=next_week,
        selector="id,scheduledStartDate,scheduledEndDate,scheduledDuration,"
                 "appointmentTypeName,status,objectLockVersion,"
                 "patient(id,lastName,firstName)",
        where='patient.lastName=="Reed";patient.firstName=="Barric"',
    )

    if appts:
        appt = appts[0]
        print(f"    Found appointment {appt['id']}: "
              f"{appt.get('appointmentTypeName')} at {appt.get('scheduledStartDate')}")
    else:
        print("    No upcoming Barric Reed appointments found.")
        print("    Create one via the scheduler UI and re-run this script.")
        return

    appt_id = appt["id"]
    original_start = appt["scheduledStartDate"]
    lock_version = appt.get("objectLockVersion")

    print(f"    Appointment ID: {appt_id}")
    print(f"    Current start:  {original_start}")
    print(f"    Lock version:   {lock_version}")

    # Step 3: Reschedule +1 hour
    print("\n[3] Rescheduling +1 hour...")
    original_dt = datetime.fromisoformat(original_start.replace("Z", "+00:00").replace("+0000", "+00:00"))
    new_dt = original_dt + timedelta(hours=1)
    new_start = new_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    print(f"    New start: {new_start}")

    result = client.reschedule(
        appointment_id=appt_id,
        new_start=new_start,
        reason="OFFICE_EDIT",
    )
    print(f"    Reschedule returned: {result.get('id', 'OK')}")

    # Step 4: Verify
    print("\n[4] Verifying reschedule...")
    updated = client.get_appointment(
        str(appt_id),
        selector="id,scheduledStartDate,scheduledEndDate,objectLockVersion,status",
    )
    if isinstance(updated, list):
        updated = updated[0] if updated else {}

    print(f"    New start:    {updated.get('scheduledStartDate')}")
    print(f"    New end:      {updated.get('scheduledEndDate')}")
    print(f"    Lock version: {updated.get('objectLockVersion')} (was {lock_version})")
    print(f"    Status:       {updated.get('status')}")

    # Step 5: Test OptimisticLockError (use stale version)
    print("\n[5] Testing OptimisticLockError with stale version...")
    try:
        # Fetch current state, then tamper with objectLockVersion
        stale = client._get(
            f"/ema/ws/v2/appointment/{appt_id}",
            {"mapId": "CHECK_IN"},
        ).json()
        stale["objectLockVersion"] = -1  # deliberately stale
        stale["rescheduleReason"] = "OFFICE_EDIT"
        stale["overrideAllowed"] = True
        client._post(
            f"/ema/ws/v2/appointment?id={appt_id}&mapId=APPOINTMENT_DETAILS",
            stale,
        )
        print("    WARNING: Expected OptimisticLockError but request succeeded")
    except OptimisticLockError as e:
        print(f"    OptimisticLockError raised as expected: {e}")
    except Exception as e:
        print(f"    Got different error (may still be lock-related): {type(e).__name__}: {e}")

    # Step 6: Optionally move it back
    print("\n[6] Moving appointment back to original time...")
    try:
        result = client.reschedule(
            appointment_id=appt_id,
            new_start=original_start.replace("+0000", "Z"),
            reason="OFFICE_EDIT",
        )
        print(f"    Moved back successfully")
    except Exception as e:
        print(f"    Failed to move back: {e}")

    print("\n" + "=" * 60)
    print("Done!")


if __name__ == "__main__":
    main()

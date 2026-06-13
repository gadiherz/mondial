"""Verifies the Cloudflare worker's matchAwaitingResult() algorithm against the
real feed, by reimplementing it identically in Python. Run from repo root:
    python cron-worker/verify_gate.py
Not deployed; a one-off correctness check.
"""
import json
from datetime import datetime, timezone

FINISH_AFTER_MIN, GIVE_UP_AFTER_MIN = 110, 8 * 60


def _ms(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000


def awaiting(matches, now_ms):
    """Identical logic to worker.js matchAwaitingResult()."""
    for m in matches:
        if m["status"] == "final" or not m.get("kickoff_utc"):
            continue
        mins = (now_ms - _ms(m["kickoff_utc"])) / 60000
        if FINISH_AFTER_MIN <= mins <= GIVE_UP_AFTER_MIN:
            return f"{m['home']} v {m['away']} ({round(mins)} min)"
    return None


def main():
    feed = json.load(open("web/data/dashboard.json", encoding="utf-8"))
    matches = feed["matches"]
    print("matches:", len(matches),
          "| with kickoff:", sum(1 for m in matches if m.get("kickoff_utc")))
    now = datetime.now(timezone.utc).timestamp() * 1000
    print("at now -> dispatch?", bool(awaiting(matches, now)), "|", awaiting(matches, now))

    # Simulate one finished match (US 01:00Z) being unfinalised, to prove the window.
    sim = [dict(m, status="scheduled") if "United States" in m["home"] else m
           for m in matches]
    ko = _ms("2026-06-13T01:00:00Z")
    checks = [
        ("kickoff +10min  -> should NOT dispatch", ko + 10 * 60000, False),
        ("kickoff +120min -> SHOULD dispatch (US)", ko + 120 * 60000, True),
        ("kickoff +9h     -> should NOT dispatch", ko + 9 * 3600000, False),
    ]
    ok = True
    for label, t, expect in checks:
        got = bool(awaiting(sim, t))
        status = "PASS" if got == expect else "FAIL"
        if got != expect:
            ok = False
        print(f"  [{status}] {label}: dispatch={got}")
    print("ALL PASS" if ok else "SOME FAILED")


if __name__ == "__main__":
    main()

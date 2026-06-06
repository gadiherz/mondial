"""Bundle the WC-2026 flag SVGs locally (one-time asset build).

Downloads each team's flag from the MIT-licensed `flag-icons` project into
web/assets/flags/<code>.svg, so the site serves flags from its own origin (no
external requests at runtime -> CSP stays locked to 'self').

    PYTHONPATH=src python scripts/fetch_flags.py
"""
import sys
from pathlib import Path

import requests

from mondial.config import ROOT
from mondial.data.countries import TEAM_CODES

BASE = "https://raw.githubusercontent.com/lipis/flag-icons/main/flags/4x3"
OUT = ROOT / "web" / "assets" / "flags"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    codes = sorted(set(TEAM_CODES.values()))
    ok = fail = 0
    for code in codes:
        dest = OUT / f"{code}.svg"
        if dest.exists():
            ok += 1
            continue
        try:
            r = requests.get(f"{BASE}/{code}.svg", timeout=30)
            r.raise_for_status()
            dest.write_text(r.text, encoding="utf-8")
            ok += 1
        except requests.RequestException as e:
            fail += 1
            print(f"  FAILED {code}: {e}")
    print(f"flags: {ok}/{len(codes)} present in {OUT} ({fail} failed)")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())

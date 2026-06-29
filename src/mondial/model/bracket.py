"""WC-2026 knockout bracket: group standings -> qualification -> knockout fixtures.

The DB ships only the 72 group-stage fixtures (`matches`, tournament='WC',
bracket_no IS NULL). This module computes the group tables once results are final,
selects who advances (top 2 per group + the 8 best third-placed teams), resolves
the official Round-of-32 bracket, and writes the knockout fixtures into `matches`
(status='scheduled', stage + bracket_no set) so the model predicts them and the
six-player sim bets them. As each knockout round finalises, the next round's
fixtures are generated from the actual winners.

Official structure (FIFA, 48-team format; verified against the DB draw):
  * 12 groups A-L of 4 -> 24 group winners/runners-up + 8 best thirds = R32 (32).
  * R32 match numbers 73-88, R16 89-96, QF 97-100, SF 101-102, 3rd-place 103,
    Final 104 (`R32_SLOTS` + `KO_TREE`).

Third-place allocation: FIFA's regulations carry a 495-row table ("Annex C") --
one row per possible combination of the 8 qualifying groups -- mapping each
qualifying third to a specific R32 slot. We do NOT transcribe all 495 rows. Instead
we compute a VALID bracket-consistent assignment via bipartite matching of the
qualifying-third groups to the eight thirds-slots (each slot admits thirds from a
fixed set of groups). Where FIFA's table and a valid matching can differ (the
matching is not always unique), provide the official mapping for the actual
qualifying combination via `THIRD_PLACE_OVERRIDE` once the group stage ends -- only
ONE combination ever occurs, so a single row makes it FIFA-exact.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

log = logging.getLogger("bracket")

# Scope every group-stage query to the 2026 tournament by date -- the DB also holds
# historical WC finals (2014/2018/2022, tournament='WC'), which must NOT be counted
# as 2026 group results. The 2026 fixtures all fall on/after this date.
WC2026_START = "2026-06-01"

# --- Official group draw (DB canonical names; verified == DB fixture cliques) ---
GROUPS: dict[str, tuple[str, ...]] = {
    "A": ("Mexico", "South Africa", "South Korea", "Czech Republic"),
    "B": ("Canada", "Bosnia and Herzegovina", "Qatar", "Switzerland"),
    "C": ("Brazil", "Morocco", "Haiti", "Scotland"),
    "D": ("United States", "Paraguay", "Australia", "Turkey"),
    "E": ("Germany", "Curaçao", "Ivory Coast", "Ecuador"),
    "F": ("Netherlands", "Japan", "Sweden", "Tunisia"),
    "G": ("Belgium", "Egypt", "Iran", "New Zealand"),
    "H": ("Spain", "Cape Verde", "Saudi Arabia", "Uruguay"),
    "I": ("France", "Senegal", "Iraq", "Norway"),
    "J": ("Argentina", "Algeria", "Austria", "Jordan"),
    "K": ("Portugal", "DR Congo", "Uzbekistan", "Colombia"),
    "L": ("England", "Croatia", "Ghana", "Panama"),
}

# Round-of-32 slot definitions. A slot is ("1", g) = winner of group g, ("2", g) =
# runner-up, ("3", "<allowed letters>") = a best third from one of those groups.
R32_SLOTS: dict[int, tuple[tuple[str, str], tuple[str, str]]] = {
    73: (("2", "A"), ("2", "B")),
    74: (("1", "E"), ("3", "ABCDF")),
    75: (("1", "F"), ("2", "C")),
    76: (("1", "C"), ("2", "F")),
    77: (("1", "I"), ("3", "CDFGH")),
    78: (("2", "E"), ("2", "I")),
    79: (("1", "A"), ("3", "CEFHI")),
    80: (("1", "L"), ("3", "EHIJK")),
    81: (("1", "D"), ("3", "BEFIJ")),
    82: (("1", "G"), ("3", "AEHIJ")),
    83: (("2", "K"), ("2", "L")),
    84: (("1", "H"), ("2", "J")),
    85: (("1", "B"), ("3", "EFGIJ")),
    86: (("1", "J"), ("2", "H")),
    87: (("1", "K"), ("3", "DEIJL")),
    88: (("2", "D"), ("2", "G")),
}
# The eight R32 matches whose second slot is a third-placed team.
THIRDS_SLOTS: dict[int, frozenset] = {
    mno: frozenset(b[1]) for mno, (_a, b) in R32_SLOTS.items() if b[0] == "3"
}

# Knockout tree for R16..Final. Each ref is ("W", match) winner or ("L", match) loser.
KO_TREE: dict[int, tuple[tuple[str, int], tuple[str, int]]] = {
    89: (("W", 74), ("W", 77)),
    90: (("W", 73), ("W", 75)),
    91: (("W", 76), ("W", 78)),
    92: (("W", 79), ("W", 80)),
    93: (("W", 83), ("W", 84)),
    94: (("W", 81), ("W", 82)),
    95: (("W", 86), ("W", 88)),
    96: (("W", 85), ("W", 87)),
    97: (("W", 89), ("W", 90)),
    98: (("W", 93), ("W", 94)),
    99: (("W", 91), ("W", 92)),
    100: (("W", 95), ("W", 96)),
    101: (("W", 97), ("W", 98)),
    102: (("W", 99), ("W", 100)),
    103: (("L", 101), ("L", 102)),   # third-place play-off
    104: (("W", 101), ("W", 102)),   # final
}

# Stage label + representative date per bracket number (real FIFA windows; exact
# per-match dates aren't needed for prediction -- they drive idle-days features and
# ordering only).
def _stage_of(no: int) -> str:
    if 73 <= no <= 88:
        return "R32"
    if 89 <= no <= 96:
        return "R16"
    if 97 <= no <= 100:
        return "QF"
    if 101 <= no <= 102:
        return "SF"
    return "3P" if no == 103 else "F"

_STAGE_DATE = {"R32": "2026-06-30", "R16": "2026-07-04", "QF": "2026-07-09",
               "SF": "2026-07-14", "3P": "2026-07-18", "F": "2026-07-19"}

# FIFA-exact override: {frozenset(qualifying third groups): {group: match_no}}.
# The bipartite matching in `_match_thirds` finds *a* valid thirds->slot assignment, but
# not necessarily FIFA's official Annex-C one -- when they differ, the generated R32 has
# the right teams in the wrong slots, producing fixtures that never happen (so they go
# unpriced and the bracket stalls). Register the official mapping for the ACTUAL
# qualifying combination once the group stage ends -- only ONE combination ever occurs.
#
# 2026 combination (verified against the real Round-of-32 draw): the qualifying thirds are
# from groups B,D,E,F,I,J,K,L. The bipartite default placed B/D/F in slots 74/77/81 the
# wrong way round (Germany-Bosnia/France-Paraguay/USA-Sweden); the official draw is
# Germany-Paraguay(74)/France-Sweden(77)/USA-Bosnia(81). E,I,J,K,L are unchanged.
THIRD_PLACE_OVERRIDE: dict[frozenset, dict[str, int]] = {
    frozenset("BDEFIJKL"): {"B": 81, "D": 74, "E": 79, "F": 77,
                            "I": 82, "J": 85, "K": 80, "L": 87},
}


# --------------------------------------------------------------------------- #
# Standings
# --------------------------------------------------------------------------- #
@dataclass
class TeamRecord:
    team: str
    played: int = 0
    w: int = 0
    d: int = 0
    l: int = 0
    gf: int = 0
    ga: int = 0
    opp_goals: dict = field(default_factory=dict)  # team -> (gf, ga) for head-to-head

    @property
    def pts(self) -> int:
        return 3 * self.w + self.d

    @property
    def gd(self) -> int:
        return self.gf - self.ga


def _group_results(conn: sqlite3.Connection, teams: tuple[str, ...]) -> list[TeamRecord]:
    """Build each team's record from its FINAL group matches (bracket_no IS NULL)."""
    recs = {t: TeamRecord(t) for t in teams}
    name_id = {r["name"]: r["team_id"] for r in conn.execute(
        "SELECT team_id, name FROM teams WHERE name IN (%s)" %
        ",".join("?" * len(teams)), teams)}
    ids = tuple(name_id[t] for t in teams if t in name_id)
    if len(ids) < len(teams):
        missing = [t for t in teams if t not in name_id]
        raise LookupError(f"group teams missing from DB: {missing}")
    id_name = {v: k for k, v in name_id.items()}
    rows = conn.execute(
        """SELECT home_id, away_id, home_goals, away_goals FROM matches
           WHERE tournament='WC' AND status='final' AND bracket_no IS NULL
             AND date >= ? AND home_id IN (%s) AND away_id IN (%s)
             AND home_goals IS NOT NULL AND away_goals IS NOT NULL"""
        % (",".join("?" * len(ids)), ",".join("?" * len(ids))),
        (WC2026_START, *ids, *ids),
    ).fetchall()
    for r in rows:
        h, a = id_name[r["home_id"]], id_name[r["away_id"]]
        hg, ag = int(r["home_goals"]), int(r["away_goals"])
        for t, gf, ga in ((h, hg, ag), (a, ag, hg)):
            rec = recs[t]
            rec.played += 1
            rec.gf += gf
            rec.ga += ga
            rec.w += gf > ga
            rec.d += gf == ga
            rec.l += gf < ga
        recs[h].opp_goals[a] = (hg, ag)
        recs[a].opp_goals[h] = (ag, hg)
    return list(recs.values())


def _h2h_key(rec: TeamRecord, tied: list[str]) -> tuple:
    """Head-to-head (points, GD, GF) restricted to matches among the `tied` teams."""
    pts = gd = gf = 0
    for opp in tied:
        if opp == rec.team or opp not in rec.opp_goals:
            continue
        f, a = rec.opp_goals[opp]
        gf += f
        gd += f - a
        pts += 3 if f > a else 1 if f == a else 0
    return (pts, gd, gf)


def rank_group(records: list[TeamRecord]) -> list[TeamRecord]:
    """FIFA order: points, GD, GF; ties broken by head-to-head (pts, GD, GF) among
    the exactly-tied teams; then overall GF; then team name (stable stand-in for the
    unmodelled fair-play / drawing-of-lots final criteria)."""
    base = sorted(records, key=lambda r: (r.pts, r.gd, r.gf), reverse=True)
    out: list[TeamRecord] = []
    i = 0
    while i < len(base):
        j = i
        while (j + 1 < len(base)
               and (base[j + 1].pts, base[j + 1].gd, base[j + 1].gf)
               == (base[i].pts, base[i].gd, base[i].gf)):
            j += 1
        block = base[i:j + 1]
        if len(block) > 1:
            names = [r.team for r in block]
            block.sort(key=lambda r: (*_h2h_key(r, names), r.gf, _neg_name(r.team)),
                       reverse=True)
        out.extend(block)
        i = j + 1
    return out


def _neg_name(name: str) -> tuple:
    # Make alphabetical order act as a *descending*-sort tiebreak (earlier name wins).
    return tuple(-ord(c) for c in name)


def compute_standings(conn: sqlite3.Connection) -> dict[str, list[TeamRecord]]:
    """Ranked records per group (index 0=winner .. 3=fourth)."""
    return {g: rank_group(_group_results(conn, teams)) for g, teams in GROUPS.items()}


def best_thirds(standings: dict[str, list[TeamRecord]]) -> list[tuple[str, TeamRecord]]:
    """The eight best third-placed teams as (group_letter, record), best first.

    Cross-group ranking: points, GD, GF, then group letter (stable stand-in for
    fair-play / lots). Head-to-head does not apply across groups.
    """
    thirds = [(g, recs[2]) for g, recs in standings.items() if len(recs) >= 3]
    thirds.sort(key=lambda gr: (gr[1].pts, gr[1].gd, gr[1].gf, _neg_name(gr[0])),
                reverse=True)
    return thirds[:8]


# --------------------------------------------------------------------------- #
# Qualification -> R32 slot resolution
# --------------------------------------------------------------------------- #
def _match_thirds(qual_groups: frozenset) -> dict[str, int]:
    """Assign the 8 qualifying third-group letters to the 8 thirds R32 matches.

    Uses FIFA's exact override for this combination if registered; otherwise finds a
    valid bipartite matching (group g -> match m iff g in THIRDS_SLOTS[m]).
    """
    if qual_groups in THIRD_PLACE_OVERRIDE:
        return dict(THIRD_PLACE_OVERRIDE[qual_groups])

    slots = sorted(THIRDS_SLOTS)                  # deterministic slot order
    groups = sorted(qual_groups)                  # deterministic group order
    match_to_group: dict[int, str] = {}

    def assign(gi: int) -> bool:
        if gi == len(groups):
            return True
        g = groups[gi]
        for m in slots:
            if g in THIRDS_SLOTS[m] and m not in match_to_group:
                match_to_group[m] = g
                if assign(gi + 1):
                    return True
                del match_to_group[m]
        return False

    if not assign(0):
        raise ValueError(f"no valid thirds matching for groups {sorted(qual_groups)}")
    return {g: m for m, g in match_to_group.items()}


def resolve_r32(conn: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    """Resolve every R32 match (73-88) to (home_team, away_team).

    Requires all 72 group matches final. The first slot is treated as home (neutral
    venues -> orientation is cosmetic; predict_match uses neutral=True for WC).
    """
    standings = compute_standings(conn)
    winners = {g: recs[0].team for g, recs in standings.items()}
    runners = {g: recs[1].team for g, recs in standings.items()}
    thirds = best_thirds(standings)
    qual_groups = frozenset(g for g, _ in thirds)
    third_by_group = {g: rec.team for g, rec in thirds}
    group_to_match = _match_thirds(qual_groups)
    third_team_by_match = {m: third_by_group[g] for g, m in group_to_match.items()}

    def team_of(slot: tuple[str, str], match_no: int) -> str:
        kind, ref = slot
        if kind == "1":
            return winners[ref]
        if kind == "2":
            return runners[ref]
        return third_team_by_match[match_no]   # kind == "3"

    out = {}
    for mno, (sa, sb) in R32_SLOTS.items():
        out[mno] = (team_of(sa, mno), team_of(sb, mno))
    return out


# --------------------------------------------------------------------------- #
# Fixture generation
# --------------------------------------------------------------------------- #
def _winner_loser(conn, bracket_no: int) -> tuple[str | None, str | None]:
    """(winner, loser) team names of a final knockout match, or (None, None).

    Advancement is decided AFTER extra time / penalties, so it reads `matches.winner_id`
    (set by results.resolve_knockout_winners: the goal winner, or the penalty-shootout
    winner for a level tie). Falls back to a decisive goal score; a level score with no
    winner_id yet is DEFERRED -> (None, None), and the caller retries next run. (The 1X2
    bet settles separately on the 90' result -- see eval/betting + matches.reg_result.)
    """
    r = conn.execute(
        """SELECT th.name h, ta.name a, m.home_goals hg, m.away_goals ag, m.status,
                  m.winner_id, m.home_id, m.away_id
           FROM matches m JOIN teams th ON th.team_id=m.home_id
           JOIN teams ta ON ta.team_id=m.away_id
           WHERE m.bracket_no=?""", (bracket_no,)).fetchone()
    if r is None or r["status"] != "final" or r["hg"] is None or r["ag"] is None:
        return None, None
    if r["winner_id"] is not None:
        if r["winner_id"] == r["home_id"]:
            return r["h"], r["a"]
        if r["winner_id"] == r["away_id"]:
            return r["a"], r["h"]
        log.warning("bracket %d: winner_id %s matches neither side -- deferring.",
                    bracket_no, r["winner_id"])
        return None, None
    if r["hg"] == r["ag"]:
        log.warning("bracket %d level at %d-%d and winner_id not set yet -- cannot "
                    "advance; results.resolve_knockout_winners will fill it from the "
                    "penalty result (or set it via scripts/set_knockout_result.py).",
                    bracket_no, r["hg"], r["ag"])
        return None, None
    return (r["h"], r["a"]) if r["hg"] > r["ag"] else (r["a"], r["h"])


def _insert_fixture(conn, bracket_no: int, home: str, away: str) -> bool:
    """Insert one knockout fixture (idempotent via the unique bracket_no index)."""
    ids = {r["name"]: r["team_id"] for r in conn.execute(
        "SELECT team_id, name FROM teams WHERE name IN (?, ?)", (home, away))}
    if home not in ids or away not in ids:
        log.warning("bracket %d: team id missing for %s/%s", bracket_no, home, away)
        return False
    stage = _stage_of(bracket_no)
    cur = conn.execute(
        """INSERT OR IGNORE INTO matches
           (date, home_id, away_id, tournament, neutral, status, stage, bracket_no)
           VALUES (?, ?, ?, 'WC', 1, 'scheduled', ?, ?)""",
        (_STAGE_DATE[stage], ids[home], ids[away], stage, bracket_no))
    return cur.rowcount > 0


def generate_knockout(conn: sqlite3.Connection) -> int:
    """Create whatever knockout fixtures are now determined. Returns count inserted.

    R32 is created once all 72 group matches are final; each later-round fixture is
    created once both its feeder matches are final. Idempotent (re-runs add only
    newly-determined fixtures). A no-op before the group stage completes.
    """
    n_group_final = conn.execute(
        "SELECT COUNT(*) n FROM matches WHERE tournament='WC' AND bracket_no IS NULL "
        "AND status='final' AND date >= ?", (WC2026_START,)).fetchone()["n"]
    inserted = 0

    if n_group_final >= 72:                       # group stage complete -> R32
        for mno, (home, away) in resolve_r32(conn).items():
            inserted += _insert_fixture(conn, mno, home, away)
    else:
        log.info("generate_knockout: group stage not complete (%d/72 final); "
                 "no R32 yet.", n_group_final)
        return 0

    # R16 -> Final: create a fixture once both feeders are final.
    existing = {r["bracket_no"] for r in conn.execute(
        "SELECT bracket_no FROM matches WHERE bracket_no IS NOT NULL")}
    for mno, (ref_a, ref_b) in KO_TREE.items():
        if mno in existing:
            continue
        wa, la = _winner_loser(conn, ref_a[1])
        wb, lb = _winner_loser(conn, ref_b[1])
        team_a = wa if ref_a[0] == "W" else la
        team_b = wb if ref_b[0] == "W" else lb
        if team_a and team_b:
            inserted += _insert_fixture(conn, mno, team_a, team_b)

    log.info("generate_knockout: %d new knockout fixtures inserted", inserted)
    return inserted


def validate_groups(conn: sqlite3.Connection) -> None:
    """Assert the GROUPS constant matches the DB's group-stage fixture cliques.

    Guards against a stale draw: every group's four teams must all face each other
    in scheduled/final group matches. Raises on mismatch (loud, never silent).
    """
    from collections import defaultdict
    adj = defaultdict(set)
    for r in conn.execute(
        """SELECT th.name h, ta.name a FROM matches m
           JOIN teams th ON th.team_id=m.home_id JOIN teams ta ON ta.team_id=m.away_id
           WHERE m.tournament='WC' AND m.bracket_no IS NULL AND m.date >= ?""",
            (WC2026_START,)):
        adj[r["h"]].add(r["a"]); adj[r["a"]].add(r["h"])
    for g, teams in GROUPS.items():
        for t in teams:
            others = set(teams) - {t}
            if not others <= adj.get(t, set()):
                raise ValueError(
                    f"GROUPS[{g}] mismatch: {t} does not face all of {others} in the "
                    f"DB fixtures (got {adj.get(t, set())}). The draw/labels are stale.")

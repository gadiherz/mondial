"""martj42 shootouts.csv parsing -> penalty-winner lookup by unordered team pair.

Rows are real historical shootouts (cf. test_winner_odds using captured markup); the
fetch is monkeypatched so the test never hits the network.
"""
from datetime import datetime

from mondial.scrapers import historical_results as hr

_CSV = (
    "date,home_team,away_team,winner,first_shooter\n"
    "2018-07-01,Croatia,Denmark,Croatia,Croatia\n"
    "2022-12-09,Netherlands,Argentina,Argentina,\n"
    "2010-07-02,Uruguay,Ghana,Uruguay,\n"            # before the 2014 cutoff
)


class _Resp:
    text = _CSV

    def raise_for_status(self):
        pass


def test_fetch_shootouts_keyed_by_pair(monkeypatch):
    monkeypatch.setattr(hr.requests, "get", lambda *a, **k: _Resp())
    out = hr.HistoricalResultsScraper().fetch_shootouts(since=datetime(2014, 1, 1))
    # Orientation-independent key; martj42 lists Netherlands as home, our query may pass
    # either order.
    assert out[frozenset(("Argentina", "Netherlands"))] == "Argentina"
    assert out[frozenset(("Croatia", "Denmark"))] == "Croatia"
    # The 2010 row is excluded by the since-cutoff.
    assert frozenset(("Uruguay", "Ghana")) not in out

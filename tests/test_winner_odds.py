"""Winner-odds parser + Hebrew-mapping tests.

The positive card is REAL markup captured from the bankerim Winner-Line page after
the 2026-06 aggregator redesign (Greece-Italy international friendly): the kickoff
datetime now lives in the `data-original-title` tooltip of `<span class="time">`
(the span text is the live status), the odds are nested in a `<span class="box-
colors">` wrapper, and a round-id tooltip sits on the same card. The negative
cards exercise the sport/market filters with minimal representative markup.
"""
from mondial.scrapers.winner_odds import he_to_english, parse_cards

# Real card from bankerim.co.il (Winner-Line), whitespace preserved as served. The
# round-id span carries its OWN data-original-title ("תוכניה מס' ..."), so this also
# guards that the time regex anchors on class="time" and never grabs that tooltip.
_REAL_FOOTBALL = (
    '<div class="game is-father colorB  disableBtns closedEvent hasExtraData"  '
    'data-info-peroid="90 דק\'"  data-event-type="2X1" data-info-id="496880" '
    'data-info-roundid="2626010100" data-info-eventid="1338583" data-sportid="2" '
    'data-rankpower=\'["-1.40"]\'   > '
    '<span class="favorite"><i class="fa fa-star  "></i></span> '
    '<span class="round-id" data-toggle="tooltip" data-placement="top" title="" '
    'data-original-title="תוכניה מס\' 2626010100"><strong>2.</strong></span> '
    '<span class="status"><i class="bullet green"></i>'
    '<span data-toggle="tooltip" data-placement="top" title="" '
    'data-original-title="22:00 07.06.2026" class="time">ראשון 22:00</span></span> '
    '<span class="desc"  >יוון - איטליה</span> '
    '<span class="s-d" data-s="1" data-d="1">S,D</span> '
    '<span class="bet-home win"> <span class="box-colors"><span data-rate-change=\'0\'>1.80</span></span> </span> '
    '<span class="bet-x "> <span class="box-colors"><span data-rate-change=\'0\'>3.00</span></span> </span> '
    '<span class="bet-guest "> <span class="box-colors"><span data-rate-change=\'0\'>2.80</span></span> </span>'
)
# Basketball (sport != 2) -> filtered out.
_NON_FOOTBALL = (
    '<div class="game" data-event-type="2X1" data-sportid="3"> '
    '<span data-original-title="20:00 07.06.2026" class="time">x</span> '
    '<span class="desc">קבוצה א - קבוצה ב</span> '
    '<span class="bet-home ">1.50</span><span class="bet-x ">3.00</span><span class="bet-guest ">2.50</span></div>'
)
# Football but a derivative market (handicap suffix in desc) -> filtered out.
_HANDICAP = (
    '<div class="game" data-event-type="2X1" data-sportid="2"> '
    '<span data-original-title="20:00 07.06.2026" class="time">x</span> '
    '<span class="desc">יוון (1+) - איטליה</span> '
    '<span class="bet-home ">1.50</span><span class="bet-x ">3.00</span><span class="bet-guest ">2.50</span></div>'
)


def test_parse_extracts_real_football_1x2():
    recs = parse_cards(_REAL_FOOTBALL)
    assert len(recs) == 1
    r = recs[0]
    assert r["home_he"] == "יוון" and r["away_he"] == "איטליה"
    assert (r["odd_home"], r["odd_draw"], r["odd_away"]) == (1.80, 3.00, 2.80)
    # kickoff tooltip "22:00 07.06.2026" -> ISO; the round-id tooltip is ignored
    assert r["commence"] == "2026-06-07T22:00:00"


def test_parse_filters_non_football_and_derivatives():
    assert parse_cards(_NON_FOOTBALL) == []     # basketball dropped
    assert parse_cards(_HANDICAP) == []         # handicap market dropped
    # all three together -> only the clean football 1X2 survives
    assert len(parse_cards(_REAL_FOOTBALL + _NON_FOOTBALL + _HANDICAP)) == 1


def test_he_to_english_mapping_and_geresh():
    assert he_to_english("יוון") == "Greece"
    assert he_to_english("איטליה") == "Italy"
    assert he_to_english("צ'ילה") == "Chile"      # geresh normalised both sides
    assert he_to_english("נורבגיה") == "Norway"   # spelling variant
    assert he_to_english("קאפו ורדה") == "Cape Verde"  # 2026-06 site spelling
    assert he_to_english("ריאל מדריד ב'") is None  # a club -> not a national team

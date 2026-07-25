"""Тесты парсера alpinbet: строки прогнозов и таблицы прибыли.

Ловим поломку, когда alpinbet поменяет вёрстку — тогда тесты падают, а не тихо
приходит 0 сигналов в проде.
"""
from alpine_lite.alpinbet import (AlpinbetClient, _parse_profit_units,
                                   parse_day_label, parse_month_label)

FORECAST_HTML = """
<div id="pjax-forecast-list">
  <div class="rTableLine table-header"><div class="cell-team-title">шапка</div></div>
  <div class="rTableLine">
    <div class="cell-icon js-sport-tooltip" data-tippy-content="Футбол"></div>
    <div class="cell-team-title"><a href="/forecast/soccer/174315296-spain-argentina">м</a></div>
    <div class="time-event">Лайв<span>90'</span></div>
    <span class="js-current-minutes">90</span>
    <div class="cell-team-command">Spain</div>
    <div class="cell-team-command">Argentina</div>
    <div class="cell-team-score">1:2</div>
    <div class="cell-team-tnm">FIFA - World Cup</div>
    <div class="cell-type"><span class="info-help">Основная игра Ничья</span></div>
    <div class="cell-coefficient__total">2.43</div>
    <div class="cell-prognos">
      <img class="img-light" data-src="/images/photo/Forecast/174315296/hash.png">
    </div>
  </div>
  <div class="rTableLine">
    <div class="cell-team-title"><a href="/forecast/soccer/174315000-a-b">м</a></div>
    <div class="cell-team-command">A</div>
    <div class="cell-team-command">B</div>
    <span class="lose">-1 000 o</span>
  </div>
</div>
"""


def test_parse_rows_active_and_settled():
    rows = AlpinbetClient.parse_rows(FORECAST_HTML)
    assert len(rows) == 2

    active = rows[0]
    assert active.forecast_id == "174315296"
    assert active.sport == "Футбол"
    assert active.home_team == "Spain"
    assert active.away_team == "Argentina"
    assert active.league == "FIFA - World Cup"
    assert active.coefficient == "2.43"
    assert active.score == "1:2"
    assert active.bet_type == "Основная игра Ничья"
    assert active.image_url.endswith("/images/photo/Forecast/174315296/hash.png")
    assert active.settled is False

    settled = rows[1]
    assert settled.forecast_id == "174315000"
    assert settled.settled is True
    assert settled.outcome == "lose"
    assert settled.profit_units == -1000


# Новая вёрстка alpinbet для рассчитанных прогнозов: ссылка в .cell-oboroty,
# счёт в .cell-count, коэф. в .completed_rate_desc, итог в .cell-subscribers span
# (выигрыш — БЕЗ класса, проигрыш class="lose", возврат class="return").
SETTLED_NEW_HTML = """
<div id="pjax-forecast-list">
  <div class="rTableLine table-header"><div class="cell-oboroty">Матч</div></div>
  <div class="rTableLine">
    <div class="cell-icon js-sport-tooltip" data-tippy-content="Баскетбол"></div>
    <div class="cell-oboroty"><a href="/forecast/basketball/192131355-negeri-sembilan-vs-sabah-23-07-2026">
      <span class="time-event">23.07.2026, 11:00</span>
      <span class="cell-team-command">Negeri Sembilan</span>
      <span class="cell-team-command">Sabah</span>
      <span class="cell-team-tnm">Malaysia. MABA Cup</span>
    </a></div>
    <div class="cell-prognos"><div class="completed_rate_desc"><div class="rate">2.00</div></div>
      <div class="rate-description">с ОТ. ТМ (154.5)</div></div>
    <div class="cell-count">105:45</div>
    <div class="cell-subscribers cell-subscribers-forcast"><span class="">1 000<span class="rouble">o</span></span></div>
  </div>
  <div class="rTableLine">
    <div class="cell-oboroty"><a href="/forecast/basketball/192131300-a-vs-b-23-07-2026">
      <span class="cell-team-command">A</span><span class="cell-team-command">B</span></a></div>
    <div class="cell-prognos"><div class="completed_rate_desc"><div class="rate">1.85</div></div></div>
    <div class="cell-count">70:90</div>
    <div class="cell-subscribers"><span class="lose">-1 000<span class="rouble">o</span></span></div>
  </div>
  <div class="rTableLine">
    <div class="cell-oboroty"><a href="/forecast/basketball/192131301-c-vs-d-23-07-2026">
      <span class="cell-team-command">C</span><span class="cell-team-command">D</span></a></div>
    <div class="cell-prognos"><div class="completed_rate_desc"><div class="rate">1.90</div></div></div>
    <div class="cell-count">2:2</div>
    <div class="cell-subscribers"><span class="return">0<span class="rouble">o</span></span></div>
  </div>
</div>
"""


def test_parse_rows_new_layout_settled():
    rows = AlpinbetClient.parse_rows(SETTLED_NEW_HTML)
    assert len(rows) == 3
    win, lose, ret = rows

    assert win.forecast_id == "192131355"
    assert win.sport == "Баскетбол"
    assert win.home_team == "Negeri Sembilan"
    assert win.away_team == "Sabah"
    assert win.league == "Malaysia. MABA Cup"
    assert win.coefficient == "2.00"
    assert win.bet_type == "с ОТ. ТМ (154.5)"
    assert win.score == "105:45"
    assert win.settled is True
    assert win.outcome == "win"
    assert win.profit_units == 1000

    assert lose.settled is True and lose.outcome == "lose" and lose.profit_units == -1000
    assert ret.settled is True and ret.outcome == "return" and ret.profit_units == 0


STATS_HTML = """
<div id="tab-day">
  <div class="rTableLine table-header">
    <div class="cell-month">Дата</div>
  </div>
  <div class="rTableLine">
    <div class="cell-month">19.07.2026</div>
    <div class="cell-win">5 2 1</div>
    <div class="cell-roi">+3.50%</div>
    <div class="cell-subscribers">+3 500 o</div>
  </div>
</div>
"""


def test_parse_stat_table():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(STATS_HTML, "lxml")
    rows = AlpinbetClient._parse_stat_table(soup, "tab-day")
    assert len(rows) == 1
    r = rows[0]
    assert r.label == "19.07.2026"
    assert (r.win, r.lose, r.ret) == (5, 2, 1)
    assert r.profit_percent == "+3.50%"
    assert r.profit_units == 3500


def test_parse_profit_units():
    assert _parse_profit_units("-1 000 o") == -1000
    assert _parse_profit_units("+950 o") == 950
    assert _parse_profit_units("") == 0
    assert _parse_profit_units("нет числа") == 0


def test_parse_day_label():
    d = parse_day_label("19.07.2026")
    assert d is not None and (d.year, d.month, d.day) == (2026, 7, 19)
    assert parse_day_label("мусор") is None


def test_parse_month_label():
    assert parse_month_label("Июл 2026") == (2026, 7)
    assert parse_month_label("Дек 2025") == (2025, 12)
    assert parse_month_label("плохо") is None

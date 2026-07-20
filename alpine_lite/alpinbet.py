"""
Парсер alpinbet.com БЕЗ браузера (чистый requests + BeautifulSoup).

Вход (Yii2):
  1. GET /            -> кука _csrf-frontend + <meta csrf-token>
  2. POST /site/login -> куки advanced + valid_user (ОБЫЧНЫЙ POST, без X-Requested-With,
     иначе Yii вернёт только ajax-валидацию и не залогинит)
  3. GET PJAX рассылки -> HTML-фрагмент со строками прогнозов (лёгкий, для опроса 10 c)
  4. GET страницы рассылки целиком -> таблицы прибыли День/Месяц/Год (для отчётов)

Один экземпляр AlpinbetClient = одна сессия одного ЛК; сессия переиспользуется,
при протухании автоматически перелогинивается.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://alpinbet.com"
LOGIN_URL = f"{BASE}/site/login"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) "
      "Gecko/20100101 Firefox/151.0")

DEFAULT_ACTIVE_TAB = "1"  # таб активных + завершённых за день

RUS_MONTH = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
RUS_MONTH_SHORT = {v: k.capitalize() for k, v in RUS_MONTH.items()}


class AlpinbetAuthError(RuntimeError):
    """Не удалось авторизоваться (неверный логин/пароль/капча)."""


@dataclass
class ParsedForecast:
    forecast_id: str
    sport: Optional[str]
    status: Optional[str]
    minute: Optional[str]
    score: Optional[str]
    home_team: Optional[str]
    away_team: Optional[str]
    league: Optional[str]
    bet_type: Optional[str]
    coefficient: Optional[str]
    url: str
    image_url: str = ""
    settled: bool = False
    outcome: Optional[str] = None       # win / lose / return
    profit_units: int = 0               # знаковая сумма (база 1000 = 1%)


@dataclass
class StatRow:
    label: str          # дата "10.06.2026" / месяц "Июн 2026" / год "2026"
    win: int
    lose: int
    ret: int
    profit_percent: str  # "Доход." — заголовочный процент
    profit_units: int


def _txt(node) -> Optional[str]:
    return node.get_text(strip=True) if node else None


_DIGITS = re.compile(r"-?\d[\d\s]*")


def _parse_profit_units(text: str) -> int:
    """'-1 000 o' -> -1000 ; '+950 o' -> 950."""
    if not text:
        return 0
    m = _DIGITS.search(text)
    if not m:
        return 0
    try:
        return int(m.group(0).replace(" ", "").replace(" ", ""))
    except ValueError:
        return 0


def _parse_int(text: Optional[str]) -> int:
    if not text:
        return 0
    m = re.search(r"-?\d+", text.replace(" ", ""))
    return int(m.group(0)) if m else 0


class AlpinbetClient:
    def __init__(self, username: str, password: str,
                 timeout: int = 20, throttle: float = 1.0):
        self.username = username
        self.password = password
        self.timeout = timeout
        self.throttle = throttle
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": UA})
        # устойчивость к обрывам/5xx: ретраи с бэкоффом
        retry = Retry(total=3, connect=3, read=3, backoff_factor=0.6,
                      status_forcelist=(500, 502, 503, 504),
                      allowed_methods=frozenset(["GET", "POST"]))
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._logged_in = False
        self._last_request = 0.0

    # ---- низкоуровневое ----
    def _sleep_throttle(self) -> None:
        if self.throttle <= 0:
            return
        delta = time.monotonic() - self._last_request
        if delta < self.throttle:
            time.sleep(self.throttle - delta)
        self._last_request = time.monotonic()

    def _get(self, url: str, **kw) -> requests.Response:
        self._sleep_throttle()
        return self.session.get(url, timeout=self.timeout, **kw)

    def _post(self, url: str, **kw) -> requests.Response:
        self._sleep_throttle()
        return self.session.post(url, timeout=self.timeout, **kw)

    # ---- авторизация ----
    def _csrf_token(self) -> str:
        r = self._get(BASE + "/")
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        meta = soup.find("meta", attrs={"name": "csrf-token"})
        if not meta or not meta.get("content"):
            raise AlpinbetAuthError("Не нашёл csrf-token на главной")
        return meta["content"]

    @staticmethod
    def _page_is_authed(html: str) -> bool:
        soup = BeautifulSoup(html, "lxml")
        return bool(soup.find("a", href=lambda h: h and "site/logout" in h))

    def login(self) -> None:
        csrf = self._csrf_token()
        payload = {
            "_csrf-frontend": csrf,
            "LoginForm[username]": self.username,
            "LoginForm[password]": self.password,
            "LoginForm[rememberMe]": "1",
        }
        headers = {
            "Referer": BASE + "/",
            "Origin": BASE,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        r = self._post(LOGIN_URL, data=payload, headers=headers, allow_redirects=True)
        r.raise_for_status()
        if not self._page_is_authed(r.text):
            check = self._get(BASE + "/")
            if not self._page_is_authed(check.text):
                raise AlpinbetAuthError(
                    "Вход не подтверждён — проверь логин/пароль ЛК alpinbet")
        self._logged_in = True

    def ensure_logged_in(self) -> None:
        if not self._logged_in:
            self.login()

    def force_login(self) -> None:
        """Принудительный релогин (сбрасываем куки/флаг и входим заново)."""
        self._logged_in = False
        self.session.cookies.clear()
        self.login()

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    def _get_authed(self, url: str, **kw) -> requests.Response:
        """GET с авто-релогином при протухании сессии."""
        self.ensure_logged_in()
        r = self._get(url, **kw)
        if r.status_code == 404 or "site/login" in r.url:
            self.login()
            r = self._get(url, **kw)
        r.raise_for_status()
        return r

    # ---- бинарные данные (картинка прогноза) ----
    def get_image_bytes(self, image_url: str) -> tuple[bytes, str]:
        # картинки — статика на /images/photo, троттл не применяем (важно для
        # всплеска из многих сигналов подряд)
        self.ensure_logged_in()
        r = self.session.get(image_url, timeout=self.timeout)
        r.raise_for_status()
        return r.content, r.headers.get("Content-Type", "image/png")

    # ---- параметры рассылки ----
    def extract_search_params(self, dispatch_url: str) -> dict:
        r = self._get_authed(dispatch_url)
        params = {}
        for key in ("user_id", "dispatch_id"):
            m = re.search(
                r'name="ForecastSearch\[' + key + r'\]"\s+value="([^"]*)"', r.text)
            if m:
                params[key] = m.group(1)
        if "user_id" not in params or "dispatch_id" not in params:
            raise RuntimeError(
                "Не удалось извлечь user_id/dispatch_id со страницы рассылки "
                "(нет доступа к этой рассылке этим ЛК?)")
        return params

    # ---- прогнозы (лёгкий PJAX) ----
    def fetch_forecasts(self, dispatch_url: str, user_id: str, dispatch_id: str,
                        active_tab: str = DEFAULT_ACTIVE_TAB) -> list[ParsedForecast]:
        params = {
            "ForecastSearch[isFavorite]": "0",
            "ForecastSearch[sort]": "date",
            "ForecastSearch[user_id]": user_id,
            "ForecastSearch[g_id]": "",
            "ForecastSearch[dispatch_id]": dispatch_id,
            "ForecastSearch[activeTab]": active_tab,
            "ForecastSearch[perPage]": "20",
            "page": "",
            "_pjax": "#pjax-forecast-list",
        }
        headers = {
            "Referer": dispatch_url,
            "X-Requested-With": "XMLHttpRequest",
            "X-PJAX": "true",
            "X-PJAX-Container": "#pjax-forecast-list",
        }
        r = self._get_authed(dispatch_url, params=params, headers=headers)
        return self.parse_rows(r.text)

    @staticmethod
    def _extract_image(row) -> str:
        cell = row.select_one(".cell-prognos")
        if not cell:
            return ""
        # предпочитаем светлую тему; lazy data-src, потом src, потом noscript
        for sel in ("img.img-light[data-src]", "img[data-src]",
                    "img.img-light[src]", "img[src]"):
            img = cell.select_one(sel)
            if img:
                u = img.get("data-src") or img.get("src")
                if u:
                    return urljoin(BASE, u)
        ns = cell.find("noscript")
        if ns:
            inner = BeautifulSoup(ns.decode_contents(), "lxml").find("img")
            if inner and inner.get("src"):
                return urljoin(BASE, inner["src"])
        return ""

    @classmethod
    def parse_rows(cls, html: str) -> list[ParsedForecast]:
        soup = BeautifulSoup(html, "lxml")
        result: list[ParsedForecast] = []
        for row in soup.select(".rTableLine"):
            link = row.select_one(".cell-team-title a[href]")
            if not link:
                continue  # строка-шапка

            href = link.get("href", "")
            tail = href.rstrip("/").split("/")[-1]
            forecast_id = tail.split("-")[0] if tail.split("-")[0].isdigit() else ""

            ev = row.select_one(".time-event")
            status = None
            if ev:
                lead = ev.find(string=True, recursive=False)
                status = lead.strip() if lead else _txt(ev)

            teams = [_txt(t) for t in row.select(".cell-team-command")]
            sport_icon = row.select_one(".cell-icon.js-sport-tooltip")

            outcome_span = row.select_one("span.win, span.lose, span.return")
            settled = outcome_span is not None
            outcome = None
            profit_units = 0
            if outcome_span:
                classes = outcome_span.get("class", [])
                for c in ("win", "lose", "return"):
                    if c in classes:
                        outcome = c
                        break
                profit_units = _parse_profit_units(
                    outcome_span.get_text(" ", strip=True))

            result.append(ParsedForecast(
                forecast_id=forecast_id,
                sport=sport_icon.get("data-tippy-content") if sport_icon else None,
                status=status,
                minute=_txt(row.select_one(".js-current-minutes")),
                score=_txt(row.select_one(".cell-team-score")),
                home_team=teams[0] if len(teams) > 0 else None,
                away_team=teams[1] if len(teams) > 1 else None,
                league=_txt(row.select_one(".cell-team-tnm")),
                bet_type=_txt(row.select_one(".cell-type .info-help")),
                coefficient=_txt(row.select_one(".cell-coefficient__total")),
                url=urljoin(BASE, href),
                image_url=cls._extract_image(row),
                settled=settled,
                outcome=outcome,
                profit_units=profit_units,
            ))
        return result

    # ---- статистика прибыли (полная страница рассылки) ----
    def fetch_stats_tables(self, dispatch_url: str) -> dict[str, list[StatRow]]:
        """Возвращает {'day': [...], 'month': [...], 'year': [...]}.

        Таблицы прибыли уже отрендерены в полной странице рассылки — отдельный
        PJAX не нужен.
        """
        r = self._get_authed(dispatch_url)
        soup = BeautifulSoup(r.text, "lxml")
        return {
            "day": self._parse_stat_table(soup, "tab-day"),
            "month": self._parse_stat_table(soup, "tab-month"),
            "year": self._parse_stat_table(soup, "tab-year"),
        }

    @staticmethod
    def _parse_stat_table(soup, container_id: str) -> list[StatRow]:
        container = soup.find(id=container_id)
        if not container:
            return []
        rows: list[StatRow] = []
        for line in container.select(".rTableLine"):
            label_cell = line.select_one(".cell-month")
            if not label_cell or "table-header" in (line.get("class") or []):
                continue
            label = label_cell.get_text(strip=True)
            if not label or label.lower() in ("дата", "месяц", "год"):
                continue
            wlr = (line.select_one(".cell-win").get_text(" ", strip=True)
                   if line.select_one(".cell-win") else "")
            nums = re.findall(r"-?\d+", wlr)
            win = int(nums[0]) if len(nums) > 0 else 0
            lose = int(nums[1]) if len(nums) > 1 else 0
            ret = int(nums[2]) if len(nums) > 2 else 0
            roi = line.select_one(".cell-roi")
            profit_units_cell = line.select_one(".cell-subscribers")
            rows.append(StatRow(
                label=label,
                win=win, lose=lose, ret=ret,
                profit_percent=roi.get_text(strip=True) if roi else "0.00%",
                profit_units=_parse_profit_units(
                    profit_units_cell.get_text(" ", strip=True) if profit_units_cell else ""),
            ))
        return rows


# ---- утилиты периодов ----
def parse_day_label(label: str) -> Optional[date]:
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", label.strip())
    if not m:
        return None
    d, mo, y = map(int, m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_month_label(label: str) -> Optional[tuple[int, int]]:
    parts = label.strip().split()
    if len(parts) < 2:
        return None
    mon = RUS_MONTH.get(parts[0][:3].lower())
    try:
        year = int(parts[1])
    except ValueError:
        return None
    return (year, mon) if mon else None

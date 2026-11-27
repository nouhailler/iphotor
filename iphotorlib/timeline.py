"""Frise chronologique : agrégation des dates par année, mois et jour.

Entrée : une liste de (identifiant, date Unix ou None), déjà triée ou non. Aucun accès disque.
Les dates aberrantes (avant 1900 ou dans le futur lointain) sont classées « inconnues ».
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Iterable, Optional

MONTHS_FR = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
             "septembre", "octobre", "novembre", "décembre")
MIN_YEAR = 1900


@dataclass
class Period:
    level: str                    # "year" | "month" | "day" | "unknown" | "gap"
    year: Optional[int] = None
    month: Optional[int] = None
    day: Optional[int] = None
    count: int = 0
    first_id: Optional[int] = None      # photo représentative (la première de la période)
    start: Optional[float] = None       # bornes Unix [start, end[
    end: Optional[float] = None
    gap_years: int = 0                  # « gap » : nombre d'années consécutives sans photo
    top: bool = False                   # parmi les périodes les plus riches

    @property
    def label(self) -> str:
        if self.level == "year":
            return str(self.year)
        if self.level == "month":
            return f"{MONTHS_FR[self.month - 1].capitalize()} {self.year}"
        if self.level == "day":
            d = _dt.date(self.year, self.month, self.day)
            return f"{d.day} {MONTHS_FR[d.month - 1]} {d.year}"
        if self.level == "gap":
            return f"{self.year}" if self.gap_years == 1 else f"{self.year} – {self.year + self.gap_years - 1}"
        return "Date inconnue"


def _valid(ts: Optional[float]) -> Optional[_dt.datetime]:
    if ts is None:
        return None
    try:
        d = _dt.datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError):
        return None
    if d.year < MIN_YEAR or d.year > _dt.date.today().year + 1:
        return None
    return d


def _bounds(y: int, m: Optional[int] = None, d: Optional[int] = None) -> tuple[float, float]:
    if m is None:
        a, b = _dt.datetime(y, 1, 1), _dt.datetime(y + 1, 1, 1)
    elif d is None:
        a = _dt.datetime(y, m, 1)
        b = _dt.datetime(y + (m == 12), m % 12 + 1, 1)
    else:
        a = _dt.datetime(y, m, d)
        b = a + _dt.timedelta(days=1)
    return a.timestamp(), b.timestamp()


@dataclass
class Timeline:
    years: dict = field(default_factory=dict)       # y -> [count, first_id]
    months: dict = field(default_factory=dict)      # (y, m) -> [count, first_id]
    days: dict = field(default_factory=dict)        # (y, m, d) -> [count, first_id]
    unknown: int = 0
    unknown_first: Optional[int] = None
    total: int = 0

    @classmethod
    def build(cls, items: Iterable[tuple[int, Optional[float]]]) -> "Timeline":
        t = cls()
        for pid, ts in sorted(items, key=lambda it: (it[1] is None, it[1] or 0, it[0])):
            t.total += 1
            d = _valid(ts)
            if d is None:
                t.unknown += 1
                if t.unknown_first is None:
                    t.unknown_first = pid
                continue
            for table, k in ((t.years, d.year), (t.months, (d.year, d.month)), (t.days, (d.year, d.month, d.day))):
                e = table.get(k)
                if e is None:
                    table[k] = [1, pid]
                else:
                    e[0] += 1
        return t

    def year_periods(self, show_gaps: bool = True) -> list[Period]:
        out: list[Period] = []
        ys = sorted(self.years)
        top = self._top(self.years, 3)
        prev = None
        for y in ys:
            if show_gaps and prev is not None and y - prev > 1:
                out.append(Period("gap", year=prev + 1, gap_years=y - prev - 1))
            c, fid = self.years[y]
            s, e = _bounds(y)
            out.append(Period("year", year=y, count=c, first_id=fid, start=s, end=e, top=y in top))
            prev = y
        if self.unknown:
            out.append(Period("unknown", count=self.unknown, first_id=self.unknown_first))
        return out

    def month_periods(self, year: int) -> list[Period]:
        """Les 12 mois de l'année, y compris les mois sans photo (count = 0)."""
        sub = {m: v for (y, m), v in self.months.items() if y == year}
        top = self._top(sub, 2)
        out = []
        for m in range(1, 13):
            c, fid = sub.get(m, (0, None))
            s, e = _bounds(year, m)
            out.append(Period("month", year=year, month=m, count=c, first_id=fid, start=s, end=e,
                              top=m in top and c > 0))
        return out

    def day_periods(self, year: int, month: int) -> list[Period]:
        """Les jours du mois ayant des photos."""
        sub = {d: v for (y, m, d), v in self.days.items() if y == year and m == month}
        top = self._top(sub, 3)
        out = []
        for d in sorted(sub):
            c, fid = sub[d]
            s, e = _bounds(year, month, d)
            out.append(Period("day", year=year, month=month, day=d, count=c, first_id=fid, start=s, end=e,
                              top=d in top))
        return out

    def richest_days(self, n: int = 5) -> list[Period]:
        best = sorted(self.days.items(), key=lambda kv: (-kv[1][0], kv[0]))[:n]
        out = []
        for (y, m, d), (c, fid) in best:
            s, e = _bounds(y, m, d)
            out.append(Period("day", year=y, month=m, day=d, count=c, first_id=fid, start=s, end=e, top=True))
        return out

    @staticmethod
    def _top(table: dict, n: int) -> set:
        """Périodes « les plus riches » : au plus n, et seulement si elles se distinguent vraiment
        (au moins 3 périodes par période mise en avant, effectif supérieur à la moyenne)."""
        if len(table) < 2:
            return set()
        n = max(1, min(n, len(table) // 3))
        avg = sum(v[0] for v in table.values()) / len(table)
        best = sorted(table.items(), key=lambda kv: -kv[1][0])[:n]
        return {k for k, v in best if v[0] > avg}

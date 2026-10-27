"""Fixed official monthly series; no model-generated URLs, estimates or gap filling."""

import csv
import hashlib
import io
import math
import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

import httpx

from .db import now

MONTHLY_SERIES = {
    "OECD_KOR_MANUFACTURING": {
        "name": "한국 제조업 생산지수",
        "unit": "2015=100 · 계절조정 · OECD",
        "frequency": "월간",
        "source": "OECD · STES",
        "domain": "data-explorer.oecd.org",
        "url": "https://data-explorer.oecd.org/vis?df[ag]=OECD.SDD.STES&df[id]=DSD_STES@DF_INDSERV&df[vs]=4.3&dq=KOR.M.PRVM.IX.C.Y._Z._Z.N",
        "fetch_url": "https://sdmx.oecd.org/public/rest/v1/data/OECD.SDD.STES,DSD_STES@DF_INDSERV,4.3/KOR.M.PRVM.IX.C.Y._Z._Z.N",
        "scope": "한국 제조업 전체 생산량 지수. 개별 기업·품목 출하량이 아님. 기준연도·계절조정 고정; 최신 수정치.",
    },
    "EIA_BRENT_MONTHLY": {
        "name": "브렌트유 현물 월평균 가격",
        "unit": "USD/배럴 · 월평균 · FOB",
        "frequency": "월간",
        "source": "U.S. EIA",
        "domain": "eia.gov",
        "url": "https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=RBRTE&f=M",
        "expected_title": "Europe Brent Spot Price FOB (Dollars per Barrel)",
        "scope": "유럽 브렌트 현물 기준가격. 국내 기업 구매가격·선물가격·원화 비용이 아님.",
    },
    "EIA_WTI_MONTHLY": {
        "name": "WTI 현물 월평균 가격",
        "unit": "USD/배럴 · 월평균 · FOB",
        "frequency": "월간",
        "source": "U.S. EIA",
        "domain": "eia.gov",
        "url": "https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=RWTC&f=M",
        "expected_title": "Cushing, OK WTI Spot Price FOB (Dollars per Barrel)",
        "scope": "미국 Cushing WTI 현물 기준가격. 국내 기업 구매가격·선물가격·원화 비용이 아님.",
    },
    "EIA_HENRY_HUB_MONTHLY": {
        "name": "Henry Hub 천연가스 현물 월평균 가격",
        "unit": "USD/MMBtu · 월평균",
        "frequency": "월간",
        "source": "U.S. EIA",
        "domain": "eia.gov",
        "url": "https://www.eia.gov/dnav/ng/hist/rngwhhdm.htm",
        "expected_title": "Henry Hub Natural Gas Spot Price (Dollars per Million Btu)",
        "scope": "미국 Henry Hub 현물가격. 한국 LNG 수입가격·JKM·기업 연료비와 구분.",
    },
}


def monthly_date(period):
    if not re.fullmatch(r"\d{4}-\d{2}", period):
        raise ValueError("Expected monthly period")
    return date.fromisoformat(period + "-01")


def oecd_points(text):
    expected = {
        "DATAFLOW": "OECD.SDD.STES:DSD_STES@DF_INDSERV(4.3)",
        "REF_AREA": "KOR",
        "FREQ": "M",
        "MEASURE": "PRVM",
        "UNIT_MEASURE": "IX",
        "ACTIVITY": "C",
        "ADJUSTMENT": "Y",
        "TRANSFORMATION": "_Z",
        "TIME_HORIZ": "_Z",
        "METHODOLOGY": "N",
        "UNIT_MULT": "0",
        "BASE_PER": "2015",
    }
    points = {}
    for row in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        if any(row.get(k) != v for k, v in expected.items()):
            raise ValueError("Unexpected OECD series, unit or base year")
        stamp = monthly_date(row["TIME_PERIOD"])
        if not date(2020, 1, 1) <= stamp < now().date().replace(day=1):
            continue
        if row["OBS_VALUE"] in {"", ":"}:
            continue
        value = float(row["OBS_VALUE"])
        if not math.isfinite(value) or str(stamp) in points:
            raise ValueError("Invalid or duplicate observation")
        points[str(stamp)] = {"date": str(stamp), "value": value, "status": row.get("OBS_STATUS", "")[:4]}
    if not points:
        raise ValueError("No observations")
    return sorted(points.values(), key=lambda p: p["date"])


class EIATable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.cells, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.cells = []
        elif tag == "td" and self.cells is not None:
            self.cell = [dict(attrs).get("class", ""), ""]

    def handle_data(self, text):
        if self.cell is not None:
            self.cell[1] += text

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.cells.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.cells is not None:
            self.rows.append(self.cells)
            self.cells, self.cell = None, None


def eia_points(text, expected_title):
    # Confirm the exact product/unit, not merely an HTTP 200 or numeric table.
    plain = " ".join(re.sub(r"<[^>]*>", " ", text).split())
    if expected_title not in plain:
        raise ValueError("Wrong EIA product or unit")
    parser = EIATable()
    parser.feed(text)
    points = {}
    for row in parser.rows:
        if not row or row[0][0] != "B4" or not re.fullmatch(r"\d{4}", row[0][1].strip()):
            continue
        if len(row) != 13 or any(c[0] != "B3" for c in row[1:]):
            raise ValueError("Unexpected EIA monthly table")
        year = int(row[0][1].strip())
        for month, (_, raw) in enumerate(row[1:], 1):
            stamp = date(year, month, 1)
            if not date(2020, 1, 1) <= stamp < now().date().replace(day=1):
                continue
            raw = raw.strip()
            if raw in {"", "-", "--", "NA", "W"}:
                continue
            value = float(raw)
            if not math.isfinite(value) or str(stamp) in points:
                raise ValueError("Invalid or duplicate observation")
            points[str(stamp)] = {"date": str(stamp), "value": value}
    if not points:
        raise ValueError("No monthly observations")
    release = re.search(r"Release Date:\s*(\d{1,2}/\d{1,2}/\d{4})", plain)
    released = datetime.strptime(release[1], "%m/%d/%Y").date() if release else None
    if released and released > now().date():
        raise ValueError("Future release")
    return sorted(points.values(), key=lambda p: p["date"]), str(released) if released else None


def fetch_monthly(code, transport=None):
    definition = MONTHLY_SERIES[code]
    url = definition.get("fetch_url", definition["url"])
    params = {"startPeriod": "2020-01", "format": "csvfile"} if code.startswith("OECD_") else None
    with httpx.Client(timeout=25, trust_env=False, follow_redirects=False, transport=transport) as client:
        with client.stream("GET", url, params=params) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 1_000_000:
                    raise ValueError("Oversized monthly response")
    text = raw.decode("utf-8-sig")
    if code.startswith("OECD_"):
        if "csv" not in content_type:
            raise ValueError("Expected OECD CSV")
        points, release = oecd_points(text), None
    else:
        if "html" not in content_type:
            raise ValueError("Expected EIA HTML")
        points, release = eia_points(text, definition["expected_title"])
    plain = " ".join(re.sub(r"<[^>]*>", " ", text).split())
    announced = re.search(r"Next Release Date:\s*(\d{1,2}/\d{1,2}/\d{4})", plain)
    next_release = datetime.strptime(announced[1], "%m/%d/%Y").date() if announced else None
    if next_release and not now().date() <= next_release <= now().date() + timedelta(days=366):
        next_release = None
    return {
        "points": points,
        "source_url": url,
        "frequency": "M",
        "unit": definition["unit"],
        "release_date": release,
        "next_release_date": str(next_release) if next_release else None,
        "release_date_scope": "source table release; not each observation vintage",
        "response_sha256": hashlib.sha256(raw).hexdigest(),
        "scope": definition["scope"],
    }

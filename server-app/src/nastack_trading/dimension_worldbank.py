"""Public Pink Sheet monthly observations. Never daily-fill or backdate availability."""

import calendar
import hashlib
import io
import math
import re
import zipfile
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import httpx
from fastapi import HTTPException

from .db import now

LANDING = "https://www.worldbank.org/en/research/commodity-markets"
SERIES = {
    "commodity-06": ("Zinc", "($/mt)", "USD/mt"),
    "commodity-07": ("Aluminum", "($/mt)", "USD/mt"),
    "commodity-09": ("Lead", "($/mt)", "USD/mt"),
    "commodity-10": ("Tin", "($/mt)", "USD/mt"),
    "commodity-19": ("Crude oil, Dubai", "($/bbl)", "USD/bbl"),
    "commodity-21": ("Natural gas, Europe", "($/mmbtu)", "USD/MMBtu"),
    "commodity-28": ("Soybean oil", "($/mt)", "USD/mt"),
    "commodity-29": ("Palm oil", "($/mt)", "USD/mt"),
    "commodity-31": ("Coffee, Arabica", "($/kg)", "USD/kg"),
    "commodity-32": ("Cotton, A Index", "($/kg)", "USD/kg"),
    "commodity-33": ("Rubber, RSS3", "($/kg)", "USD/kg"),
}
VARIANTS = {
    "commodity-29": ("팜유 · 2026 이후 월평균", "공급자가 2026-01부터 교체한 계열만 수집합니다. 2025년 이전 자료와 연결·보정하지 않습니다."),
    "commodity-31": ("커피 Arabica · 월평균", "Arabica만 수집합니다. Robusta·혼합 커피·ICE 선물과 합성하지 않습니다."),
    "commodity-33": ("고무 RSS3 · 월평균", "RSS3 근월 기준 월평균입니다. 현물·TSR20과 혼합하지 않습니다."),
}
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def apply_worldbank(row):
    if row["id"] not in SERIES:
        return
    label, _, unit = SERIES[row["id"]]
    row.update(
        provider="worldbank_monthly",
        series_code=label,
        unit=unit,
        frequency="월간",
        representation="monthly_benchmark",
        can_collect=True,
        assessment="collectable_monthly",
        assessment_date="2026-10-27",
        source_name="World Bank Pink Sheet · " + label,
        source_url=LANDING,
        source_version="wb-pink-monthly-v1",
        access_method="공식 월별 XLSX · 수동 수집",
        license="World Bank 데이터 이용조건·출처 표기; 일별/원거래소 feed 이용권과 별도",
        daily_feasibility="월간 원자료 수집 구현 · 일별 벡터 입력 제외",
        reason="월평균 원자료만 저장·표시합니다. 공표 당시 이용 가능성과 월간 분석은 별도 검증이며 일별 벡터/예측에는 사용하지 않습니다.",
        calendar_note="월말은 관측월 표기이며 거래일/공표일이 아닙니다. 보간·일별 복제 없음.",
        manual_only=True,
        daily_analysis_eligible=False,
        implementation_label="월간 수집 구현",
        implementation_status="implemented_monthly",
    )
    if row["id"] in VARIANTS:
        row["name"], note = VARIANTS[row["id"]]
        row["reason"] = note + " " + row["reason"]
    if row["id"] == "commodity-29":
        row.update(source_version="wb-palm-2026-monthly-v1", observation_start="2026-01-01", series_break="2026-01")


class MonthlyLink(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = set()

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href", "")
        u = urlsplit(href)
        if (
            u.scheme == "https"
            and u.netloc == "thedocs.worldbank.org"
            and not u.query
            and not u.fragment
            and re.fullmatch(r"/en/doc/[A-Za-z0-9-]+/related/CMO-Historical-Data-Monthly\.xlsx", u.path)
        ):
            self.links.add(href)


def xml(raw):
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("DTD not permitted")
    return ET.fromstring(raw)


def parse_monthly(raw, dimension, start, end):
    """Select sheet by workbook relationship, not file order or hardcoded column."""
    with zipfile.ZipFile(io.BytesIO(raw)) as book:
        files = book.infolist()
        if (
            len(files) > 100
            or any(f.flag_bits & 1 for f in files)
            or len({f.filename for f in files}) != len(files)
            or sum(f.file_size for f in files) > 20_000_000
        ):
            raise ValueError("Workbook expansion limit")
        sheets = xml(book.read("xl/workbook.xml"))
        links = {r.get("Id"): r.get("Target") for r in xml(book.read("xl/_rels/workbook.xml.rels"))}
        sheet = [r for r in sheets.findall("s:sheets/s:sheet", NS) if r.get("name") == "Monthly Prices"]
        if len(sheet) != 1:
            raise ValueError("Missing monthly sheet")
        target = links[
            sheet[0].get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        ]
        path = target.lstrip("/") if target.startswith("/xl/") else "xl/" + target
        if not re.fullmatch(r"xl/worksheets/sheet\d+\.xml", path):
            raise ValueError("Invalid worksheet relationship")
        strings = []
        if "xl/sharedStrings.xml" in book.namelist():
            strings = [
                "".join(si.itertext()) for si in xml(book.read("xl/sharedStrings.xml")).findall("s:si", NS)
            ]
        rows = []
        for row in xml(book.read(path)).findall("s:sheetData/s:row", NS):
            cells = {}
            for cell in row.findall("s:c", NS):
                match = re.fullmatch(r"([A-Z]+)\d+", cell.get("r", ""))
                if not match or match[1] in cells:
                    raise ValueError("Invalid cell reference")
                value = cell.findtext("s:v", default="", namespaces=NS)
                kind = cell.get("t")
                if kind == "s":
                    value = strings[int(value)]
                elif kind == "inlineStr":
                    value = "".join(cell.find("s:is", NS).itertext())
                elif kind in {"b", "e"}:
                    # Unrelated columns may contain Excel error cells. Do not
                    # reject the entire sheet, but never accept these as prices.
                    value = "INVALID_CELL_TYPE:" + kind
                cells[match[1]] = value.strip()
            rows.append(cells)
    label, unit, _ = SERIES[dimension["id"]]
    header = [(i, col) for i, row in enumerate(rows) for col, v in row.items() if v == label]
    if len(header) != 1:
        raise ValueError("Series header mismatch")
    index, col = header[0]
    if rows[index + 1].get(col) != unit:
        raise ValueError("Unit changed")
    stamps = [v for row in rows[:index] for v in row.values() if v.startswith("Updated on ")]
    if len(stamps) != 1:
        raise ValueError("Missing update date")
    released = datetime.strptime(stamps[0], "Updated on %B %d, %Y").date()
    if released > now().date():
        raise ValueError("Future release")
    points, seen = [], set()
    for row in rows[index + 2 :]:
        period = row.get("A", "")
        if not re.fullmatch(r"\d{4}M\d{2}", period):
            continue
        year, month = int(period[:4]), int(period[5:])
        stamp = date(year, month, calendar.monthrange(year, month)[1])
        if stamp >= released or period in seen:
            raise ValueError("Future or duplicate observation")
        seen.add(period)
        value = row.get(col, "")
        if value in {"", "...", "…", ".."}:
            value = None
        else:
            value = float(value)
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Invalid price")
        # The replacement series is not spliced to the old palm-oil benchmark.
        first = max(start, date.fromisoformat(dimension.get("observation_start", "2000-01-01")))
        if first <= stamp <= end:
            points.append({"date": str(stamp), "period": f"{year:04d}-{month:02d}", "value": value})
    if not any(p["value"] is not None for p in points):
        raise HTTPException(
            409,
            "선택 기간에 발표된 월간 자료가 없습니다. 이전 자료를 보존합니다.",
            headers={"X-Collection-State": "not_published"},
        )
    return sorted(points, key=lambda p: p["date"]), str(released)


def fetch_monthly(dimension, start, end, transport=None):
    def read(client, url):
        raw = bytearray()
        with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise ValueError("Provider response")
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 2_000_000:
                    raise ValueError("Response size limit")
        return bytes(raw)

    try:
        with httpx.Client(timeout=20, trust_env=False, follow_redirects=False, transport=transport) as client:
            landing = read(client, LANDING)
            parser = MonthlyLink()
            parser.feed(landing.decode("utf-8"))
            if len(parser.links) != 1:
                raise ValueError("Ambiguous monthly download")
            url = parser.links.pop()
            raw = read(client, url)
        points, released = parse_monthly(raw, dimension, start, end)
        return {
            "provider": "worldbank_monthly",
            "series_code": dimension["series_code"],
            "source_version": dimension["source_version"],
            "frequency": "monthly",
            "points": points,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "landing_sha256": hashlib.sha256(landing).hexdigest(),
            "source_url": url,
            "retrieved_at": now().isoformat(),
            "release_date": released,
            "available_at": now().isoformat(),
            "publication_times_verified": False,
            "daily_analysis_eligible": False,
            "series_break": dimension.get("series_break"),
        }
    except HTTPException:
        raise
    except (
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        UnicodeError,
        zipfile.BadZipFile,
        ET.ParseError,
        httpx.HTTPError,
    ):
        raise HTTPException(
            502, "월간 공식 자료의 경로·계열·단위·형식을 확인하지 못했습니다. 이전 자료를 보존합니다."
        ) from None

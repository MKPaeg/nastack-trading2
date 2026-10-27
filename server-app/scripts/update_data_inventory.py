"""Offline inventory export and immutable versioned Markdown/JSON/PDF history.

No DB, network, credentials or paid calls. Use project Python; --pdf-python may
point to the bundled reportlab runtime. --check is strictly read-only.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
NOTE = ROOT / "docs/research/external-data-inventory.md"
BASE = ROOT / "docs/research/external-data"
PDF = BASE / "external-data-inventory.pdf"
CURRENT = BASE / "inventory.json"
MANIFEST = BASE / "history/manifest.json"
START, END = "<!-- GENERATED_INVENTORY_START -->", "<!-- GENERATED_INVENTORY_END -->"
WATCHED = [
    "brokers.py", "market.py", "instrument_master.py", "price_history.py",
    "dimension_namuh.py", "dimension_twse.py", "dimension_worldbank.py", "dimension_imf.py", "dimension_pending.py", "dimension_krx.py", "dimension_steel.py", "kiwoom.py", "kiwoom_catalog.py", "etf_expansion.py",
    "dimension_catalog.py", "dimension_providers.py", "dimensions.py", "official_daily.py",
    "statistics.py", "monthly_statistics.py", "news.py", "issuer_sources.py",
    "business_context.py", "business_research.py", "collection_search.py", "research.py",
    "portfolio.py", "recommendations.py", "trade_gateway.py", "trading.py", "live_worker.py", "personal_connections.py",
    "collection_pipeline.py", "model_catalog.py", "model_pricing.py", "news_translation.py", "telegram_settings.py", "llm_usage.py", "llm_usage_history.py", "worker.py",
    "trade_notifications.py", "namuh_reads.py", "user_brokers.py", "default_attributes.py", "opendart.py",
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes():
    paths = [f"src/nastack_trading/{name}" for name in WATCHED]
    paths += ["scripts/update_data_inventory.py", "docs/research/external-data-history.md",
              "docs/research/2026-10-27-unconnected-dimensions.md",
              "docs/research/2026-10-27-opendart-financial-check.md", "app.js", "trade-ui.js", "collection-ui.js",
              "docs/research/2026-10-27-pending-dimensions-plan.md", "scripts/probe_pending_dimensions.py", "scripts/check_worldbank_monthly.py",
              "docs/implementation/SOURCE_ACCESS_FINAL_2026-10-21.json"]
    paths += [str(p.relative_to(ROOT)) for p in sorted((BASE / "probes").glob("2026-10-27-pending-public-*.json"))]
    paths += [str(p.relative_to(ROOT)) for p in sorted((BASE / "probes").glob("*-wb-runtime-parser.json"))]
    paths += ["scripts/check_remaining_dimensions.py"]
    paths += [str(p.relative_to(ROOT)) for p in sorted((BASE / "probes").glob("*-remaining-runtime.json"))]
    return {p: digest(ROOT / p) for p in paths}


def cell(value):
    return str(value if value is not None else "미확정").replace("|", "/").replace("\n", " ")


def table(headers, rows):
    return "\n".join(["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
                     + ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows])


def definitions():
    sys.path.insert(0, str(ROOT / "src"))
    from nastack_trading.business_context import REVIEWED, REVIEWED_DATES, REVIEWED_ON
    from nastack_trading.dimension_catalog import DIMENSIONS
    from nastack_trading.news import FEEDS, SOURCE_INFORMATION
    from nastack_trading.statistics import SERIES
    old = json.loads((ROOT / "docs/implementation/SOURCE_ACCESS_FINAL_2026-10-21.json").read_text())
    candidates = [{"name": r["name"], "domain": r["domain"], "information": r.get("information", ""),
                   "evidence_date": "2026-10-20/21 (historical, not live)"} for r in old["results"]]
    review = (ROOT / "docs/research/2026-10-27-pending-dimensions-plan.md").read_text()
    review_rows = []
    for line in review.splitlines():
        if re.match(r"\| (?:sector|commodity|market|control)-\d+ ", line):
            fields = [v.strip() for v in line.strip().strip("|").split("|")]
            if len(fields) != 4 or fields[3] not in {"완료 (월간 수집·조회)", "완료 (개인 연결 확인 필요)", "미완료"}:
                raise ValueError("25개 진행표의 구현완료 상태를 확인하세요")
            review_rows.append({"id": fields[0].split()[0], "item": fields[0],
                                "source_and_test": fields[1], "next_and_status": fields[2],
                                "implementation_status": fields[3],
                                "implementation_complete": fields[3].startswith("완료"),
                                "implementation_scope": ("conditional_daily_collection_view" if fields[3] == "완료 (개인 연결 확인 필요)" else "monthly_collection_view") if fields[3].startswith("완료") else None})
    return {"dimensions": DIMENSIONS, "statistics": SERIES,
            "pending_dimension_review": {"as_of": "2026-10-27", "rows": review_rows,
                "scope": "initial_25_candidates_progress_with_monthly_collection_separate_from_daily_analysis", "plan_markdown": review},
            "reviewed_business_sources": [{"code": code, "name": v[0], "source_url": v[2],
                 "topics": v[3], "reviewed_on": REVIEWED_DATES.get(code, REVIEWED_ON),
                 "kind": "static_reviewed_context_not_live_fetch"} for code, v in REVIEWED.items()],
            "feeds": {key: {"url": value[0], "description": value[1]} for key, value in FEEDS.items()},
            "registered_candidates_historical": candidates,
            "source_capability_notes": SOURCE_INFORMATION}


def appendix(data):
    dims = data["dimensions"]
    parts = ["## 자동 부록 - 전체 정의 목록",
             f"코드 정의 {len(dims)}개, 수집 가능 플래그 {sum(bool(x['can_collect']) for x in dims)}개. 플래그는 실제 성공·사용자별 권한·활성화 보장이 아니다."]
    for group in dict.fromkeys(x["group_name"] for x in dims):
        rows = [x for x in dims if x["group_name"] == group]
        parts += [f"### 차원 - {group}", table(["ID / 분석 주제", "실제 계열 / 제공자", "단위 / 표현", "연결"],
            [(f"{x['id']} / {x['name']}", f"{x.get('series_code') or '-'} / {x.get('instrument_name') or x['source_name']} / {x.get('provider', '-')}",
              f"{x['unit']} / {x.get('representation', '정의 확인 필요')}", x.get("implementation_label") or ("구현" if x["can_collect"] else "후보·권한/정의 대기")) for x in rows])]
    parts += ["### 정형 통계 - 전체 7개", table(["계열 ID", "데이터 / 단위", "주기 / 출처"],
              [(key, f"{v['name']} / {v['unit']}", f"{v['frequency']} / {v['source']}") for key, v in data["statistics"].items()]),
              "### RSS 정의 - 코드 존재와 실제 적용 구분",
              table(["호스트", "데이터", "적용"], [(key, v["description"], "통계 분류로 뉴스 피드 제외" if key == "bls.gov" else "개인 정책·실행 조건 적용") for key, v in data["feeds"].items()]),
              "### 기존 검토 사업정보 - 고정 원문 출처 전체 목록",
              "사업 맥락의 정적 출처다. 모든 URL의 현재 접근·전문 수집·최신성 검증을 의미하지 않는다. 종목별 동적 검색 결과는 개인 DB 이력이며 공용 문서에서 제외한다.",
              table(["종목 / 검토일", "고정 출처"], [(f"{r['name']} ({r['code']}) / {r['reviewed_on']}", r["source_url"]) for r in data["reviewed_business_sources"]]),
              "### 기존 등록 후보 - 과거 공개 목록의 전체 항목",
              "아래는 2026-10-20/21 공개 접근 점검의 후보 목록이다. 현재 수집 중인 전체 API라고 해석하면 안 된다. 개인 소스 등록/제외 상태는 내보내지 않는다. 실제 연결 여부는 본문과 차원/통계 정의를 우선한다.",
              table(["등록명 / 도메인", "데이터 범위 및 제약"], [(f"{r['name']} / {r['domain']}", r["information"]) for r in data["registered_candidates_historical"]])]
    parts += [data["pending_dimension_review"]["plan_markdown"]]
    return "\n\n".join(parts)


def render_pdf(note, output):
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    fonts = [Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf"),
             Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf")]
    font = next((f for f in fonts if f.exists()), None)
    if not font:
        raise SystemExit("Korean TrueType font required; no fallback to unreadable glyphs")
    pdfmetrics.registerFont(TTFont("Korean", str(font)))
    normal = ParagraphStyle("body", fontName="Korean", fontSize=9, leading=14, wordWrap="CJK", spaceAfter=7)
    tiny = ParagraphStyle("cell", parent=normal, fontSize=7.6, leading=11, spaceAfter=2)
    headings = {level: ParagraphStyle(f"h{level}", parent=normal, fontSize=size, leading=size + 8,
                    spaceBefore=14, spaceAfter=9, keepWithNext=True, textColor=colors.HexColor("#183650"))
                for level, size in [(1, 21), (2, 14), (3, 11)]}
    def inline(text):
        links = []
        def link(m):
            links.append((m.group(1), m.group(2)))
            return f"LINKPLACEHOLDER{len(links)-1}END"
        text = re.sub(r"\[([^]]+)\]\((https?://[^)]+)\)", link, text)
        text = escape(text).replace("`", "")
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
        for index, (label, url) in enumerate(links):
            text = text.replace(f"LINKPLACEHOLDER{index}END", f'<a href="{escape(url, {chr(34): "&quot;"})}" color="#755300"><u>{escape(label)}</u></a>')
        return text
    lines = note.read_text().splitlines()
    story, i = [], 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("<!--") or not line:
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                fields = [v.strip() for v in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", v) for v in fields):
                    rows.append([Paragraph(inline(v), tiny) for v in fields])
                i += 1
            width = A4[0] - 76
            count = len(rows[0])
            weights = {2: [0.35, 0.65], 3: [0.25, 0.36, 0.39], 4: [0.22, 0.33, 0.29, 0.16]}[count]
            t = LongTable(rows, colWidths=[width * w for w in weights], repeatRows=1, hAlign="LEFT")
            t.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#e5edf4")),
                ("VALIGN", (0,0), (-1,-1), "TOP"), ("GRID", (0,0), (-1,-1), 0.3, colors.HexColor("#c3ccd4")),
                ("LEFTPADDING", (0,0), (-1,-1), 6), ("RIGHTPADDING", (0,0), (-1,-1), 6),
                ("TOPPADDING", (0,0), (-1,-1), 5), ("BOTTOMPADDING", (0,0), (-1,-1), 5)]))
            story += [t, Spacer(1, 9)]
            continue
        heading = re.match(r"^(#{1,3}) (.+)$", line)
        story.append(Paragraph(inline(heading.group(2) if heading else line), headings[len(heading.group(1))] if heading else normal))
        i += 1
    mdhash = digest(note)
    def page(canvas, doc):
        canvas.setFont("Korean", 7)
        canvas.setFillColor(colors.HexColor("#536779"))
        basis = re.search(r"기준일: (\d{4}-\d{2}-\d{2})", note.read_text())
        canvas.drawString(38, 24, "NaStack Trading | 외부 데이터 목록 | " + (basis[1] if basis else "기준일 미상") + " 기준")
        canvas.drawRightString(A4[0] - 38, 24, str(doc.page))
    doc = SimpleDocTemplate(str(output), pagesize=A4, rightMargin=38, leftMargin=38, topMargin=37, bottomMargin=40,
            title="NaStack Trading 외부 데이터 수집 목록", author="NaStack Trading", subject=f"Markdown SHA256: {mdhash}")
    doc.build(story, onFirstPage=page, onLaterPages=page)


def check():
    meta = json.loads(CURRENT.read_text())
    problems = []
    if source_hashes() != meta["source_hashes"]:
        problems.append("감시 소스 지문 변경: 문서 재검토·새 버전 필요")
    if digest(NOTE) != meta["markdown_sha256"] or digest(PDF) != meta["pdf_sha256"]:
        problems.append("정본 Markdown/PDF가 스냅샷과 다름")
    rows = json.loads(MANIFEST.read_text())
    if not rows or rows[-1]["version"] != meta["version"]:
        problems.append("현재 버전과 manifest 불일치")
    for row in rows:
        for name, expected in row["files"].items():
            path = ROOT / name
            if not path.is_file() or digest(path) != expected:
                problems.append(f"보존 이력 손상: {name}")
    if problems:
        raise SystemExit("\n".join(problems))
    print(f"PASS {meta['version']}: source fingerprints, Markdown/PDF and {len(rows)} immutable version(s)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version")
    parser.add_argument("--reason")
    parser.add_argument("--pdf-python", default=sys.executable)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--render", nargs=2, metavar=("MARKDOWN", "PDF"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.render:
        render_pdf(*(Path(p) for p in args.render))
        return
    if args.check:
        check()
        return
    if not args.version or not re.fullmatch(r"\d+\.\d+\.\d+", args.version) or not args.reason:
        parser.error("--version X.Y.Z and --reason required")
    history = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else []
    if any(x["version"] == args.version for x in history):
        parser.error("version already exists; old history must not be overwritten")
    prefix = BASE / "history" / args.version
    if any(prefix.with_suffix(prefix.suffix + suffix).exists() for suffix in (".md", ".pdf", ".json")):
        parser.error("version files already exist")
    data = definitions()
    text = NOTE.read_text()
    if text.count(START) != 1 or text.count(END) != 1:
        parser.error("exactly one generated appendix marker pair required")
    text = text.split(START)[0] + START + "\n\n" + appendix(data) + "\n\n" + END + text.split(END)[1]
    BASE.mkdir(parents=True, exist_ok=True)
    (BASE / "history").mkdir(exist_ok=True)
    # A failed render never publishes a new current note or immutable history.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="nastack-inventory-") as directory:
        staged_md, staged_pdf = Path(directory) / "inventory.md", Path(directory) / "inventory.pdf"
        staged_md.write_text(text)
        subprocess.run([args.pdf_python, str(Path(__file__).resolve()), "--render", str(staged_md), str(staged_pdf)], check=True)
        NOTE.write_text(text)
        shutil.copyfile(staged_pdf, PDF)
    data.update(version=args.version, reason=args.reason,
                updated_at=datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
                source_hashes=source_hashes(), markdown_sha256=digest(NOTE), pdf_sha256=digest(PDF))
    CURRENT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    files = {}
    for source, suffix in [(NOTE, ".md"), (PDF, ".pdf"), (CURRENT, ".json")]:
        target = Path(str(prefix) + suffix)
        with target.open("xb") as out:
            out.write(source.read_bytes())
        files[str(target.relative_to(ROOT))] = digest(target)
    history.append({"version": args.version, "updated_at": data["updated_at"], "reason": args.reason, "files": files})
    MANIFEST.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n")
    check()


if __name__ == "__main__":
    main()

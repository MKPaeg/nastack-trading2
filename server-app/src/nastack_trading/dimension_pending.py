"""Public unresolved-data requirements. Never infer private license/key status."""

PENDING = {
    "sector-41": ("exact_index_required", "정확한 공식 디스플레이·OLED 지수 코드·구성·시계열이 미확인입니다. ETF·자체 바스켓으로 대체하지 않습니다."),
    "sector-42": ("exact_index_required", "정확한 공식 공조·냉난방·냉각 지수 코드·구성·시계열이 미확인입니다. 복합 기계/가전 지수나 ETF를 대신 사용하지 않습니다."),
    "sector-43": ("exact_index_required", "정확한 공식 비철금속·제련 지수 코드·구성·시계열이 미확인입니다. 철강 혼합 지수나 ETF로 대체하지 않습니다."),
    "commodity-12": ("licensed_feed_required", "중국 FOB 열연강판 현물 평가와 선물 중 기준 확정 및 LME/Argus 데이터 이용권·접속 권한이 필요합니다. 공개 경로 403을 우회하지 않습니다."),
    "commodity-14": ("license_and_quality_required", "KOMIS 코발트 99.8% Rotterdam/ISE 기준의 저장·가공 이용 허락과 주말·반복값·2026년 원천 변경 검증이 필요합니다. 숫자 열람 성공은 수집 이용권이 아닙니다."),
    "commodity-15": ("license_and_quality_required", "KOMIS 흑연 -194 FOB China의 규격·자료원 변경·주말 값과 저장·가공 이용 허락을 확인해야 합니다. 천연/인조/구상흑연을 임의 대체하지 않습니다."),
    "commodity-22": ("licensed_feed_required", "Platts JKM 원평가의 계약·API 이용권이 필요합니다. World Bank 일본 LNG CIF 및 ICE JKM 선물과 동일하지 않습니다."),
    "commodity-36": ("licensed_history_required", "SCFI 종합지수의 공식 과거 관측·발표일과 저장 이용권이 필요합니다. 공개 HTML 200만으로 시계열 수집 성공으로 보지 않습니다."),
    "market-10": ("storage_permission_required", "ASX 공식 월말 가격표는 있지만 사이트 약관이 S&P 지수값 저장·재배포와 자동 접근을 제한합니다. 승인된 원지수 데이터 feed/저장 이용권 확보 전 수집하지 않습니다."),
    "market-14": ("licensed_history_required", "HSI 공식 지수 페이지는 JavaScript 화면이며 원지수 역사 자료 구독/승인된 배포 경로가 필요합니다. 2800 ETF로 대체하지 않습니다."),
    "market-16": ("licensed_history_required", "STOXX SXXP EUR Price 원지수의 현재 역사 feed와 이용권이 필요합니다. 폐기된 404 URL·SXXR 총수익·EXSA ETF로 우회하지 않습니다."),
    "market-18": ("licensed_history_required", "FTSE 100 UKX 원지수의 승인된 일별 역사 feed가 필요합니다. 공개 안내의 분기 갱신 월말 자료는 일별 종가가 아니며 ISF ETF로 대체하지 않습니다."),
}


def apply_pending(row):
    if row["id"] not in PENDING or row["can_collect"]:
        return
    code, reason = PENDING[row["id"]]
    row.update(
        blocker_code=code, reason=reason, implementation_status="pending",
        implementation_label="미구현 · " + ("정확한 지수 대기" if code == "exact_index_required" else "인증·이용권/검증 대기"),
        assessment_date="2026-10-27", next_action=reason,
    )

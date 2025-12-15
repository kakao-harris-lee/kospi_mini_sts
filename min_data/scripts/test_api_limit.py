#!/usr/bin/env python3
"""
한국투자증권 API의 과거 데이터 조회 가능 기간 테스트.

사용법:
    python scripts/test_api_limit.py
"""
import sys
from datetime import date, timedelta
from pathlib import Path

# 프로젝트 루트를 path에 추가
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.fetch_minute import fetch_minute
from app.futures import get_active_codes_for_date


def test_historical_limit():
    """과거 날짜별로 API 응답 확인하여 조회 제한 기간 파악"""
    today = date.today()

    # 현재 활성 종목 중 첫 번째 사용 (현재 상장된 종목만 조회 가능)
    active_codes = get_active_codes_for_date(today)
    if not active_codes:
        print("오류: 활성 월물을 찾을 수 없습니다.")
        return []

    test_code = active_codes[0]

    print("=" * 60)
    print("한국투자증권 API 과거 데이터 조회 제한 테스트")
    print("=" * 60)
    print(f"테스트 기준일: {today}")
    print(f"테스트 종목: {test_code}")
    print()
    print("주의: 분봉 데이터는 약 2주(15일) 전까지만 조회 가능합니다.")
    print()

    results = []
    last_ok_offset = 0

    # 1일~30일 범위에서 테스트 (평일만)
    for offset in range(1, 31):
        test_date = today - timedelta(days=offset)

        # 주말 건너뛰기
        if test_date.weekday() >= 5:  # 토(5), 일(6)
            continue

        date_str = test_date.strftime("%Y%m%d")
        weekday = test_date.strftime("%a")

        print(f"  테스트 중: {test_code} / {date_str} ({weekday}, {offset}일 전)...", end=" ", flush=True)

        data = fetch_minute(test_code, date_str)

        # 응답 분석
        has_error = "error" in data
        output = data.get("output2", []) or data.get("output", [])
        has_data = len(output) > 0 if output else False

        if has_error:
            error_msg = data.get("error", "")
            print(f"오류: {error_msg[:50]}")
            status = "ERROR"
        elif has_data:
            print(f"OK ({len(output)}건)")
            status = "OK"
            last_ok_offset = offset
        else:
            print("NO_DATA")
            status = "NO_DATA"

        results.append({
            "date": test_date,
            "offset_days": offset,
            "weekday": weekday,
            "code": test_code,
            "status": status,
            "data_count": len(output) if output else 0,
        })

        # 3일 연속 NO_DATA면 중단
        if len(results) >= 3:
            recent = results[-3:]
            if all(r["status"] == "NO_DATA" for r in recent):
                print()
                print("  (3일 연속 NO_DATA - 테스트 중단)")
                break

    # 결과 요약
    print()
    print("=" * 60)
    print("테스트 결과 요약")
    print("=" * 60)
    print(f"{'날짜':<12} {'요일':<4} {'일수':<6} {'상태':<10} {'데이터 수':<10}")
    print("-" * 60)

    for r in results:
        print(f"{r['date']}  {r['weekday']:<4} {r['offset_days']:<6} {r['status']:<10} {r['data_count']:<10}")

    # 제한 추정
    ok_results = [r for r in results if r["status"] == "OK"]
    no_data_results = [r for r in results if r["status"] == "NO_DATA"]

    print()
    print("=" * 60)
    print("결론")
    print("=" * 60)

    if ok_results:
        max_days = max(r["offset_days"] for r in ok_results)
        print(f"API 조회 가능 기간: 최대 {max_days}일 전까지")

        if no_data_results:
            first_no_data = min(r["offset_days"] for r in no_data_results)
            print(f"조회 불가 시작: {first_no_data}일 전부터")
    else:
        print("경고: 조회 성공한 날짜가 없습니다.")
        print("API 연결 상태 또는 종목 코드를 확인하세요.")

    return results


if __name__ == "__main__":
    test_historical_limit()

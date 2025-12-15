#!/usr/bin/env python3
"""
수집된 데이터의 완결성 검증.

사용법:
    python scripts/validate_data.py [--days 365]
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

# 프로젝트 루트를 path에 추가
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import db, config
from app.calendar import get_trading_days_range
from app.futures import get_active_codes_for_date, get_expiry_date, get_listing_start


def validate_collection(start: date, end: date, verbose: bool = False):
    """
    지정 기간의 데이터 완결성 검증.

    Args:
        start: 시작일
        end: 종료일
        verbose: 상세 출력 여부

    Returns:
        누락된 (code, date) 조합 set
    """
    print("=" * 60)
    print("데이터 완결성 검증")
    print("=" * 60)
    print(f"검증 기간: {start} ~ {end}")
    print()

    # 거래일 목록
    trading_days = get_trading_days_range(start, end)
    print(f"거래일 수: {len(trading_days)}")

    # 예상 (code, date) 조합 계산
    print("예상 조합 계산 중...")
    expected = set()
    for day in trading_days:
        # 해당 날짜에 활성인 월물들
        active_codes = get_active_codes_for_date(day)
        for code in active_codes:
            expected.add((code, day))

    print(f"예상 (code, date) 조합 수: {len(expected)}")

    # DB에서 실제 수집된 데이터 조회
    print("DB 조회 중...")
    try:
        client = db.get_client(database=config.CLICKHOUSE_DATABASE)
        collected = db.get_collected_pairs_in_range(client, start, end)
        client.close()
    except Exception as e:
        print(f"DB 연결 오류: {e}")
        return expected  # 모두 누락으로 간주

    print(f"수집된 (code, date) 조합 수: {len(collected)}")

    # 누락된 조합
    missing = expected - collected

    # 추가로 수집되었지만 예상에 없는 조합 (이상 데이터)
    extra = collected - expected

    # 결과 출력
    print()
    print("=" * 60)
    print("검증 결과")
    print("=" * 60)
    print(f"예상 조합: {len(expected)}")
    print(f"수집 완료: {len(collected)}")
    print(f"누락 조합: {len(missing)}")
    print(f"예상 외 데이터: {len(extra)}")

    if len(expected) > 0:
        coverage = (len(collected) / len(expected)) * 100
        print(f"수집률: {coverage:.1f}%")

    if missing and verbose:
        print()
        print("=" * 60)
        print("누락 데이터 상세 (최대 50개)")
        print("=" * 60)
        for code, day in sorted(missing)[:50]:
            print(f"  {code} - {day}")

        if len(missing) > 50:
            print(f"  ... 외 {len(missing) - 50}건")

    if extra and verbose:
        print()
        print("=" * 60)
        print("예상 외 데이터 (최대 20개)")
        print("=" * 60)
        for code, day in sorted(extra)[:20]:
            print(f"  {code} - {day}")

    return missing


def main():
    parser = argparse.ArgumentParser(description="데이터 완결성 검증")
    parser.add_argument("--days", type=int, default=365, help="검증할 일수 (기본: 365)")
    parser.add_argument("--verbose", "-v", action="store_true", help="상세 출력")
    args = parser.parse_args()

    today = date.today()
    start = today - timedelta(days=args.days)
    end = today

    missing = validate_collection(start, end, verbose=args.verbose)

    if missing:
        print()
        print(f"총 {len(missing)}건의 누락 데이터가 있습니다.")
        print("백필을 실행하여 누락 데이터를 수집하세요:")
        print(f"  python collector.py --backfill --days {args.days}")
    else:
        print()
        print("모든 데이터가 수집되었습니다!")


if __name__ == "__main__":
    main()

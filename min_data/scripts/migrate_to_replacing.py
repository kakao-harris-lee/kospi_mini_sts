#!/usr/bin/env python3
"""
기존 MergeTree 테이블을 ReplacingMergeTree로 마이그레이션.

주의: 실행 전 데이터 백업을 권장합니다.

사용법:
    python scripts/migrate_to_replacing.py [--dry-run]
"""
import argparse
import sys
from pathlib import Path

# 프로젝트 루트를 path에 추가
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import db, config


def check_table_engine(client) -> str:
    """현재 테이블 엔진 확인"""
    result = client.query(f"""
        SELECT engine
        FROM system.tables
        WHERE database = '{config.CLICKHOUSE_DATABASE}'
          AND name = 'kospi_mini_1m'
    """)
    if result.result_rows:
        return result.result_rows[0][0]
    return None


def get_row_count(client) -> int:
    """테이블 행 수 조회"""
    result = client.query(f"""
        SELECT count() FROM {config.CLICKHOUSE_DATABASE}.kospi_mini_1m
    """)
    return result.result_rows[0][0] if result.result_rows else 0


def migrate_table(dry_run: bool = False):
    """MergeTree에서 ReplacingMergeTree로 마이그레이션"""
    print("=" * 60)
    print("ClickHouse 테이블 마이그레이션")
    print("MergeTree -> ReplacingMergeTree")
    print("=" * 60)

    client = db.get_client(database=config.CLICKHOUSE_DATABASE)

    # 현재 엔진 확인
    engine = check_table_engine(client)
    if engine is None:
        print("테이블이 존재하지 않습니다.")
        print("새 테이블을 생성합니다...")
        if not dry_run:
            db.ensure_table(client)
            print("테이블 생성 완료 (ReplacingMergeTree)")
        else:
            print("[DRY-RUN] 테이블 생성 예정")
        return

    print(f"현재 테이블 엔진: {engine}")

    if "Replacing" in engine:
        print("이미 ReplacingMergeTree입니다. 마이그레이션 불필요.")
        return

    # 행 수 확인
    row_count = get_row_count(client)
    print(f"현재 데이터 행 수: {row_count:,}")

    if dry_run:
        print()
        print("[DRY-RUN] 다음 작업이 수행될 예정입니다:")
        print("  1. 새 테이블 생성 (kospi_mini_1m_new, ReplacingMergeTree)")
        print("  2. 기존 데이터 복사 (중복 제거)")
        print("  3. 기존 테이블 이름 변경 (kospi_mini_1m -> kospi_mini_1m_old)")
        print("  4. 새 테이블 이름 변경 (kospi_mini_1m_new -> kospi_mini_1m)")
        print()
        print("실제 마이그레이션을 수행하려면 --dry-run 옵션을 제거하세요.")
        return

    print()
    print("마이그레이션을 시작합니다...")

    # 1. 새 테이블 생성
    print("1. 새 테이블 생성 중...")
    client.command(f"""
        CREATE TABLE IF NOT EXISTS {config.CLICKHOUSE_DATABASE}.kospi_mini_1m_new (
            code String,
            datetime DateTime,
            open Float64,
            high Float64,
            low Float64,
            close Float64,
            volume UInt64
        ) ENGINE = ReplacingMergeTree()
        ORDER BY (code, datetime)
    """)

    # 2. 데이터 복사 (중복 제거)
    print("2. 데이터 복사 중 (중복 제거)...")
    client.command(f"""
        INSERT INTO {config.CLICKHOUSE_DATABASE}.kospi_mini_1m_new
        SELECT DISTINCT code, datetime, open, high, low, close, volume
        FROM {config.CLICKHOUSE_DATABASE}.kospi_mini_1m
    """)

    # 복사된 행 수 확인
    new_count = client.query(f"""
        SELECT count() FROM {config.CLICKHOUSE_DATABASE}.kospi_mini_1m_new
    """).result_rows[0][0]
    print(f"   복사된 행 수: {new_count:,}")
    if row_count > new_count:
        print(f"   중복 제거: {row_count - new_count:,}개 행")

    # 3. 테이블 교체
    print("3. 테이블 교체 중...")
    client.command(f"""
        RENAME TABLE
            {config.CLICKHOUSE_DATABASE}.kospi_mini_1m TO {config.CLICKHOUSE_DATABASE}.kospi_mini_1m_old,
            {config.CLICKHOUSE_DATABASE}.kospi_mini_1m_new TO {config.CLICKHOUSE_DATABASE}.kospi_mini_1m
    """)

    print()
    print("=" * 60)
    print("마이그레이션 완료!")
    print("=" * 60)
    print(f"기존 테이블: kospi_mini_1m_old ({row_count:,}행)")
    print(f"새 테이블: kospi_mini_1m ({new_count:,}행)")
    print()
    print("기존 테이블 삭제 방법:")
    print(f"  DROP TABLE {config.CLICKHOUSE_DATABASE}.kospi_mini_1m_old")

    client.close()


def main():
    parser = argparse.ArgumentParser(description="테이블 마이그레이션 (MergeTree -> ReplacingMergeTree)")
    parser.add_argument("--dry-run", action="store_true", help="실제 실행 없이 예상 결과만 출력")
    args = parser.parse_args()

    migrate_table(dry_run=args.dry_run)


if __name__ == "__main__":
    main()

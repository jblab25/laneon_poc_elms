#!/usr/bin/env python3
"""
LANEON PoC — 외부테스트 Test Marker CLI

외부 실환경 테스트 중 실제 상황(맑음/직사광선/그늘/센서 가림/살수/박수/차량통과 등)을
타임스탬프와 함께 기록해, 6시간 Report에서 센서 변화와 나란히 볼 수 있게 한다.
ELMS 백엔드(app.py)가 떠 있지 않아도 동작하도록 DB에 직접 쓴다(저빈도라 안전).

사용 예:
    python3 backend/test_marker_cli.py TEST_START "야외 설치 테스트 시작"
    python3 backend/test_marker_cli.py SENSOR_COVER
    python3 backend/test_marker_cli.py WATER_SPRAY_LIGHT "분무기 5회"
"""
import argparse
import os
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'elms.db')

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS test_marker (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    label TEXT,
    memo TEXT
);
CREATE INDEX IF NOT EXISTS idx_test_marker_ts ON test_marker(ts);
"""


def main():
    parser = argparse.ArgumentParser(description='LANEON PoC Test Marker 기록')
    parser.add_argument('label', help='예: TEST_START, DRY, DIRECT_SUN, SHADE, SENSOR_COVER, '
                                       'WATER_SPRAY_LIGHT, WATER_SPRAY_HEAVY, MIC_CLAP, '
                                       'VEHICLE_PASS, TEST_END (자유 텍스트 가능)')
    parser.add_argument('memo', nargs='?', default='', help='선택 메모')
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute('PRAGMA busy_timeout=5000')
    conn.executescript(_SCHEMA_SQL)
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('INSERT INTO test_marker (ts, label, memo) VALUES (?, ?, ?)',
                 (ts, args.label, args.memo))
    conn.commit()
    conn.close()
    print(f'[TEST_MARKER] {ts}  {args.label}  {args.memo}')


if __name__ == '__main__':
    main()

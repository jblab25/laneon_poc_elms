#!/usr/bin/env python3
"""
LANEON PoC — DB Retention Cleanup

신규 PoC Sensor 상세 History(jsb_packet_history/fusion_history/jpb_status_history/
auto_control_history)만 db_retention_days(기본 14일) 이전 데이터를 삭제한다.

절대 건드리지 않는 것:
- 기존 Legacy `status` 테이블, `slave_config`/`gps_location`/`lane_calibration`
  (운영 설정/상태 테이블 — 이번 구현 범위 밖, 별도 판단 대상)
- `control_event_log`, `test_marker` — 데이터량이 매우 작아 장기보관한다
  (요청에 따라 이번 cleanup 대상에서 제외)

거대한 단일 Transaction 대신 batch 단위(기본 5000행)로 나눠서 삭제하고,
각 batch마다 즉시 commit한다. VACUUM은 실행하지 않는다(별도 유지보수 정책 대상).
테이블별 오류는 서로 영향을 주지 않도록 개별적으로 처리한다.

Report(.md) 파일은 report_retention_days(기본 90일)보다 오래되면 별도로 삭제한다.

운영 프로세스(app.py)와 완전히 분리된 프로세스로, 하루 1회 systemd timer로 실행한다.
"""
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DB_PATH         = os.path.join(BASE_DIR, 'elms.db')
REPORT_CFG_PATH = os.path.join(BASE_DIR, 'report_config.json')
TS_FMT          = '%Y-%m-%d %H:%M:%S'
BATCH_SIZE      = 5000

# 14일 rolling 대상 (Sensor 상세 History만 — Legacy status/설정 테이블은 제외)
SENSOR_DETAIL_TABLES_DEFAULT = [
    'jsb_packet_history',
    'fusion_history',
    'jpb_status_history',
    'auto_control_history',
]


def load_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'[db_retention] 설정 로드 실패({path}): {e} — 기본값 사용')
        return default or {}


def cleanup_table(conn, table, cutoff_ts, batch_size=BATCH_SIZE):
    """id 배치 단위로 SELECT->DELETE. 각 batch가 하나의 작은 Transaction이다."""
    total = 0
    while True:
        rows = conn.execute(
            f'SELECT id FROM {table} WHERE ts < ? LIMIT ?', (cutoff_ts, batch_size)
        ).fetchall()
        if not rows:
            break
        ids = [r[0] for r in rows]
        placeholders = ','.join('?' for _ in ids)
        try:
            conn.execute(f'DELETE FROM {table} WHERE id IN ({placeholders})', ids)
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise
        total += len(ids)
        if len(ids) < batch_size:
            break
    return total


def cleanup_reports(report_dir, cutoff_dt):
    if not os.path.isdir(report_dir):
        return 0
    removed = 0
    for path in glob.glob(os.path.join(report_dir, '*.md')):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
            if mtime < cutoff_dt:
                os.remove(path)
                removed += 1
        except Exception as e:
            print(f'[db_retention] Report 삭제 실패({path}): {e}')
    return removed


def main():
    cfg = load_json(REPORT_CFG_PATH, default={})
    db_retention_days = cfg.get('db_retention_days', 14)
    report_retention_days = cfg.get('report_retention_days', 90)
    sensor_tables = cfg.get('sensor_detail_tables', SENSOR_DETAIL_TABLES_DEFAULT)
    report_dir = os.path.normpath(os.path.join(BASE_DIR, '..', cfg.get('report_dir', 'reports/environment')))

    now = datetime.now()
    db_cutoff = (now - timedelta(days=db_retention_days)).strftime(TS_FMT)
    report_cutoff = now - timedelta(days=report_retention_days)

    print(f'[db_retention] 시작 — DB cutoff={db_cutoff} (retention {db_retention_days}일), '
          f'Report cutoff={report_cutoff.strftime(TS_FMT)} (retention {report_retention_days}일)')

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute('PRAGMA busy_timeout=10000')

    results = {}
    for table in sensor_tables:
        try:
            deleted = cleanup_table(conn, table, db_cutoff)
            results[table] = deleted
            print(f'[db_retention] {table}: {deleted}행 삭제')
        except Exception as e:
            print(f'[db_retention] {table} 정리 실패(다른 테이블에는 영향 없음): {e}', file=sys.stderr)
            results[table] = f'FAILED: {e}'

    conn.close()

    try:
        removed_reports = cleanup_reports(report_dir, report_cutoff)
        print(f'[db_retention] Report 파일 삭제: {removed_reports}개')
    except Exception as e:
        print(f'[db_retention] Report 정리 실패: {e}', file=sys.stderr)

    print('[db_retention] 완료 (VACUUM 미실행, free page는 이후 쓰기에 재사용됨)')
    return results


if __name__ == '__main__':
    main()

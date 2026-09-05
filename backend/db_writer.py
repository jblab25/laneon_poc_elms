"""
LANEON PoC — Background DB Writer

UART 수신/Fusion/AUTO 제어 경로에서 SQLite INSERT를 동기적으로 하지 않기 위한
non-blocking queue + background writer thread. 큐가 가득 차면 즉시 drop하고
db_queue_drop_count만 올린다 — 실제 제어(JSB 수신/Fusion/MANUAL/AUTO/JPB 제어)는
DB 상태와 무관하게 항상 계속되어야 한다.

일부러 복잡한 persistence framework를 만들지 않는다: 테이블별 컬럼 목록 + 큐 +
5초/배치 단위 commit이 전부다.
"""
import atexit
import json
import queue
import sqlite3
import threading
import time

FLUSH_INTERVAL_SEC = 5
MAX_BATCH_ROWS     = 100   # 테이블당 이 이상 쌓이면 주기와 무관하게 즉시 flush
QUEUE_MAXSIZE      = 2000  # bounded — 이 이상은 drop

# 테이블별 컬럼 순서 (id/autoincrement 제외) — INSERT 문 생성에 사용
_TABLE_COLUMNS = {
    'jsb_packet_history': [
        'ts', 'jsb_seq', 'group_seq', 'uptime',
        'ncv_count', 'ncv_json',
        'bme_valid', 'bme_temp_x10', 'bme_hum_x10', 'bme_pres_x10',
        'mic_count', 'mic_json',
        'tcs_valid', 'tcs_json',
        'gps_valid', 'lat_e6', 'lon_e6',
        'raw_packet_size',
        'chunk_error_count_cum', 'incomplete_group_count_cum',
    ],
    'fusion_history': [
        'ts', 'jsb_seq', 'mode',
        'light_raw', 'light_filtered', 'light_target', 'light_level',
        'rain_wet_distance', 'rain_wet_present', 'rain_event_mag', 'rain_event_state',
        'rain_event_count_window', 'rain_target', 'rain_level',
        'fog_humid_ready', 'fog_score', 'fog_target', 'fog_level',
        'rs_mean', 'rs_variation', 'rs_impulse_ratio', 'rs_persistence',
        'mic_rms', 'mic_peak', 'mic_state',
    ],
    'jpb_status_history': [
        'ts', 'jpb_seq',
        'lane1_active', 'lane1_bright', 'lane1_voltage_mv', 'lane1_current_ma',
        'lane2_active', 'lane2_bright', 'lane2_voltage_mv', 'lane2_current_ma',
        'lane3_active', 'lane3_bright', 'lane3_voltage_mv', 'lane3_current_ma',
        'jsb_link_valid', 'jsb_seq', 'jsb_age_ms',
    ],
    'auto_control_history': [
        'ts', 'jsb_seq', 'mode', 'env_level',
        'target_onoff', 'target_bright', 'actual_onoff', 'actual_bright', 'control_match',
    ],
    'control_event_log': [
        'ts', 'mode', 'event_type', 'lanes', 'value', 'reason',
    ],
    'test_marker': [
        'ts', 'label', 'memo',
    ],
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jsb_packet_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    jsb_seq INTEGER,
    group_seq INTEGER,
    uptime INTEGER,
    ncv_count INTEGER,
    ncv_json TEXT,
    bme_valid INTEGER,
    bme_temp_x10 INTEGER,
    bme_hum_x10 INTEGER,
    bme_pres_x10 INTEGER,
    mic_count INTEGER,
    mic_json TEXT,
    tcs_valid INTEGER,
    tcs_json TEXT,
    gps_valid INTEGER,
    lat_e6 INTEGER,
    lon_e6 INTEGER,
    raw_packet_size INTEGER,
    chunk_error_count_cum INTEGER,
    incomplete_group_count_cum INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jsb_packet_history_ts ON jsb_packet_history(ts);

CREATE TABLE IF NOT EXISTS fusion_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    jsb_seq INTEGER,
    mode TEXT,
    light_raw REAL,
    light_filtered REAL,
    light_target INTEGER,
    light_level INTEGER,
    rain_wet_distance REAL,
    rain_wet_present INTEGER,
    rain_event_mag REAL,
    rain_event_state TEXT,
    rain_event_count_window INTEGER,
    rain_target INTEGER,
    rain_level INTEGER,
    fog_humid_ready INTEGER,
    fog_score REAL,
    fog_target INTEGER,
    fog_level INTEGER,
    rs_mean REAL,
    rs_variation REAL,
    rs_impulse_ratio REAL,
    rs_persistence REAL,
    mic_rms REAL,
    mic_peak REAL,
    mic_state TEXT
);
CREATE INDEX IF NOT EXISTS idx_fusion_history_ts ON fusion_history(ts);

CREATE TABLE IF NOT EXISTS jpb_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    jpb_seq INTEGER,
    lane1_active INTEGER, lane1_bright INTEGER, lane1_voltage_mv INTEGER, lane1_current_ma INTEGER,
    lane2_active INTEGER, lane2_bright INTEGER, lane2_voltage_mv INTEGER, lane2_current_ma INTEGER,
    lane3_active INTEGER, lane3_bright INTEGER, lane3_voltage_mv INTEGER, lane3_current_ma INTEGER,
    jsb_link_valid INTEGER,
    jsb_seq INTEGER,
    jsb_age_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jpb_status_history_ts ON jpb_status_history(ts);

CREATE TABLE IF NOT EXISTS auto_control_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    jsb_seq INTEGER,
    mode TEXT,
    env_level INTEGER,
    target_onoff INTEGER,
    target_bright INTEGER,
    actual_onoff INTEGER,
    actual_bright INTEGER,
    control_match INTEGER
);
CREATE INDEX IF NOT EXISTS idx_auto_control_history_ts ON auto_control_history(ts);

CREATE TABLE IF NOT EXISTS control_event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT,
    event_type TEXT,
    lanes INTEGER,
    value INTEGER,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_control_event_log_ts ON control_event_log(ts);

CREATE TABLE IF NOT EXISTS test_marker (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    label TEXT,
    memo TEXT
);
CREATE INDEX IF NOT EXISTS idx_test_marker_ts ON test_marker(ts);
"""


def init_history_tables(db_path: str):
    conn = sqlite3.connect(db_path, timeout=5)
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    conn.close()
    print('[DB_WRITER] History table 스키마 준비 완료')


_queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
_stop_event = threading.Event()
_writer_thread = None

_drop_lock = threading.Lock()
_db_queue_drop_count = 0


def enqueue(table: str, row: dict):
    """Non-blocking. 큐가 가득 차면 즉시 drop하고 카운터만 올린다 — 절대 blocking하지 않는다."""
    global _db_queue_drop_count
    try:
        _queue.put_nowait((table, row))
    except queue.Full:
        with _drop_lock:
            _db_queue_drop_count += 1
        # 매번 로그 찍으면 오히려 flood가 되므로 낮은 빈도로만 남긴다
        if _db_queue_drop_count % 50 == 1:
            print(f'[DB_WRITER] queue full — 기록 drop (누적 {_db_queue_drop_count})')
    except Exception as e:
        # 어떤 이유로든 enqueue 자체가 실패해도 호출자(제어 경로)에는 영향 없어야 한다
        print(f'[DB_WRITER] enqueue 실패: {e}')


def get_drop_count() -> int:
    with _drop_lock:
        return _db_queue_drop_count


def _flush(conn, buffers: dict):
    for table, rows in buffers.items():
        if not rows:
            continue
        try:
            cols = _TABLE_COLUMNS[table]
            placeholders = ','.join('?' for _ in cols)
            sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
            values = [tuple(r.get(c) for c in cols) for r in rows]
            conn.executemany(sql, values)
        except Exception as e:
            print(f'[DB_WRITER] {table} 기록 실패({len(rows)}건 유실): {e}')
        finally:
            rows.clear()
    try:
        conn.commit()
    except Exception as e:
        print(f'[DB_WRITER] commit 실패: {e}')


def _writer_loop(db_path: str):
    try:
        conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        conn.execute('PRAGMA busy_timeout=5000')
    except Exception as e:
        print(f'[DB_WRITER] DB 연결 실패, writer 중단: {e}')
        return

    buffers = {t: [] for t in _TABLE_COLUMNS}
    last_flush = time.time()

    while not _stop_event.is_set():
        try:
            table, row = _queue.get(timeout=0.5)
            if table in buffers:
                buffers[table].append(row)
        except queue.Empty:
            pass

        now = time.time()
        total = sum(len(v) for v in buffers.values())
        due_by_time = (now - last_flush) >= FLUSH_INTERVAL_SEC and total > 0
        due_by_size = any(len(v) >= MAX_BATCH_ROWS for v in buffers.values())
        if due_by_time or due_by_size:
            _flush(conn, buffers)
            last_flush = now

    # 종료 시 큐에 남은 것까지 최대한 flush
    drained = 0
    while True:
        try:
            table, row = _queue.get_nowait()
            if table in buffers:
                buffers[table].append(row)
                drained += 1
        except queue.Empty:
            break
    if drained:
        print(f'[DB_WRITER] 종료 전 잔여 {drained}건 flush 시도')
    _flush(conn, buffers)
    conn.close()


def start(db_path: str):
    global _writer_thread
    init_history_tables(db_path)
    _writer_thread = threading.Thread(target=_writer_loop, args=(db_path,), daemon=True)
    _writer_thread.start()
    atexit.register(shutdown)
    print('[DB_WRITER] 백그라운드 writer 시작')


def shutdown():
    if _stop_event.is_set():
        return
    _stop_event.set()
    if _writer_thread is not None:
        _writer_thread.join(timeout=5)

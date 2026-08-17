"""
ELMS Backend — Flask + Socket.IO + UART + SQLite + MQTT
Pi Zero ↔ STM32 마스터 (UART1, 115200bps)
"""
import os
import json
import struct
import sqlite3
import threading
import time
from datetime import datetime

import serial
import paho.mqtt.client as mqtt
from flask import Flask, jsonify, request, send_from_directory, make_response
from flask_socketio import SocketIO

# ──────────────────────────────────────────
# 설정
# ──────────────────────────────────────────
SERIAL_PORT = '/dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.4:1.0-port0'
SERIAL_BAUD = 115200
DB_PATH     = os.path.join(os.path.dirname(__file__), 'elms.db')

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
PIZERO_DIR = os.path.normpath(os.path.join(BASE_DIR, '..'))
SCRIPT_DIR = os.path.join(PIZERO_DIR, 'elms01', 'src')

# STM32 프로토콜 커맨드
CMD_STATUS_REQ  = 0x01
CMD_STATUS_RESP = 0x02
CMD_ONOFF       = 0x10
CMD_BRIGHTNESS  = 0x11
CMD_MODE        = 0x13
CMD_ACK         = 0x06
CMD_GPS_REQ     = 0x20
CMD_GPS_RESP    = 0x21
CMD_CAL_SET     = 0x14
CMD_CAL_GET     = 0x15
CMD_CAL_RESP    = 0x16
CMD_BOOT_NOTIFY = 0x17

# 레벨별 기본 캘리브레이션 스텝 (MCU 리셋 시 초기값과 동일)
CAL_DEFAULT_STEPS = {1: 98, 2: 95, 3: 92, 4: 88}

# MQTT 설정 (Global 서버)
MQTT_BROKER       = '192.168.1.108'
MQTT_PORT         = 1883
MQTT_TOPIC_PREFIX = 'sensors'
EDGE_ID           = 'pi_zero_01'

# ──────────────────────────────────────────
# Flask / Socket.IO 초기화
# ──────────────────────────────────────────
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

# ──────────────────────────────────────────
# UART 초기화
# ──────────────────────────────────────────
ser = None
_ser_real_path = None  # 마지막으로 연결에 성공한 실제 장치 경로 (ttyUSB 번호 변경 감지용)
serial_lock = threading.Lock()


def _open_serial():
    """포트를 (재)오픈한다. USB-UART 어댑터가 물리적으로 재연결되어도
    /dev/serial/by-path/... 심볼릭 링크는 그대로라 안전하게 재시도 가능하다."""
    global ser, _ser_real_path
    try:
        ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.05)
        _ser_real_path = os.path.realpath(SERIAL_PORT)
        print(f'[UART] {SERIAL_PORT} 열림')
    except Exception as e:
        ser = None
        _ser_real_path = None
        print(f'[UART] 포트 열기 실패: {e} (시뮬레이션 모드)')


_open_serial()


def _crc(data) -> int:
    crc = 0
    for b in data:
        crc ^= b
    return crc & 0xFF


def uart_send(cmd: int, payload: bytes):
    """STM32 프로토콜 프레임 송신: [0xAA][CMD][LEN][PAYLOAD][CRC]"""
    global ser
    print(f'[UART TX] CMD=0x{cmd:02X} payload={payload.hex()}')
    if ser is None:
        return
    frame = bytearray([0xAA, cmd, len(payload)]) + bytearray(payload)
    frame.append(_crc(frame))
    with serial_lock:
        try:
            ser.write(bytes(frame))
        except Exception as e:
            print(f'[UART] 송신 실패 — 연결 끊김으로 판단, 재연결 대기: {e}')
            try:
                ser.close()
            except Exception:
                pass
            ser = None


# ──────────────────────────────────────────
# UART 수신 스레드
# ──────────────────────────────────────────
def uart_reader():
    global ser
    buf = bytearray()
    last_reconnect_attempt = 0.0
    last_link_check = 0.0
    while True:
        if ser is None:
            now = time.time()
            if now - last_reconnect_attempt >= 3:
                last_reconnect_attempt = now
                _open_serial()
                if ser is not None:
                    resend_all_calibration()
            time.sleep(0.5)
            continue

        # read/write에서 예외가 안 뜨는 경우를 대비해, 실제 장치 경로가
        # 바뀌었는지(USB 재연결로 ttyUSB 번호가 변경됐는지) 주기적으로 확인한다.
        now = time.time()
        if now - last_link_check >= 2:
            last_link_check = now
            if os.path.realpath(SERIAL_PORT) != _ser_real_path:
                print('[UART] 장치 경로 변경 감지 — 재연결 시도')
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                continue

        try:
            chunk = ser.read(64)
        except Exception as e:
            print(f'[UART] 수신 실패 — 연결 끊김으로 판단, 재연결 대기: {e}')
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            time.sleep(0.1)
            continue

        for b in chunk:
            if len(buf) == 0 and b != 0xAA:
                continue
            buf.append(b)
            if len(buf) >= 3:
                expected = 3 + buf[2] + 1
                if len(buf) >= expected:
                    frame = buf[:expected]
                    buf = buf[expected:]
                    if _crc(frame[:-1]) == frame[-1]:
                        print(f'[UART RX] CMD=0x{frame[1]:02X} payload={frame[3:3+frame[2]].hex()}')
                        _handle_frame(frame[1], bytes(frame[3:3 + frame[2]]))
                    else:
                        print(f'[UART RX] CRC 불일치, 폐기: {frame.hex()}')
                        buf = bytearray()


def _handle_frame(cmd: int, payload: bytes):
    if cmd == CMD_STATUS_RESP:
        _handle_status(payload)
    elif cmd == CMD_GPS_RESP:
        _handle_gps(payload)
    elif cmd == CMD_CAL_RESP:
        _handle_cal_resp(payload)
    elif cmd == CMD_BOOT_NOTIFY:
        _handle_boot_notify(payload)
    elif cmd == CMD_ACK:
        pass


def _handle_status(payload: bytes):
    """
    STATUS_RESP 페이로드 파싱 (19 bytes)
    [0]     slave_id
    [1..6]  v[0..2]  uint16 LE (mV)
    [7..12] i[0..2]  uint16 LE (mA)
    [13]    rain level (0~4)
    [14]    fog level (0~4)
    [15]    lane_state bitmask (bit0=CH1, bit1=CH2, bit2=CH3)
    [16]    밝기 packed (하위nibble=lane1, 상위nibble=lane2)
    [17]    lane3 밝기
    [18]    light(조도) level (0~4)
    """
    if len(payload) < 15:
        return

    slave_id     = payload[0]
    v            = struct.unpack_from('<HHH', payload, 1)
    i_raw        = struct.unpack_from('<HHH', payload, 7)
    rain         = payload[13]
    fog          = payload[14]
    lane_state   = payload[15] if len(payload) > 15 else 0
    bright_12    = payload[16] if len(payload) > 16 else 0
    bright_lane3 = payload[17] if len(payload) > 17 else 0
    cds          = payload[18] if len(payload) > 18 else 0
    brightness   = [bright_12 & 0x0F, (bright_12 >> 4) & 0x0F, bright_lane3 & 0x0F]

    status = {
        'slave_id':   slave_id,
        'voltage':    [round(v[0]/1000.0, 3), round(v[1]/1000.0, 3), round(v[2]/1000.0, 3)],
        'current':    [round(i_raw[0]/1000.0, 3), round(i_raw[1]/1000.0, 3), round(i_raw[2]/1000.0, 3)],
        'lane_state': lane_state,
        'brightness': brightness,
        'cds':        cds,
        'rain':       rain,
        'fog':        fog,
        'timestamp':  datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    _save_status(status)
    mqtt_publish(status)
    socketio.emit('status_update', status)


# ──────────────────────────────────────────
# 데이터베이스
# ──────────────────────────────────────────
def get_db():
    """스레드 간 동시 접근 시 'database is locked' 오류를 줄이기 위해
    busy_timeout을 설정한 커넥션을 반환한다."""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


def init_db():
    conn = get_db()
    conn.execute('PRAGMA journal_mode=WAL')

    # 1. 이력 로그 테이블
    conn.execute('''
        CREATE TABLE IF NOT EXISTS status (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT,
            slave_id   INTEGER,
            v1 REAL, v2 REAL, v3 REAL,
            i1 REAL, i2 REAL, i3 REAL,
            cds        INTEGER,
            rain       INTEGER,
            lane_state INTEGER DEFAULT 0,
            bright1    INTEGER DEFAULT 0,
            bright2    INTEGER DEFAULT 0,
            bright3    INTEGER DEFAULT 0
        )
    ''')

    # 기존 테이블에 신규 컬럼 없으면 자동 추가 (마이그레이션)
    cur = conn.execute("PRAGMA table_info(status)")
    existing = [r[1] for r in cur.fetchall()]
    for col, typedef in [('lane_state','INTEGER DEFAULT 0'),
                         ('bright1','INTEGER DEFAULT 0'),
                         ('bright2','INTEGER DEFAULT 0'),
                         ('bright3','INTEGER DEFAULT 0'),
                         ('fog','INTEGER DEFAULT 0')]:
        if col not in existing:
            conn.execute(f'ALTER TABLE status ADD COLUMN {col} {typedef}')

    # 2. 슬레이브별 스케줄 설정 테이블
    conn.execute('''
        CREATE TABLE IF NOT EXISTS slave_config (
            slave_id      INTEGER PRIMARY KEY,
            on_time       TEXT DEFAULT '18:00',
            off_time      TEXT DEFAULT '06:00',
            auto_schedule INTEGER DEFAULT 1
        )
    ''')
    for i in range(1, 4):
        conn.execute(
            'INSERT OR IGNORE INTO slave_config (slave_id) VALUES (?)', (i,)
        )

    # 3. 파워보드 GPS 위치 (단일 row)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS gps_location (
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            fix_valid  INTEGER DEFAULT 0,
            latitude   REAL,
            longitude  REAL,
            updated_ts TEXT
        )
    ''')

    # 4. 슬레이브·Lane·레벨별 캘리브레이션 스텝 (MCU는 RAM 전용이라 Pi가 영구 보관)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS lane_calibration (
            slave_id INTEGER,
            lane     INTEGER,
            level    INTEGER,
            step     INTEGER,
            PRIMARY KEY (slave_id, lane, level)
        )
    ''')
    for slave_id in range(1, 4):
        for lane in range(1, 4):
            for level, step in CAL_DEFAULT_STEPS.items():
                conn.execute(
                    'INSERT OR IGNORE INTO lane_calibration (slave_id, lane, level, step) '
                    'VALUES (?, ?, ?, ?)',
                    (slave_id, lane, level, step)
                )

    conn.commit()
    conn.close()
    print('[DB] 초기화 완료')


def _save_status(s: dict):
    conn = get_db()
    conn.execute(
        'INSERT INTO status '
        '(ts,slave_id,v1,v2,v3,i1,i2,i3,cds,rain,fog,lane_state,bright1,bright2,bright3) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (s['timestamp'], s['slave_id'],
         s['voltage'][0], s['voltage'][1], s['voltage'][2],
         s['current'][0], s['current'][1], s['current'][2],
         s['cds'], s['rain'], s['fog'], s['lane_state'],
         s['brightness'][0], s['brightness'][1], s['brightness'][2])
    )
    conn.commit()
    conn.close()


def _handle_gps(payload: bytes):
    """
    GPS_RESP 페이로드 파싱 (9 bytes)
    [0]    fix_valid
    [1..4] latitude  int32 LE (raw / 1_000_000)
    [5..8] longitude int32 LE (raw / 1_000_000)
    """
    if len(payload) < 9:
        return

    fix_valid = payload[0]
    lat_raw, lon_raw = struct.unpack_from('<ii', payload, 1)

    gps = {
        'fix_valid':  bool(fix_valid),
        'latitude':   lat_raw / 1_000_000,
        'longitude':  lon_raw / 1_000_000,
        'updated_ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    conn = get_db()
    conn.execute(
        'INSERT INTO gps_location (id, fix_valid, latitude, longitude, updated_ts) '
        'VALUES (1, ?, ?, ?, ?) '
        'ON CONFLICT(id) DO UPDATE SET '
        'fix_valid=excluded.fix_valid, latitude=excluded.latitude, '
        'longitude=excluded.longitude, updated_ts=excluded.updated_ts',
        (int(fix_valid), gps['latitude'], gps['longitude'], gps['updated_ts'])
    )
    conn.commit()
    conn.close()

    socketio.emit('gps_update', gps)


def _handle_cal_resp(payload: bytes):
    """
    CAL_RESP 페이로드 파싱 (4 bytes)
    [0] slave_id
    [1] lane
    [2] level
    [3] step
    """
    if len(payload) < 4:
        return

    slave_id, lane, level, step = payload[0], payload[1], payload[2], payload[3]

    conn = get_db()
    conn.execute(
        'INSERT INTO lane_calibration (slave_id, lane, level, step) '
        'VALUES (?, ?, ?, ?) '
        'ON CONFLICT(slave_id, lane, level) DO UPDATE SET step=excluded.step',
        (slave_id, lane, level, step)
    )
    conn.commit()
    conn.close()

    socketio.emit('cal_update', {
        'slave_id': slave_id, 'lane': lane, 'level': level, 'step': step,
    })


def _handle_boot_notify(payload: bytes):
    """
    BOOT_NOTIFY 페이로드 (1 byte)
    [0] slave_id
    MCU가 재부팅되면(캘리브레이션 값이 RAM 전용이라 초기화됨) 이 프레임을 2초 간격으로
    재전송한다. Pi는 즉시 ACK(1)로 응답해 재전송을 멈추게 하고, 해당 슬레이브에 저장해둔
    캘리브레이션 값을 전부 다시 밀어준다.
    """
    if len(payload) < 1:
        return

    slave_id = payload[0]
    uart_send(CMD_ACK, bytes([1]))

    conn = get_db()
    rows = conn.execute(
        'SELECT lane, level, step FROM lane_calibration WHERE slave_id=?',
        (slave_id,)
    ).fetchall()
    conn.close()

    for lane, level, step in rows:
        uart_send(CMD_CAL_SET, bytes([slave_id, lane, level, step]))
    print(f'[CAL] Slave{slave_id} BOOT_NOTIFY 수신 → 캘리브레이션 {len(rows)}건 재전송')


# ──────────────────────────────────────────
# MQTT (Global 서버 발행)
# ──────────────────────────────────────────
_mqtt_client = None


def init_mqtt():
    global _mqtt_client
    try:
        _mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
        _mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        _mqtt_client.loop_start()
        print(f'[MQTT] 브로커 연결: {MQTT_BROKER}:{MQTT_PORT}')
    except Exception as e:
        _mqtt_client = None
        print(f'[MQTT] 연결 실패 (오프라인 모드): {e}')


def mqtt_publish(status: dict):
    """
    슬레이브별 실제 lane 데이터를 그대로 발행
    topic: sensors/pi_zero_01/slave{N}
    """
    if _mqtt_client is None:
        return

    slave_id = status['slave_id']
    ls = status['lane_state']

    payload = {
        'server_id': f'{EDGE_ID}_slave{slave_id}',
        'slave_id':  slave_id,
        'status': {
            'lane1': {
                'voltage':    status['voltage'][0],
                'current':    status['current'][0],
                'on':         bool((ls >> 0) & 1),
                'brightness': status['brightness'][0],
            },
            'lane2': {
                'voltage':    status['voltage'][1],
                'current':    status['current'][1],
                'on':         bool((ls >> 1) & 1),
                'brightness': status['brightness'][1],
            },
            'lane3': {
                'voltage':    status['voltage'][2],
                'current':    status['current'][2],
                'on':         bool((ls >> 2) & 1),
                'brightness': status['brightness'][2],
            },
        },
        'sensor': {
            'cds':  status['cds'],
            'rain': status['rain'],
            'fog':  status['fog'],
            'vis':  0,
        },
        'timestamp': status['timestamp'],
    }
    topic = f'{MQTT_TOPIC_PREFIX}/{EDGE_ID}/slave{slave_id}'
    try:
        _mqtt_client.publish(topic, json.dumps(payload))
        print(f'[MQTT] 발행: {topic}')
    except Exception as e:
        print(f'[MQTT] 발행 실패: {e}')


def resend_all_calibration():
    """MCU는 캘리브레이션 값을 RAM에만 들고 있어 전원이 끊기면 기본값으로 리셋된다.
    Pi가 DB에 보관해둔 값을 서비스 시작마다 전부 다시 밀어준다."""
    conn = get_db()
    rows = conn.execute('SELECT slave_id, lane, level, step FROM lane_calibration').fetchall()
    conn.close()
    for slave_id, lane, level, step in rows:
        uart_send(CMD_CAL_SET, bytes([slave_id, lane, level, step]))
    print(f'[CAL] 캘리브레이션 {len(rows)}건 재전송 완료')


# ──────────────────────────────────────────
# 상태 폴링 (슬레이브별 순차 STATUS_REQ)
# ──────────────────────────────────────────
def status_poller():
    """슬레이브 1→2→3 순서로 5초 간격 STATUS_REQ를 보내 상태 갱신을 유도한다.
    슬레이브당 5초 간격이므로 전체 한 바퀴는 15초."""
    slave_id = 1
    while True:
        uart_send(CMD_STATUS_REQ, bytes([slave_id]))
        slave_id = slave_id % 3 + 1
        time.sleep(5)


# ──────────────────────────────────────────
# 스케줄러 (점등/소등 시간 자동 제어)
# ──────────────────────────────────────────
def schedule_checker():
    """매분 on_time/off_time을 확인해 슬레이브 자동 점소등"""
    while True:
        now = datetime.now().strftime('%H:%M')
        try:
            conn = get_db()
            rows = conn.execute(
                'SELECT slave_id, on_time, off_time FROM slave_config WHERE auto_schedule=1'
            ).fetchall()
            conn.close()
            for slave_id, on_time, off_time in rows:
                if now == on_time:
                    uart_send(CMD_ONOFF, bytes([slave_id, 0x07, 1]))
                    print(f'[SCHEDULE] Slave{slave_id} 점등 ({on_time})')
                elif now == off_time:
                    uart_send(CMD_ONOFF, bytes([slave_id, 0x07, 0]))
                    print(f'[SCHEDULE] Slave{slave_id} 소등 ({off_time})')
        except Exception as e:
            print(f'[SCHEDULE] 오류: {e}')
        time.sleep(60)


# ──────────────────────────────────────────
# REST API — 기존
# ──────────────────────────────────────────
@app.route('/api/control/onoff', methods=['POST'])
def api_onoff():
    d        = request.json
    slave_id = int(d.get('slave_id', 1))
    lanes    = int(d.get('lanes', 7))
    on       = int(d.get('on', 1))
    uart_send(CMD_ONOFF, bytes([slave_id, lanes, on]))
    return jsonify({'ok': True})


@app.route('/api/control/brightness', methods=['POST'])
def api_brightness():
    d        = request.json
    slave_id = int(d.get('slave_id', 1))
    lane     = int(d.get('lane', 1))
    value    = int(d.get('value', 2))
    uart_send(CMD_BRIGHTNESS, bytes([slave_id, lane, value]))
    return jsonify({'ok': True})


@app.route('/api/control/mode', methods=['POST'])
def api_mode():
    d        = request.json
    slave_id = int(d.get('slave_id', 1))
    mode     = int(d.get('mode', 0))
    uart_send(CMD_MODE, bytes([slave_id, mode]))
    return jsonify({'ok': True})


# ──────────────────────────────────────────
# REST API — 파워보드 GPS 위치 (버튼 요청 시에만 조회)
# ──────────────────────────────────────────
@app.route('/api/gps', methods=['GET'])
def api_gps_get():
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM gps_location WHERE id=1').fetchone()
    conn.close()
    return jsonify(dict(row) if row else {})


@app.route('/api/gps/request', methods=['POST'])
def api_gps_request():
    uart_send(CMD_GPS_REQ, b'')
    return jsonify({'ok': True})


# ──────────────────────────────────────────
# REST API — Lane 초기 전압 캘리브레이션 튜닝
# ──────────────────────────────────────────
@app.route('/api/cal', methods=['GET'])
def api_cal_get():
    slave_id = int(request.args.get('slave_id', 1))
    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT lane, level, step FROM lane_calibration '
        'WHERE slave_id=? ORDER BY lane, level',
        (slave_id,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/control/cal', methods=['POST'])
def api_cal_set():
    d        = request.json
    slave_id = int(d.get('slave_id', 1))
    lane     = int(d.get('lane', 1))
    level    = int(d.get('level', 1))
    step     = int(d.get('step', 0))

    uart_send(CMD_CAL_SET, bytes([slave_id, lane, level, step]))

    conn = get_db()
    conn.execute(
        'INSERT INTO lane_calibration (slave_id, lane, level, step) '
        'VALUES (?, ?, ?, ?) '
        'ON CONFLICT(slave_id, lane, level) DO UPDATE SET step=excluded.step',
        (slave_id, lane, level, step)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/control/cal/request', methods=['POST'])
def api_cal_request():
    d        = request.json
    slave_id = int(d.get('slave_id', 1))
    lane     = int(d.get('lane', 1))
    level    = int(d.get('level', 1))

    uart_send(CMD_CAL_GET, bytes([slave_id, lane, level]))
    return jsonify({'ok': True})


@app.route('/api/history')
def api_history():
    from_dt  = request.args.get('from', '2000-01-01') + ' 00:00:00'
    to_dt    = request.args.get('to',   '2099-12-31') + ' 23:59:59'
    slave_id = request.args.get('slave_id')

    conn = get_db()
    conn.row_factory = sqlite3.Row
    if slave_id:
        rows = conn.execute(
            'SELECT * FROM status WHERE ts BETWEEN ? AND ? AND slave_id=? '
            'ORDER BY ts DESC LIMIT 500',
            (from_dt, to_dt, int(slave_id))
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT * FROM status WHERE ts BETWEEN ? AND ? '
            'ORDER BY ts DESC LIMIT 500',
            (from_dt, to_dt)
        ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/history/csv')
def api_history_csv():
    from_dt  = request.args.get('from', '2000-01-01') + ' 00:00:00'
    to_dt    = request.args.get('to',   '2099-12-31') + ' 23:59:59'
    slave_id = request.args.get('slave_id')

    conn = get_db()
    if slave_id:
        rows = conn.execute(
            'SELECT ts,slave_id,v1,v2,v3,i1,i2,i3,cds,rain,fog,lane_state,bright1,bright2,bright3 '
            'FROM status WHERE ts BETWEEN ? AND ? AND slave_id=? ORDER BY ts',
            (from_dt, to_dt, int(slave_id))
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT ts,slave_id,v1,v2,v3,i1,i2,i3,cds,rain,fog,lane_state,bright1,bright2,bright3 '
            'FROM status WHERE ts BETWEEN ? AND ? ORDER BY ts',
            (from_dt, to_dt)
        ).fetchall()
    conn.close()

    lines = ['timestamp,slave_id,v1,v2,v3,i1,i2,i3,cds,rain,fog,lane_state,bright1,bright2,bright3']
    for r in rows:
        lines.append(','.join(str(x) for x in r))

    resp = make_response('\n'.join(lines))
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename=elms_history.csv'
    return resp


# ──────────────────────────────────────────
# REST API — 스케줄 설정
# ──────────────────────────────────────────
@app.route('/api/schedule', methods=['GET'])
def api_schedule_get():
    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT * FROM slave_config').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/schedule', methods=['POST'])
def api_schedule_set():
    d        = request.json
    slave_id = int(d.get('slave_id', 1))
    on_time  = d.get('on_time',  '18:00')
    off_time = d.get('off_time', '06:00')
    auto     = int(d.get('auto_schedule', 1))
    conn = get_db()
    conn.execute(
        'UPDATE slave_config SET on_time=?, off_time=?, auto_schedule=? WHERE slave_id=?',
        (on_time, off_time, auto, slave_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ──────────────────────────────────────────
# 정적 파일 (대시보드)
# ──────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory(PIZERO_DIR, 'index.html')


@app.route('/static/script.js')
def script_js():
    return send_from_directory(SCRIPT_DIR, 'script.js')


# ──────────────────────────────────────────
# Socket.IO 이벤트
# ──────────────────────────────────────────
@socketio.on('connect')
def on_connect():
    print('[WS] 클라이언트 연결')


@socketio.on('disconnect')
def on_disconnect():
    print('[WS] 클라이언트 연결 해제')


# ──────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    init_mqtt()
    threading.Thread(target=uart_reader,     daemon=True).start()
    threading.Thread(target=status_poller,   daemon=True).start()
    threading.Thread(target=schedule_checker, daemon=True).start()
    resend_all_calibration()
    print('[SERVER] http://0.0.0.0:5000 시작')
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)

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

import env_fusion
import db_writer

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

# PoC 전용: JPB(파워보드) 상태 + JSB(센서보드) Raw 데이터 청크
CMD_JPB_STATUS      = 0x30
CMD_JSB_DATA_CHUNK  = 0x31

# PoC 전용: ELMS Fusion 결과(L/R/F) → JPB 상태 표시/진단용 (제어용 아님).
# 0x12는 기존 ELMS 코드와 실기 Hex Dump 양쪽에서 미사용 확인됨(2026-09-03 감사).
CMD_ENV_STATUS = 0x12

JSB_CHUNK_TIMEOUT_SEC = 3.0

# 이번 PoC는 JPB 1대 구성이라 slave_id는 항상 1
SLAVE_ID = 1

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
    elif cmd == CMD_JPB_STATUS:
        _handle_jpb_status(payload)
    elif cmd == CMD_JSB_DATA_CHUNK:
        _handle_jsb_chunk(payload)
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
# PoC — JPB STATUS (0x30) / JSB DATA CHUNK (0x31)
# ──────────────────────────────────────────
_state_lock = threading.Lock()
latest_jpb_status = {}
latest_jsb_sensor = {}

_jsb_chunk_lock = threading.Lock()
_jsb_reassembly = {
    'group_seq':    None,
    'total_chunks': None,
    'chunks':       {},
    'tainted':      False,
    'started_at':   None,
}
_jsb_diag = {
    'chunk_error_count':      0,
    'incomplete_group_count': 0,
    'last_complete_group_seq': None,
    'last_group_seq':         None,
    'last_chunks_received':   0,
    'last_total_chunks':      0,
    'last_raw_packet_size':   0,
}


def _handle_jpb_status(payload: bytes):
    """
    JPB_STATUS(0x30) 페이로드 파싱 (29 bytes)
    JPB retarget.c/h 소스가 없어 실기로 byte order를 직접 확인하지 못했다.
    기존 ELMS 프로토콜 관례(멀티바이트 필드 전부 little-endian, 패딩 없음)를
    그대로 적용했으며, 아래 오프셋 합산이 스펙의 29바이트와 정확히 일치해
    구조적으로는 맞아떨어진다 — 실기 Hex Dump로 재검증할 것.
    """
    if len(payload) < 29:
        print(f'[JPB_STATUS] payload 길이 부족: {len(payload)}')
        return

    jpb_seq = struct.unpack_from('<I', payload, 0)[0]

    lanes = []
    off = 4
    for _ in range(3):
        active      = payload[off]
        bright      = payload[off + 1]
        voltage_mv, current_ma = struct.unpack_from('<HH', payload, off + 2)
        lanes.append({
            'active':       bool(active),
            'bright_level': bright,
            'voltage_mv':   voltage_mv,
            'current_ma':   current_ma,
        })
        off += 6

    jsb_link_valid = payload[off]
    jsb_seq         = struct.unpack_from('<I', payload, off + 1)[0]
    jsb_age_ms      = struct.unpack_from('<H', payload, off + 5)[0]

    status = {
        'jpb_seq':        jpb_seq,
        'lane1':          lanes[0],
        'lane2':          lanes[1],
        'lane3':          lanes[2],
        'jsb_link_valid': bool(jsb_link_valid),
        'jsb_seq':        jsb_seq,
        'jsb_age_ms':     jsb_age_ms,
        'timestamp':      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    global latest_jpb_status
    with _state_lock:
        latest_jpb_status = status
    socketio.emit('jpb_status_update', status)

    try:
        db_writer.enqueue('jpb_status_history', {
            'ts': status['timestamp'],
            'jpb_seq': jpb_seq,
            'lane1_active': int(lanes[0]['active']), 'lane1_bright': lanes[0]['bright_level'],
            'lane1_voltage_mv': lanes[0]['voltage_mv'], 'lane1_current_ma': lanes[0]['current_ma'],
            'lane2_active': int(lanes[1]['active']), 'lane2_bright': lanes[1]['bright_level'],
            'lane2_voltage_mv': lanes[1]['voltage_mv'], 'lane2_current_ma': lanes[1]['current_ma'],
            'lane3_active': int(lanes[2]['active']), 'lane3_bright': lanes[2]['bright_level'],
            'lane3_voltage_mv': lanes[2]['voltage_mv'], 'lane3_current_ma': lanes[2]['current_ma'],
            'jsb_link_valid': int(jsb_link_valid), 'jsb_seq': jsb_seq, 'jsb_age_ms': jsb_age_ms,
        })
    except Exception as e:
        print(f'[DB_WRITER] jpb_status_history 준비 실패: {e}')


def _handle_jsb_chunk(payload: bytes):
    """
    JSB_DATA_CHUNK(0x31) 페이로드: GROUP_SEQ(4) + CHUNK_INDEX(1) + TOTAL_CHUNKS(1) + DATA(가변)
    JSB의 1초 ASCII Raw Packet을 JPB가 여러 프레임으로 잘라 보낸 것을 GROUP_SEQ 기준으로
    재조립한다. 청크 누락/중복/group_seq 변경/total_chunks 불일치는 정상 완료로 처리하지
    않고 진단 카운터만 남긴다 — 별도 retry 프로토콜은 만들지 않는다.
    """
    if len(payload) < 6:
        _jsb_diag['chunk_error_count'] += 1
        return

    group_seq, chunk_index, total_chunks = struct.unpack_from('<IBB', payload, 0)
    data = payload[6:]

    with _jsb_chunk_lock:
        r = _jsb_reassembly
        now = time.time()

        if r['group_seq'] != group_seq:
            if r['group_seq'] is not None and len(r['chunks']) < (r['total_chunks'] or 0):
                _jsb_diag['incomplete_group_count'] += 1
            r['group_seq']    = group_seq
            r['total_chunks'] = total_chunks
            r['chunks']       = {}
            r['tainted']      = False
            r['started_at']   = now

        if total_chunks != r['total_chunks']:
            _jsb_diag['chunk_error_count'] += 1
            r['tainted'] = True

        if chunk_index in r['chunks']:
            _jsb_diag['chunk_error_count'] += 1
            r['tainted'] = True
        else:
            r['chunks'][chunk_index] = data

        _jsb_diag['last_group_seq']       = group_seq
        _jsb_diag['last_chunks_received'] = len(r['chunks'])
        _jsb_diag['last_total_chunks']    = r['total_chunks']

        complete = (
            not r['tainted']
            and r['total_chunks']
            and len(r['chunks']) == r['total_chunks']
            and all(i in r['chunks'] for i in range(r['total_chunks']))
        )

        if complete:
            raw = b''.join(r['chunks'][i] for i in range(r['total_chunks']))
            _jsb_diag['last_complete_group_seq'] = group_seq
            _jsb_diag['last_raw_packet_size']    = len(raw)
            r['group_seq']    = None
            r['total_chunks'] = None
            r['chunks']       = {}
            r['tainted']      = False
            r['started_at']   = None
            _process_jsb_raw_packet(raw, group_seq)


def jsb_chunk_timeout_checker():
    """재조립 중인 group이 JSB_CHUNK_TIMEOUT_SEC 이상 완료되지 않으면 폐기한다."""
    while True:
        time.sleep(1)
        with _jsb_chunk_lock:
            r = _jsb_reassembly
            if r['started_at'] and (time.time() - r['started_at']) > JSB_CHUNK_TIMEOUT_SEC:
                _jsb_diag['incomplete_group_count'] += 1
                r['group_seq']    = None
                r['total_chunks'] = None
                r['chunks']       = {}
                r['tainted']      = False
                r['started_at']   = None


def _int(fields: dict, key: str, default: int = 0) -> int:
    try:
        return int(fields[key])
    except (KeyError, ValueError):
        return default


def _parse_jsb_fields(fields: dict) -> dict:
    """JSB ASCII Raw Packet의 key:value 목록을 그룹별로 구조화한다."""
    ncv_count = min(_int(fields, 'NCV_COUNT'), 5)
    ncv = []
    for i in range(ncv_count):
        p = f'NCV{i}_'
        ncv.append({k: _int(fields, p + k) for k in
                    ('R1L1', 'R2L1', 'R1L2', 'R2L2', 'R1DC', 'R2DC', 'LS1', 'LS2', 'LS3', 'LS4')})

    bme_valid = _int(fields, 'BME_VALID')
    temp_x10  = _int(fields, 'TEMP_X10')
    hum_x10   = _int(fields, 'HUM_X10')
    pres_x10  = _int(fields, 'PRES_X10')

    mic_count = min(_int(fields, 'MIC_COUNT'), 2)
    mic = []
    for i in range(mic_count):
        mic.append({
            'rms':  _int(fields, f'MIC{i}_RMS'),
            'peak': _int(fields, f'MIC{i}_PEAK'),
        })

    tcs_valid = _int(fields, 'TCS_VALID')
    tcs_keys  = (['FZ', 'FY', 'FXL', 'NIR', 'GAIN', 'SAT']
                 + [f'F{i}' for i in range(1, 9)]
                 + [f'VIS{i}' for i in range(1, 7)])
    tcs = {k: _int(fields, 'TCS_' + k) for k in tcs_keys}

    return {
        'pver':   fields.get('PVER'),
        'seq':    _int(fields, 'SEQ'),
        'uptime': _int(fields, 'UPTIME'),
        'gps': {
            'valid':  bool(_int(fields, 'GPS_VALID')),
            'lat_e6': _int(fields, 'LAT_E6'),
            'lon_e6': _int(fields, 'LON_E6'),
            'utc_h':  _int(fields, 'UTC_H'),
            'utc_m':  _int(fields, 'UTC_M'),
            'utc_s':  _int(fields, 'UTC_S'),
        },
        'ncv_count': ncv_count,
        'ncv':       ncv,
        'bme': {
            'valid':    bool(bme_valid),
            'temp_x10': temp_x10,
            'hum_x10':  hum_x10,
            'pres_x10': pres_x10,
            'temp':     round(temp_x10 / 10.0, 1),
            'hum':      round(hum_x10 / 10.0, 1),
            'pres':     round(pres_x10 / 10.0, 1),
        },
        'mic_count': mic_count,
        'mic':       mic,
        'tcs':       {'valid': bool(tcs_valid), **tcs},
        'raw_fields': fields,
    }


def _process_jsb_raw_packet(raw: bytes, group_seq: int):
    try:
        text = raw.decode('ascii', errors='replace').strip()
    except Exception as e:
        _jsb_diag['chunk_error_count'] += 1
        print(f'[JSB] ASCII 디코딩 실패: {e}')
        return

    fields = {}
    for token in text.replace('\r', '').replace('\n', '').split(','):
        token = token.strip()
        if not token or ':' not in token:
            continue
        k, v = token.split(':', 1)
        fields[k.strip()] = v.strip()

    sensor = _parse_jsb_fields(fields)
    sensor['group_seq']       = group_seq
    sensor['raw_packet_size'] = len(raw)
    sensor['raw_text']        = text
    sensor['timestamp']       = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    try:
        fusion = env_fusion.process_jsb_packet(sensor)
        sensor['fusion'] = fusion
        send_env_status(SLAVE_ID, fusion['light']['level'], fusion['rain']['level'], fusion['fog']['level'])
        auto_decision_step(fusion, sensor.get('seq'))
    except Exception as e:
        print(f'[FUSION] 처리 실패: {e}')

    try:
        db_writer.enqueue('jsb_packet_history', {
            'ts': sensor['timestamp'],
            'jsb_seq': sensor.get('seq'),
            'group_seq': group_seq,
            'uptime': sensor.get('uptime'),
            'ncv_count': sensor.get('ncv_count'),
            'ncv_json': json.dumps(sensor.get('ncv', [])),
            'bme_valid': int(sensor['bme']['valid']),
            'bme_temp_x10': sensor['bme']['temp_x10'],
            'bme_hum_x10': sensor['bme']['hum_x10'],
            'bme_pres_x10': sensor['bme']['pres_x10'],
            'mic_count': sensor.get('mic_count'),
            'mic_json': json.dumps(sensor.get('mic', [])),
            'tcs_valid': int(sensor['tcs']['valid']),
            'tcs_json': json.dumps(sensor.get('tcs', {})),
            'gps_valid': int(sensor['gps']['valid']),
            'lat_e6': sensor['gps']['lat_e6'],
            'lon_e6': sensor['gps']['lon_e6'],
            'raw_packet_size': sensor.get('raw_packet_size'),
            'chunk_error_count_cum': _jsb_diag['chunk_error_count'],
            'incomplete_group_count_cum': _jsb_diag['incomplete_group_count'],
        })
        if 'fusion' in sensor:
            f = sensor['fusion']
            db_writer.enqueue('fusion_history', {
                'ts': sensor['timestamp'],
                'jsb_seq': sensor.get('seq'),
                'mode': current_control_mode,
                'light_raw': f['light']['raw'], 'light_filtered': f['light']['filtered'],
                'light_target': f['light']['target_level'], 'light_level': f['light']['level'],
                'rain_wet_distance': f['rain']['wet_distance'], 'rain_wet_present': int(f['rain']['wet_present']),
                'rain_event_mag': f['rain']['event_mag'], 'rain_event_state': f['rain']['event_state'],
                'rain_event_count_window': f['rain']['event_count_window'],
                'rain_target': f['rain']['target_level'], 'rain_level': f['rain']['level'],
                'fog_humid_ready': int(f['fog']['humid_ready']), 'fog_score': f['fog']['score'],
                'fog_target': f['fog']['target_level'], 'fog_level': f['fog']['level'],
                'rs_mean': f['ncv_features']['rs_mean'], 'rs_variation': f['ncv_features']['rs_variation'],
                'rs_impulse_ratio': f['ncv_features']['rs_impulse_ratio'],
                'rs_persistence': f['ncv_features']['rs_persistence'],
                'mic_rms': f['mic_feature']['rms'], 'mic_peak': f['mic_feature']['peak'],
                'mic_state': f['mic_feature']['state'],
            })
    except Exception as e:
        print(f'[DB_WRITER] jsb/fusion history 준비 실패: {e}')

    global latest_jsb_sensor
    with _state_lock:
        latest_jsb_sensor = sensor
    socketio.emit('jsb_sensor_update', sensor)


# ──────────────────────────────────────────
# JPB 공통 송신 함수 — MANUAL Route와 AUTO Decision이 동일 함수를 사용한다.
# Wire Protocol/기존 MANUAL API 동작은 변경하지 않는다(기존 payload 그대로).
# ──────────────────────────────────────────
def _log_control_event(event_type: str, lanes, value, reason: str = ''):
    try:
        db_writer.enqueue('control_event_log', {
            'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'mode': current_control_mode,
            'event_type': event_type,
            'lanes': lanes,
            'value': value,
            'reason': reason,
        })
    except Exception as e:
        print(f'[DB_WRITER] control_event_log 준비 실패: {e}')


def send_mode(slave_id: int, mode: int):
    uart_send(CMD_MODE, bytes([slave_id, mode]))
    _log_control_event('MODE_CHANGE', None, mode)


def send_lane_onoff(slave_id: int, lanes: int, on: int):
    uart_send(CMD_ONOFF, bytes([slave_id, lanes, on]))
    _log_control_event('ONOFF', lanes, on)


def send_lane_brightness(slave_id: int, lane: int, value: int):
    uart_send(CMD_BRIGHTNESS, bytes([slave_id, lane, value]))
    _log_control_event('BRIGHTNESS', lane, value)


def send_env_status(slave_id: int, light_level: int, rain_level: int, fog_level: int):
    """CMD_ENV_STATUS(0x12) — JPB 상태 표시/진단 전용. 실제 Lane 제어에는 쓰이지 않는다."""
    uart_send(CMD_ENV_STATUS, bytes([slave_id, light_level, rain_level, fog_level]))


# ──────────────────────────────────────────
# PoC AUTO Decision — env_level = max(L,R,F), 0=OFF, 1~4=ON+Brightness
# MANUAL 모드에서는 계산/모니터링만 하고 절대 명령을 발행하지 않는다(안전조건).
# Target이 이전과 같으면 아무 것도 보내지 않는다(중복명령 금지).
# ──────────────────────────────────────────
current_control_mode = 'MANUAL'  # 서버가 기억하는 현재 모드. 기동 시 안전하게 MANUAL로 시작
_auto_lock = threading.Lock()
_auto_state = {
    'env_level':     0,
    'target_onoff':  0,
    'target_bright': 0,
    'reason':        '',
    'sent_onoff':    None,  # AUTO가 실제로 마지막에 보낸 ON/OFF (None=아직 없음)
    'sent_bright':   None,  # AUTO가 실제로 마지막에 보낸 Brightness
}


def _compute_actual_state(jpb: dict):
    """JPB 0x30 최신 상태에서 3-Lane 종합 ON/OFF·Brightness를 뽑는다.
    3개 Lane 상태가 혼재(과도상태 등)하면 -1로 표시한다. /api/monitor와 DB 기록이 공유."""
    if jpb is None:
        return None, None
    lanes_active = [jpb['lane1']['active'], jpb['lane2']['active'], jpb['lane3']['active']]
    lanes_bright = [jpb['lane1']['bright_level'], jpb['lane2']['bright_level'], jpb['lane3']['bright_level']]
    if all(lanes_active):
        actual_onoff = 1
    elif not any(lanes_active):
        actual_onoff = 0
    else:
        actual_onoff = -1
    actual_bright = lanes_bright[0] if len(set(lanes_bright)) == 1 else -1
    return actual_onoff, actual_bright


def auto_decision_step(fusion: dict, jsb_seq=None):
    light = fusion['light']['level']
    rain  = fusion['rain']['level']
    fog   = fusion['fog']['level']
    env_level = max(light, rain, fog)

    target_onoff  = 1 if env_level >= 1 else 0
    target_bright = env_level if env_level >= 1 else 0
    reason = f'env_level={env_level} (L={light} R={rain} F={fog})'

    with _auto_lock:
        _auto_state['env_level']     = env_level
        _auto_state['target_onoff']  = target_onoff
        _auto_state['target_bright'] = target_bright
        _auto_state['reason']        = reason

        if current_control_mode == 'AUTO':
            prev_onoff  = _auto_state['sent_onoff']
            prev_bright = _auto_state['sent_bright']

            if target_onoff != prev_onoff:
                if target_onoff == 1:
                    # OFF->ON: Lane1~3 Brightness 먼저, 그 다음 CMD_ONOFF(lanes=0x07, ON)
                    for lane in (1, 2, 3):
                        send_lane_brightness(SLAVE_ID, lane, target_bright)
                    send_lane_onoff(SLAVE_ID, 0x07, 1)
                    _auto_state['sent_bright'] = target_bright
                else:
                    # ON->OFF: CMD_ONOFF(lanes=0x07, OFF)만 전송
                    send_lane_onoff(SLAVE_ID, 0x07, 0)
                _auto_state['sent_onoff'] = target_onoff
            elif target_onoff == 1 and target_bright != prev_bright:
                # 이미 ON인 상태에서 밝기만 바뀐 경우 -> ON/OFF는 재전송하지 않는다
                for lane in (1, 2, 3):
                    send_lane_brightness(SLAVE_ID, lane, target_bright)
                _auto_state['sent_bright'] = target_bright
            # else: target 변화 없음 -> 아무 것도 보내지 않는다

    try:
        with _state_lock:
            jpb_snapshot = dict(latest_jpb_status) if latest_jpb_status else None
        actual_onoff, actual_bright = _compute_actual_state(jpb_snapshot)
        control_match = None
        if actual_onoff is not None:
            control_match = int((actual_onoff == target_onoff) and
                                 (target_onoff == 0 or actual_bright == target_bright))
        db_writer.enqueue('auto_control_history', {
            'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'jsb_seq': jsb_seq,
            'mode': current_control_mode,
            'env_level': env_level,
            'target_onoff': target_onoff,
            'target_bright': target_bright,
            'actual_onoff': actual_onoff,
            'actual_bright': actual_bright,
            'control_match': control_match,
        })
    except Exception as e:
        print(f'[DB_WRITER] auto_control_history 준비 실패: {e}')


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
    send_lane_onoff(slave_id, lanes, on)
    return jsonify({'ok': True})


@app.route('/api/control/brightness', methods=['POST'])
def api_brightness():
    d        = request.json
    slave_id = int(d.get('slave_id', 1))
    lane     = int(d.get('lane', 1))
    value    = int(d.get('value', 2))
    send_lane_brightness(slave_id, lane, value)
    return jsonify({'ok': True})


@app.route('/api/control/mode', methods=['POST'])
def api_mode():
    global current_control_mode
    d        = request.json
    slave_id = int(d.get('slave_id', 1))
    mode     = int(d.get('mode', 0))
    send_mode(slave_id, mode)

    new_mode = 'AUTO' if mode == 0 else 'MANUAL'
    with _auto_lock:
        if new_mode == 'AUTO' and current_control_mode != 'AUTO':
            # AUTO 진입 시 이전 송신 이력을 리셋 -> MANUAL 동안 실제 상태가 바뀌었을 수
            # 있으므로 재진입 직후 현재 목표를 반드시 한 번 명시적으로 재전송한다.
            _auto_state['sent_onoff']  = None
            _auto_state['sent_bright'] = None
        current_control_mode = new_mode
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
# REST API — 외부테스트 Test Marker (6시간 Report에서 센서 변화와 함께 표시)
# ──────────────────────────────────────────
@app.route('/api/test_marker', methods=['POST'])
def api_test_marker():
    d     = request.json or {}
    label = str(d.get('label', '')).strip()
    memo  = str(d.get('memo', '')).strip()
    if not label:
        return jsonify({'ok': False, 'error': 'label required'}), 400
    db_writer.enqueue('test_marker', {
        'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'label': label,
        'memo': memo,
    })
    return jsonify({'ok': True})


# ──────────────────────────────────────────
# REST API — PoC Monitor (JPB STATUS / JSB SENSOR)
# ──────────────────────────────────────────
@app.route('/api/monitor')
def api_monitor():
    with _state_lock:
        jpb = dict(latest_jpb_status) if latest_jpb_status else None
        jsb = dict(latest_jsb_sensor) if latest_jsb_sensor else None
    with _jsb_chunk_lock:
        diag = dict(_jsb_diag)

    with _auto_lock:
        auto = dict(_auto_state)
        auto['mode'] = current_control_mode

    actual_onoff, actual_bright = _compute_actual_state(jpb)
    control_match = None
    if actual_onoff is not None:
        control_match = (actual_onoff == auto['target_onoff']) and (
            auto['target_onoff'] == 0 or actual_bright == auto['target_bright']
        )

    auto['actual_onoff']   = actual_onoff
    auto['actual_bright']  = actual_bright
    auto['control_match']  = control_match
    auto['db_queue_drop_count'] = db_writer.get_drop_count()

    return jsonify({'jpb': jpb, 'jsb': jsb, 'jsb_diag': diag, 'auto': auto})


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
    db_writer.start(DB_PATH)
    init_mqtt()
    threading.Thread(target=uart_reader,     daemon=True).start()
    threading.Thread(target=status_poller,   daemon=True).start()
    threading.Thread(target=schedule_checker, daemon=True).start()
    threading.Thread(target=jsb_chunk_timeout_checker, daemon=True).start()
    resend_all_calibration()
    print('[SERVER] http://0.0.0.0:5000 시작')
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)

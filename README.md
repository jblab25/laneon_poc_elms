# ELMS — Edge Light Management System

Pi Zero 기반 COB LED 가로등 엣지 제어 시스템

---

## 시스템 구성

```
[센서보드] ──UART2── [STM32 슬레이브 ×3]
                           ↕ LoRa (923.1MHz)
                     [STM32 마스터]
                           ↕ USB-UART (/dev/ttyUSB0)
                     [Pi Zero (Flask 서버)]  ──MQTT──▶ [Global 서버]
                           ↕ WebSocket / HTTP
                     [브라우저 대시보드]
```

---

## 프로젝트 구조

```
├── index.html          # 대시보드 UI
├── elms01/
│   └── src/
│       └── script.js   # 대시보드 JavaScript
└── backend/
    ├── app.py          # Flask 서버 (메인)
    └── requirements.txt
```

---

## 기능

- **실시간 모니터링** — 슬레이브 1/2/3 각 Lane 전압·전류·ON/OFF·밝기 표시
- **수동 제어** — Lane별 ON/OFF, 밝기 1~4단계, AUTO/MANUAL 모드 전환
- **자동 스케줄** — 슬레이브별 점등/소등 시간 설정 (매분 체크)
- **DB 저장** — SQLite 이력 로그, CSV 내보내기
- **MQTT 발행** — Global 서버로 실시간 상태 전송

---

## UART 프로토콜 (Pi ↔ STM32 마스터)

프레임 형식: `[0xAA][CMD][LEN][PAYLOAD...][CRC_XOR]`

| CMD  | 이름           | 방향         | 페이로드 |
|------|----------------|--------------|----------|
| 0x01 | STATUS_REQ     | Pi → 마스터  | slave_id |
| 0x02 | STATUS_RESP    | 마스터 → Pi  | 19 bytes |

**STATUS_REQ 폴링**: `status_poller()`가 슬레이브 1→2→3 순서로 5초 간격 `STATUS_REQ`를 순환 전송한다 (슬레이브당 5초, 전체 한 바퀴 15초).
| 0x10 | ONOFF          | Pi → 마스터  | slave_id, lane_mask, on |
| 0x11 | BRIGHTNESS     | Pi → 마스터  | slave_id, lane, value |
| 0x13 | MODE           | Pi → 마스터  | slave_id, mode |
| 0x06 | ACK            | 양방향       | result (마스터→Pi) / `[1]` (Pi→마스터, BOOT_NOTIFY 확인용) |
| 0x14 | CAL_SET        | Pi → 마스터  | slave_id, lane, level, step |
| 0x15 | CAL_GET        | Pi → 마스터  | slave_id, lane, level |
| 0x16 | CAL_RESP       | 마스터 → Pi  | slave_id, lane, level, step |
| 0x17 | BOOT_NOTIFY    | 마스터 → Pi  | slave_id |
| 0x20 | GPS_REQ        | Pi → 마스터  | (없음) |
| 0x21 | GPS_RESP       | 마스터 → Pi  | 9 bytes |

**STATUS_RESP 페이로드 (19 bytes)**

```
[0]     slave_id
[1..6]  v[0..2]  uint16 LE (mV)
[7..12] i[0..2]  uint16 LE (mA)
[13]    rain_level (0~4)
[14]    fog_level (0~4)
[15]    lane_state bitmask (bit0=CH1, bit1=CH2, bit2=CH3)
[16]    brightness packed (하위nibble=lane1, 상위nibble=lane2)
[17]    lane3 brightness (0~4)
[18]    light(조도)_level (0~4)
```

**GPS_REQ** — 파워보드(마스터)에 위치값을 요청. 페이로드 없음(LEN=0). 대시보드의 "위치 요청" 버튼을 눌렀을 때만 1회성으로 전송되며, 주기적으로 자동 전송되지 않음.

**GPS_RESP 페이로드 (9 bytes)**

```
[0]    fix_valid   (0=미확보, 1=위치 확보됨)
[1..4] latitude    int32 LE, 실제값 = raw / 1,000,000  (예: 37123456 → 위도 37.123456)
[5..8] longitude   int32 LE, 실제값 = raw / 1,000,000
```

- 마스터는 GPS_REQ를 받으면 내부적으로 GPS 모듈에서 fix를 획득할 때까지 기다렸다가(콜드 픽스 시 최대 수십 초~수 분 소요 가능) `GPS_RESP`를 응답하면 됩니다. Pi 쪽은 응답이 올 때까지 비동기로 대기하며, 별도의 타임아웃 재전송 로직은 없습니다.
- fix를 끝내 못 잡으면 `fix_valid=0`으로 응답하고 `latitude`/`longitude`는 0으로 채워 보내면 됩니다.
- slave_id가 없는 이유: GPS는 슬레이브가 아니라 마스터(파워보드) 자체에 달린 센서이므로 대상 식별이 필요 없습니다.

**Lane 초기 전압 캘리브레이션 (CAL_SET / CAL_GET / CAL_RESP)**

- Lane별 밝기 레벨(1~4)마다 초기 전압 보정 스텝을 설정. MCU는 이 값을 **RAM에만** 들고 있어서 전원이 끊기면 기본값 `{98,95,92,88}`(레벨 1~4)로 리셋됨.
- `CAL_RESP`(0x16)에는 `slave_id`가 없음 — Pi는 직전에 보낸 `CAL_GET` 요청의 slave_id를 기억해뒀다가 응답과 매칭한다(요청은 한 번에 하나씩만 진행).

**MCU 재부팅 감지 (BOOT_NOTIFY)**

- MCU가 부팅되면(캘리브레이션 값이 리셋됨) `BOOT_NOTIFY`(0x17, payload: `slave_id`)를 **2초 간격으로 재전송**하며 Pi의 ACK을 기다림 (최대 60초, 이후 자체 포기).
- Pi는 `BOOT_NOTIFY` 수신 즉시 `ACK`(0x06, payload `[1]`)을 마스터로 회신하고, 저장해둔 해당 slave_id의 캘리브레이션 값 전체를 `CAL_SET`으로 순차 재전송한다.
- 추가 안전장치로, Pi 서비스 자체가 시작될 때도 저장된 모든 슬레이브의 캘리브레이션 값을 한 번 더 전체 재전송한다(Pi가 다운돼 있는 동안 MCU가 재시도를 포기한 경우 대비).

---

## MQTT 발행 포맷

- **토픽**: `sensors/pi_zero_01/slave{N}`
- **브로커**: `192.168.1.107:1883` (설정 변경: `app.py` 내 `MQTT_BROKER`)

```json
{
  "server_id": "pi_zero_01_slave1",
  "slave_id": 1,
  "status": {
    "lane1": { "voltage": 20.1, "current": 0.45, "on": true, "brightness": 2 },
    "lane2": { "voltage": 20.0, "current": 0.44, "on": true, "brightness": 2 },
    "lane3": { "voltage": 20.2, "current": 0.46, "on": true, "brightness": 2 }
  },
  "sensor": { "cds": 2, "rain": 1, "fog": 0, "vis": 0 },
  "timestamp": "2026-05-19 13:00:00"
}
```

---

## 설치 및 실행 (Pi Zero)

```bash
# 1. 클론
git clone https://github.com/eybae/ELMS.git
cd ELMS/backend

# 2. 가상환경 및 패키지 설치
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. 실행
python app.py
```

### 자동 시작 (systemd)

`deploy/elms.service`에 정의된 유닛 파일을 설치합니다 (경로는 실제 배포 위치에 맞게 수정 후 사용):

```bash
sudo cp deploy/elms.service /etc/systemd/system/elms.service
sudo systemctl daemon-reload
sudo systemctl enable elms
sudo systemctl start elms
```

### 서비스 관리

```bash
sudo systemctl start elms    # 시작
sudo systemctl stop elms     # 정지
sudo systemctl restart elms  # 재시작
journalctl -u elms -f        # 로그 확인
```

---

## REST API

| Method | Endpoint | 설명 |
|--------|----------|------|
| POST | `/api/control/onoff` | Lane ON/OFF |
| POST | `/api/control/brightness` | 밝기 설정 |
| POST | `/api/control/mode` | AUTO/MANUAL 전환 |
| GET  | `/api/history` | 이력 조회 |
| GET  | `/api/history/csv` | CSV 다운로드 |
| GET  | `/api/schedule` | 스케줄 조회 |
| POST | `/api/schedule` | 스케줄 설정 |

---

## 관련 프로젝트

- **STM32 마스터**: `ELMS_POWER_Master`
- **STM32 슬레이브**: `ELMS_POWER_Slave`
- **Global 서버**: `AEMS`

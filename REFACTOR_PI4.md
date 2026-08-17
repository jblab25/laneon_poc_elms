# ELMS: Pi Zero → Raspberry Pi 4 전환 리팩토링 계획

## 1. 배경

- 기존: **Pi Zero**, Raspberry Pi OS Lite(헤드리스), 개발/운영 모두 **SSH 접속 + CLI 편집(vim/nano)** 로 진행.
- 변경: **Pi 4**로 하드웨어 이전. Pi Zero는 성능상 감당이 어려워짐.
- Pi 4는 **Raspberry Pi OS (Desktop)** 를 사용하며, 원격에서 **GUI 환경으로 접속해 코드를 수정·실행**할 수 있는 개발 환경을 구성하는 것이 이번 전환의 핵심 목표.
  - 즉 "GUI 사용"은 ELMS 대시보드 자체의 UI 변경이 아니라, **Pi 4 본체에 원격으로 붙어 GUI 기반으로 개발 작업을 하기 위한 환경 구성**을 의미함.
- 부가 목표로 Pi 4의 여유 자원(CPU/RAM)을 활용한 **성능·리소스 최적화**를 함께 반영.

> 참고: ELMS 웹 대시보드(`index.html` + Flask/Socket.IO)는 기존과 동일하게 브라우저로 접속하는 방식을 유지한다. 이번 전환의 GUI 요구사항은 대시보드가 아니라 **개발 환경**에 대한 것이다.

---

## 2. 환경 비교

| 항목 | Pi Zero (기존) | Pi 4 (신규) |
|---|---|---|
| OS | Raspberry Pi OS Lite | Raspberry Pi OS (Desktop) |
| 개발 방식 | SSH + CLI 편집기 | SSH(CLI) + 원격 GUI(VNC/RDP) 병행 |
| 코드 편집 | vim/nano, 로컬 → scp | VS Code Remote 또는 원격 데스크톱에서 직접 |
| UART 포트 | USB-UART 어댑터, `/dev/ttyUSB0` | 동일 어댑터 사용 가능하나 포트명 재확인 필요 (`/dev/ttyUSB0` 또는 GPIO UART `/dev/ttyAMA0`/`/dev/serial0`) |
| CPU/RAM | 1 core, 512MB | 4 core, 4~8GB |
| 디스플레이 출력 | 없음 | HDMI 출력 있음 (필요 시 로컬 모니터도 사용 가능) |
| systemd 서비스 경로 | `/home/jb/app/backend` | 경로 유지 여부 확인 필요 (사용자 홈 동일하면 유지 가능) |

---

## 3. 원격 GUI 개발 환경 구성

### 3.1 검토한 방식과 결정

| 방식 | 설명 | 결과 |
|---|---|---|
| Raspberry Pi Connect | 포트포워딩 없이 외부 접속 가능한 공식 릴레이 서비스 | **보류** — 기기 코드 인증(device-code) 플로우가 몇 분 내 만료되고, SSH 원격 실행 시 `-t` 미지정으로 세션이 꼬이는 등 로그인 완료에 반복 실패하여 포기 |
| **Tailscale (WireGuard 기반 메시 VPN)** (채택) | 각 기기에 Tailscale 설치 후 같은 계정으로 로그인하면 사설 가상망(tailnet) 구성, 포트포워딩/고정IP 불필요 | **채택** — SSH·VNC·대시보드(5000) 모두 tailnet 내부 IP로 통일해서 접근 |
| RealVNC(raspi-config)/xrdp 단독 | 로컬망 전용, 외부 접속 시 포트포워딩 필요 | Tailscale과 조합해서 사용 (VNC 자체는 유지, 노출 경로만 Tailscale로 통일) |

### 3.2 최종 구성

1. **Tailscale**을 Pi 4와 개발용 외부 기기(노트북 등) 모두에 설치하고 동일 계정으로 로그인 → 사설 tailnet IP(예: `100.x.x.x`) 확보.
2. **SSH**: `openssh-server` 활성화(`sudo systemctl enable --now ssh`), 외부에서는 tailnet IP로 접속.
3. **원격 GUI**: 기존에 설치돼 있던 `wayvnc`/RealVNC를 `raspi-config`로 활성화. 외부 노출은 공유기 포트포워딩이 아니라 Tailscale 인터페이스를 통해서만 이루어지므로 별도 방화벽 설정 부담이 적음.
4. **대시보드**: Flask 서버(5000번 포트)도 동일하게 tailnet IP로 접근 가능 (`http://<tailnet-ip>:5000`).
5. Raspberry Pi Connect는 `rpi-connect off` 로 비활성화.

### 3.3 확인/작업 목록

- [x] Raspberry Pi Connect 비활성화
- [x] Pi 4에 Tailscale 설치 및 로그인 (tailnet IP `100.80.5.66` 확보)
- [ ] 개발용 외부 기기에도 Tailscale 설치 및 동일 계정 로그인 (사용자 진행 필요)
- [x] `raspi-config`로 VNC(wayvnc) 활성화 및 부팅 시 자동 시작 확인
- [x] Tailscale IP로 VNC/대시보드 접속 테스트 (SSH는 외부 기기 Tailscale 연결 후 확인 필요)
- [ ] 원격 접속 계정 비밀번호 정책 점검

---

## 4. 하드웨어·환경 설정 변경 항목

- [x] UART 포트 경로 재확인: USB-UART 어댑터 유지, `/dev/ttyUSB0` 그대로 사용 (실제 STM32 마스터 연결 전까지는 시뮬레이션 모드로 동작 확인함).
- [x] `backend/app.py`의 `SERIAL_PORT`, `DB_PATH` 등 경로 상수 재확인 — 변경 불필요.
- [x] systemd 서비스 파일(`deploy/elms.service`)을 Pi 4 실제 경로(`/home/jb/Dev/ELMS/backend`)로 작성 후 `/etc/systemd/system/elms.service`로 등록 완료 (`enabled`, `active`).
- [ ] Desktop 환경 부팅 시 리소스 사용량(데스크톱 세션 자체가 백그라운드에서 CPU/RAM 사용) 확인 — ELMS 백엔드 서비스와 충돌 없는지 점검.
- [ ] 방화벽(ufw 등) 사용 시 Flask 포트(5000), VNC 포트(5900) 인바운드 규칙 확인 (현재는 Tailscale 경유 접속이라 우선순위 낮음).

---

## 5. 성능·리소스 최적화 반영 항목

Pi Zero 대비 Pi 4는 CPU 4코어, RAM 4~8GB로 여유가 크므로 아래 항목을 함께 반영한다.

- [ ] **WSGI 서버 교체**: 현재 `socketio.run(..., allow_unsafe_werkzeug=True)`로 개발 서버를 그대로 운영 중 — `eventlet`/`gevent` 전환은 `pyserial`의 블로킹 read와 충돌할 수 있어 이번 범위에서는 보류. LAN 수준의 동시접속 규모에서는 threading 모드로 충분하다고 판단.
- [x] **SQLite 접근 방식 개선**: `get_db()` 헬퍼로 커넥션 생성 로직을 통합하고 `busy_timeout=5000` 적용, `PRAGMA journal_mode=WAL` 적용 완료 (동시 쓰기 시 "database is locked" 오류 감소).
- [ ] **UART 수신 루프 최적화**: 현재 바이트 단위로 `bytearray` 슬라이싱 반복 — Pi 4에서는 성능 이슈가 크지 않지만, 프레임 단위 처리로 정리해 가독성/안정성 개선 여지 있음.
- [ ] **로그/모니터링**: 여유 자원을 활용해 `journalctl` 외에 리소스 사용량(CPU/온도/메모리) 모니터링 추가 검토 (예: 대시보드에 시스템 상태 카드 추가).
- [ ] **동시성**: Pi 4의 멀티코어를 활용해 UART 수신, 스케줄 체커, MQTT 발행 등 스레드 분리 구조를 유지하되, 필요 시 프로세스 단위 분리도 검토.

> 이전 코드 리뷰에서 발견된 스케줄 체커 정각 매칭 이슈, MQTT/시리얼 재연결 부재 등의 버그 수정은 이번 전환 문서의 범위에서 제외하고 별도 작업으로 다룬다.

---

## 6. 마이그레이션 절차 (체크리스트)

1. [ ] Pi 4에 Raspberry Pi OS (Desktop) 설치 및 초기 설정 (locale, timezone, 비밀번호 변경)
2. [ ] VNC 활성화 및 원격 GUI 접속 확인
3. [ ] 저장소 클론 (`git clone` 또는 기존 파일 이관)
4. [ ] Python venv 생성 및 `pip install -r requirements.txt`
5. [ ] UART 어댑터 연결 후 포트명 확인 (`ls /dev/ttyUSB*` 또는 `/dev/serial0`)
6. [ ] `SERIAL_PORT`, `DB_PATH` 등 설정값 검증
7. [ ] systemd 서비스 파일 경로 갱신 후 `elms.service` 재등록
8. [ ] 대시보드 브라우저 접속 확인 (`http://<pi4-ip>:5000`)
9. [ ] 실제 STM32 마스터 연결 후 UART 송수신 동작 확인
10. [ ] MQTT 발행 확인 (Global 서버 연동 시)
11. [ ] 부하/장시간 구동 테스트 (24시간 이상 안정성 확인)

---

## 7. 리스크 및 롤백

- Pi 4 데스크톱 환경이 백그라운드 자원을 소모해 실시간 UART 처리에 영향을 줄 가능성 → 문제 발생 시 Desktop 세션을 최소화하거나 필요 시에만 VNC 세션을 띄우는 방식으로 운용.
- UART 포트 경로 변경 시 서비스 기동 실패 가능 → 변경 전 `SERIAL_PORT` 값을 systemd 환경변수로 분리해 두면 롤백이 쉬움.
- 기존 Pi Zero는 백업 겸 예비 장비로 당분간 유지 권장 (전환 실패 시 즉시 복귀 가능하도록).

---

## 8. 완료 기준 (Definition of Done)

- [ ] Pi 4에서 VNC로 원격 GUI 접속이 가능하고, 접속 상태에서 VS Code로 코드 수정 및 실행이 가능함
- [ ] ELMS 백엔드가 Pi 4에서 systemd로 정상 기동/재시작됨
- [ ] UART, MQTT, 대시보드(WebSocket) 기능이 Pi Zero 때와 동일하게 동작함이 확인됨
- [ ] 위 "성능·리소스 최적화" 항목 중 합의된 범위가 반영됨

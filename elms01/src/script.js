/* ──────────────────────────────────────────
   ELMS Dashboard Script
   ────────────────────────────────────────── */
const socket = io();

let selectedSlave = 1;          // 대시보드에 표시할 슬레이브
const latestStatus = {};         // slave_id → 최신 상태 캐시

/* ──────────────────────────────────────────
   Socket.IO 이벤트
   ────────────────────────────────────────── */
socket.on('connect', () => {
    const badge = document.getElementById('connected_badge');
    badge.innerText = 'Connected';
    badge.style.color = '#0f0';
});

socket.on('disconnect', () => {
    const badge = document.getElementById('connected_badge');
    badge.innerText = 'Disconnected';
    badge.style.color = '#f44';
});

socket.on('gps_update', (data) => {
    _renderGps(data);
    document.getElementById('gps_result').innerText = '✔ 위치 수신 완료';
});

socket.on('cal_update', (data) => {
    if (data.slave_id !== _getCalSlave()) return;
    const cell = document.getElementById(`cal_cur_${data.lane}_${data.level}`);
    if (cell) cell.innerText = data.step;
    document.getElementById('cal_result').innerText =
        `✔ Lane${data.lane} Level${data.level} 조회 완료 — ${data.step}`;
});

socket.on('status_update', (data) => {
    latestStatus[data.slave_id] = data;

    document.getElementById('last_update_text').innerText =
        `Slave${data.slave_id}  ${data.timestamp}`;
    document.getElementById('status_raw').innerText =
        JSON.stringify(latestStatus, null, 2);

    if (data.slave_id === selectedSlave) {
        _updateCards(data);
    }
});

/* ──────────────────────────────────────────
   대시보드 카드 업데이트
   ────────────────────────────────────────── */
function _updateCards(d) {
    for (let i = 1; i <= 3; i++) {
        const v   = d.voltage[i - 1].toFixed(2);
        const cur = d.current[i - 1].toFixed(3);
        const on  = (d.lane_state >> (i - 1)) & 1 ? 'ON' : 'OFF';

        const bright = d.brightness ? (d.brightness[i - 1] || 0) : 0;
        document.getElementById(`lane${i}_voltage`).innerText    = v;
        document.getElementById(`lane${i}_current`).innerText    = cur;
        document.getElementById(`lane${i}_on`).innerText         = on;
        document.getElementById(`lane${i}_brightness`).innerText = bright > 0 ? `Lv.${bright}` : '-';
    }
    document.getElementById('lux_level').innerText  = d.cds;
    document.getElementById('rain_level').innerText = d.rain;
    document.getElementById('fog_level').innerText  = d.fog;
}

/* ──────────────────────────────────────────
   탭 전환
   ────────────────────────────────────────── */
function showTab(tab, btn) {
    ['dashboard', 'control', 'db', 'schedule', 'settings'].forEach(t => {
        document.getElementById(t + '_section').classList.add('hidden');
    });
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));

    document.getElementById(tab + '_section').classList.remove('hidden');
    btn.classList.add('active');
}

/* ──────────────────────────────────────────
   슬레이브 선택 (대시보드 보기)
   ────────────────────────────────────────── */
function selectSlave(id, btn) {
    selectedSlave = id;
    document.querySelectorAll('.slave-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');

    if (latestStatus[id]) {
        _updateCards(latestStatus[id]);
    } else {
        for (let i = 1; i <= 3; i++) {
            document.getElementById(`lane${i}_voltage`).innerText   = '-';
            document.getElementById(`lane${i}_current`).innerText   = '-';
            document.getElementById(`lane${i}_on`).innerText        = '-';
            document.getElementById(`lane${i}_brightness`).innerText = '-';
        }
        document.getElementById('lux_level').innerText  = '-';
        document.getElementById('rain_level').innerText = '-';
        document.getElementById('fog_level').innerText  = '-';
    }
}

/* ──────────────────────────────────────────
   수동 제어 명령
   ────────────────────────────────────────── */
function _getCtrlSlave() {
    return parseInt(document.getElementById('ctrl_slave_select').value);
}

function _setResult(laneMask, msg) {
    const idMap = { 1: 'lane1_result', 2: 'lane2_result', 4: 'lane3_result' };
    const elem  = document.getElementById(idMap[laneMask] || 'lane1_result');
    if (elem) elem.innerText = msg;
}

function sendOnOff(laneMask, onValue) {
    const slaveId = _getCtrlSlave();
    fetch('/api/control/onoff', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ slave_id: slaveId, lanes: laneMask, on: onValue }),
    })
    .then(r => r.json())
    .then(() => _setResult(laneMask, onValue ? '✔ ON 전송' : '✔ OFF 전송'))
    .catch(() => _setResult(laneMask, '✘ 오류'));
}

function setBrightness(lane, value) {
    const slaveId = _getCtrlSlave();
    const laneMask = 1 << (lane - 1);   // lane 1→1, 2→2, 3→4
    fetch('/api/control/brightness', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ slave_id: slaveId, lane: lane, value: value }),
    })
    .then(r => r.json())
    .then(() => _setResult(laneMask, `✔ 밝기 ${value} 적용`))
    .catch(() => _setResult(laneMask, '✘ 오류'));
}

function setMode(mode) {
    const slaveId = _getCtrlSlave();
    fetch('/api/control/mode', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ slave_id: slaveId, mode: mode }),
    })
    .then(r => r.json())
    .then(() => {
        document.getElementById('mode_result').innerText =
            `✔ Slave${slaveId} → ${mode === 0 ? 'AUTO' : 'MANUAL'} 전환`;
    })
    .catch(() => {
        document.getElementById('mode_result').innerText = '✘ 오류';
    });
}

/* ──────────────────────────────────────────
   GPS 위치 (파워보드) — 버튼 요청 시에만 조회
   ────────────────────────────────────────── */
function _renderGps(gps) {
    document.getElementById('gps_fix').innerText =
        gps && gps.fix_valid ? 'Fix 확보' : 'Fix 없음';
    document.getElementById('gps_lat').innerText =
        gps && gps.latitude != null ? gps.latitude : '-';
    document.getElementById('gps_lon').innerText =
        gps && gps.longitude != null ? gps.longitude : '-';
    document.getElementById('gps_updated').innerText =
        (gps && gps.updated_ts) || '-';
}

function loadGps() {
    fetch('/api/gps')
    .then(r => r.json())
    .then(gps => _renderGps(gps));
}

function requestGps() {
    document.getElementById('gps_result').innerText = '요청 전송됨 — 응답 대기중...';
    fetch('/api/gps/request', { method: 'POST' })
    .catch(() => {
        document.getElementById('gps_result').innerText = '✘ 요청 실패';
    });
}

/* ──────────────────────────────────────────
   튜닝 (Lane 초기 전압 캘리브레이션)
   ────────────────────────────────────────── */
function _getCalSlave() {
    return parseInt(document.getElementById('cal_slave_select').value);
}

function loadCal() {
    const slaveId = _getCalSlave();
    fetch(`/api/cal?slave_id=${slaveId}`)
    .then(r => r.json())
    .then(rows => {
        const tbody = document.getElementById('cal_tbody');
        tbody.innerHTML = rows.map(r => `
            <tr>
                <td>Lane ${r.lane}</td>
                <td>${r.level}</td>
                <td id="cal_cur_${r.lane}_${r.level}">${r.step}</td>
                <td>
                    <input type="number" id="cal_new_${r.lane}_${r.level}"
                           value="${r.step}" style="width: 70px">
                </td>
                <td>
                    <button class="ctrl-btn" onclick="applyCal(${r.lane}, ${r.level})">적용</button>
                </td>
                <td>
                    <button class="ctrl-btn" onclick="requestCal(${r.lane}, ${r.level})">조회</button>
                </td>
            </tr>
        `).join('');
    })
    .catch(() => {
        document.getElementById('cal_tbody').innerHTML =
            '<tr><td colspan="6" style="color:#f44">불러오기 실패</td></tr>';
    });
}

function applyCal(lane, level) {
    const slaveId = _getCalSlave();
    const step = parseInt(document.getElementById(`cal_new_${lane}_${level}`).value);
    fetch('/api/control/cal', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ slave_id: slaveId, lane: lane, level: level, step: step }),
    })
    .then(r => r.json())
    .then(() => {
        document.getElementById(`cal_cur_${lane}_${level}`).innerText = step;
        document.getElementById('cal_result').innerText =
            `✔ Lane${lane} Level${level} → ${step} 적용`;
    })
    .catch(() => {
        document.getElementById('cal_result').innerText = '✘ 적용 실패';
    });
}

function requestCal(lane, level) {
    const slaveId = _getCalSlave();
    document.getElementById('cal_result').innerText =
        `Lane${lane} Level${level} 조회 요청 전송됨 — 응답 대기중...`;
    fetch('/api/control/cal/request', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ slave_id: slaveId, lane: lane, level: level }),
    })
    .catch(() => {
        document.getElementById('cal_result').innerText = '✘ 조회 요청 실패';
    });
}

/* ──────────────────────────────────────────
   DB 조회 / CSV 다운로드
   ────────────────────────────────────────── */
function loadHistory() {
    const from     = document.getElementById('db_from').value;
    const to       = document.getElementById('db_to').value;
    const slaveId  = document.getElementById('db_slave_select').value;
    const slaveQ   = slaveId ? `&slave_id=${slaveId}` : '';

    fetch(`/api/history?from=${from}&to=${to}${slaveQ}`)
    .then(r => r.json())
    .then(rows => {
        document.getElementById('db_result').innerText =
            rows.length ? JSON.stringify(rows, null, 2) : '조회 결과 없음';
    });
}

function downloadCSV() {
    const from    = document.getElementById('db_from').value;
    const to      = document.getElementById('db_to').value;
    const slaveId = document.getElementById('db_slave_select').value;
    const slaveQ  = slaveId ? `&slave_id=${slaveId}` : '';
    window.location.href = `/api/history/csv?from=${from}&to=${to}${slaveQ}`;
}

/* ──────────────────────────────────────────
   스케줄 설정
   ────────────────────────────────────────── */
function loadSchedule() {
    fetch('/api/schedule')
    .then(r => r.json())
    .then(rows => {
        const tbody = document.getElementById('schedule_tbody');
        tbody.innerHTML = rows.map(r => `
            <tr>
                <td>Slave ${r.slave_id}</td>
                <td><input type="time" id="on_time_${r.slave_id}"  value="${r.on_time}"></td>
                <td><input type="time" id="off_time_${r.slave_id}" value="${r.off_time}"></td>
                <td>
                    <label class="toggle-switch">
                        <input type="checkbox" id="auto_${r.slave_id}" ${r.auto_schedule ? 'checked' : ''}>
                        <span class="toggle-slider"></span>
                    </label>
                </td>
                <td>
                    <button class="ctrl-btn" onclick="saveSchedule(${r.slave_id})">적용</button>
                </td>
            </tr>
        `).join('');
    })
    .catch(() => {
        document.getElementById('schedule_tbody').innerHTML =
            '<tr><td colspan="5" style="color:#f44">불러오기 실패</td></tr>';
    });
}

function saveSchedule(slaveId) {
    const on_time      = document.getElementById(`on_time_${slaveId}`).value;
    const off_time     = document.getElementById(`off_time_${slaveId}`).value;
    const auto_schedule = document.getElementById(`auto_${slaveId}`).checked ? 1 : 0;

    fetch('/api/schedule', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ slave_id: slaveId, on_time, off_time, auto_schedule }),
    })
    .then(r => r.json())
    .then(() => {
        const label = auto_schedule ? '자동 ON' : '자동 OFF';
        document.getElementById('schedule_result').innerText =
            `✔ Slave${slaveId} 스케줄 적용 — 점등 ${on_time} / 소등 ${off_time} (${label})`;
    })
    .catch(() => {
        document.getElementById('schedule_result').innerText = '✘ 저장 실패';
    });
}

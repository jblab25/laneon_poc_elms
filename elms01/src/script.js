/* ──────────────────────────────────────────
   ELMS Dashboard Script
   ────────────────────────────────────────── */
const socket = io();

let selectedSlave = 1;          // 대시보드에 표시할 슬레이브
const latestStatus = {};         // slave_id → 최신 상태 캐시

let latestJpbStatus = null;      // PoC: 최신 JPB STATUS(0x30)
let latestJsbSensor = null;      // PoC: 최신 JSB SENSOR(0x31 재조립 결과)

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

socket.on('jpb_status_update', (data) => {
    latestJpbStatus = data;
    _renderJpbStatus(data);
});

socket.on('jsb_sensor_update', (data) => {
    latestJsbSensor = data;
    _renderJsbSensor(data);
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
   전압 -> 밝기 단계 변환 (JPB 실측 캘리브레이션 기준)
   Lv1: 19.70~19.79V, Lv2: 19.80~19.89V, Lv3: 19.90~19.99V, Lv4: 20.00~20.10V
   JPB가 보고하는 raw brightness index 대신, 실제 측정 전압을 이 구간표에
   대입해 계산한 값을 화면에 표시한다(둘이 어긋나 보이는 걸 방지하기 위함).
   ────────────────────────────────────────── */
function voltageToBrightnessLevel(voltage) {
    if (voltage >= 20.00) return 4;
    if (voltage >= 19.90) return 3;
    if (voltage >= 19.80) return 2;
    if (voltage >= 19.70) return 1;
    return 0;
}

/* ──────────────────────────────────────────
   대시보드 카드 업데이트
   ────────────────────────────────────────── */
function _updateCards(d) {
    for (let i = 1; i <= 3; i++) {
        const vNum = d.voltage[i - 1];
        const v    = vNum.toFixed(2);
        const cur  = d.current[i - 1].toFixed(3);
        const on   = (d.lane_state >> (i - 1)) & 1 ? 'ON' : 'OFF';

        const bright = voltageToBrightnessLevel(vNum);
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
    ['dashboard', 'control', 'db', 'schedule', 'settings', 'poc_monitor'].forEach(t => {
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

/* ──────────────────────────────────────────
   PoC 모니터 — JPB STATUS(0x30) / JSB SENSOR(0x31)
   ────────────────────────────────────────── */
function _renderJpbStatus(d) {
    document.getElementById('poc_jpb_seq').innerText   = d.jpb_seq;
    document.getElementById('poc_jsb_link').innerText  = d.jsb_link_valid ? 'OK' : 'NO LINK';
    document.getElementById('poc_jsb_seq').innerText   = d.jsb_seq;
    document.getElementById('poc_jsb_age').innerText   = d.jsb_age_ms;

    ['lane1', 'lane2', 'lane3'].forEach((key, i) => {
        const n       = i + 1;
        const lane    = d[key];
        const voltage = lane.voltage_mv / 1000;
        document.getElementById(`poc_lane${n}_active`).innerText  = lane.active ? 'ON' : 'OFF';
        document.getElementById(`poc_lane${n}_bright`).innerText  = voltageToBrightnessLevel(voltage);
        document.getElementById(`poc_lane${n}_voltage`).innerText = voltage.toFixed(2);
        document.getElementById(`poc_lane${n}_current`).innerText = lane.current_ma;
    });
}

function _renderJsbSensor(d) {
    document.getElementById('poc_ncv_count').innerText  = d.ncv_count;
    document.getElementById('poc_bme_valid').innerText  = d.bme.valid ? 'OK' : 'INVALID';
    document.getElementById('poc_temp').innerText        = d.bme.temp;
    document.getElementById('poc_hum').innerText         = d.bme.hum;
    document.getElementById('poc_pres').innerText        = d.bme.pres;
    document.getElementById('poc_mic_count').innerText  = d.mic_count;
    document.getElementById('poc_tcs_valid').innerText  = d.tcs.valid ? 'OK' : 'INVALID';

    document.getElementById('poc_gps_fix').innerText = d.gps.valid ? 'FIX' : 'NO FIX';
    document.getElementById('poc_gps_latlon').innerText = d.gps.valid
        ? `${(d.gps.lat_e6 / 1e6).toFixed(6)}, ${(d.gps.lon_e6 / 1e6).toFixed(6)}`
        : '-';

    document.getElementById('poc_group_seq').innerText = d.group_seq;
    document.getElementById('poc_raw_size').innerText  = d.raw_packet_size;

    document.getElementById('poc_ncv_raw').innerText = JSON.stringify(d.ncv, null, 2);
    document.getElementById('poc_jsb_raw').innerText  = d.raw_text;

    if (d.fusion) _renderFusion(d.fusion);
}

function _renderFusion(f) {
    document.getElementById('fz_light_raw').innerText      = f.light.raw;
    document.getElementById('fz_light_filtered').innerText = f.light.filtered;
    document.getElementById('fz_light_target').innerText   = f.light.target_level;
    document.getElementById('fz_light_level').innerText    = f.light.level;

    document.getElementById('fz_rain_wet_distance').innerText = f.rain.wet_distance;
    document.getElementById('fz_rain_wet_present').innerText  = f.rain.wet_present ? 'YES' : 'NO';
    document.getElementById('fz_rain_event_state').innerText  = f.rain.event_state;
    document.getElementById('fz_rain_event_count').innerText  = f.rain.event_count_window;
    document.getElementById('fz_rain_target').innerText       = f.rain.target_level;
    document.getElementById('fz_rain_level').innerText        = f.rain.level;

    document.getElementById('fz_fog_humid_ready').innerText = f.fog.humid_ready ? 'READY' : 'GATE 잠김';
    document.getElementById('fz_fog_score').innerText       = f.fog.score;
    document.getElementById('fz_fog_target').innerText      = f.fog.target_level;
    document.getElementById('fz_fog_level').innerText       = f.fog.level;

    document.getElementById('fz_rs_mean').innerText       = f.ncv_features.rs_mean;
    document.getElementById('fz_rs_variation').innerText  = f.ncv_features.rs_variation;
    document.getElementById('fz_rs_impulse').innerText    = f.ncv_features.rs_impulse_ratio;
    document.getElementById('fz_rs_persistence').innerText = f.ncv_features.rs_persistence;

    document.getElementById('fz_mic_rms').innerText   = f.mic_feature.rms;
    document.getElementById('fz_mic_peak').innerText  = f.mic_feature.peak;
    document.getElementById('fz_mic_state').innerText = f.mic_feature.state;
}

function _fmtOnOff(v) {
    if (v === 1) return 'ON';
    if (v === 0) return 'OFF';
    if (v === -1) return 'MIXED';
    return '-';
}

function _renderAutoControl(a) {
    document.getElementById('ac_mode').innerText       = a.mode;
    document.getElementById('ac_env_level').innerText  = a.env_level;
    document.getElementById('ac_reason').innerText     = a.reason || '-';

    document.getElementById('ac_target_onoff').innerText  = _fmtOnOff(a.target_onoff);
    document.getElementById('ac_target_bright').innerText = a.target_bright;

    document.getElementById('ac_actual_onoff').innerText  = _fmtOnOff(a.actual_onoff);
    document.getElementById('ac_actual_bright').innerText =
        a.actual_bright === -1 ? 'MIXED' : (a.actual_bright ?? '-');

    const matchEl = document.getElementById('ac_match');
    if (a.control_match === null || a.control_match === undefined) {
        matchEl.innerText = '-';
    } else {
        matchEl.innerText = a.control_match ? '✔ MATCH' : '✘ MISMATCH';
        matchEl.style.color = a.control_match ? '#0f0' : '#f44';
    }
}

function _renderJsbDiag(diag) {
    document.getElementById('poc_chunks').innerText =
        `${diag.last_chunks_received} / ${diag.last_total_chunks || '-'}`;
    document.getElementById('poc_chunk_error').innerText  = diag.chunk_error_count;
    document.getElementById('poc_incomplete').innerText   = diag.incomplete_group_count;
    document.getElementById('poc_last_complete').innerText = diag.last_complete_group_seq ?? '-';
}

let _pocDiagTimer = null;

function loadMonitor() {
    if (latestJpbStatus) _renderJpbStatus(latestJpbStatus);
    if (latestJsbSensor) _renderJsbSensor(latestJsbSensor);

    const poll = () => {
        fetch('/api/monitor')
        .then(r => r.json())
        .then(data => {
            if (data.jpb) { latestJpbStatus = data.jpb; _renderJpbStatus(data.jpb); }
            if (data.jsb) { latestJsbSensor = data.jsb; _renderJsbSensor(data.jsb); }
            if (data.jsb_diag) _renderJsbDiag(data.jsb_diag);
            if (data.auto) _renderAutoControl(data.auto);
        })
        .catch(() => {});
    };

    poll();
    if (_pocDiagTimer) clearInterval(_pocDiagTimer);
    _pocDiagTimer = setInterval(poll, 2000);
}

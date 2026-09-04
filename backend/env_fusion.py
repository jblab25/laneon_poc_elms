"""
LANEON PoC — ELMS Environment Fusion (Light / Rain / Fog)

JPB 구펌웨어 sensor_fusion.c의 판정 로직을 JSB Raw 데이터(NCV76124/BME680/MIC) 기반으로
ELMS(Python)에 포팅한다. 사용자 승인 사항 반영:

- NCV0~NCV4는 평균 내지 않고 JSB UPTIME 기준 200ms 간격 sample로 순서대로 처리한다
  (NCV_i = UPTIME - (n-1-i)*200ms). IR/Delta/Event Latch/Light EMA/NCV rolling window는
  sample마다 갱신하고, Classify(레벨 확정)는 패킷(1초)당 1회만 수행한다.
- Wet Distance는 sample마다 계산하되, Wet Presence Hysteresis/Hold 상태판정은 Classify에서.
- MIC0/MIC1은 별도 채널이 아니라 단일 MIC의 500ms 간격 시간 샘플이므로, 합치거나
  MAX/OR 처리하지 않고 같은 갱신 로직을 MIC0(UPTIME-500) → MIC1(UPTIME) 순서로 두 번 호출한다.
- Rain은 Legacy Score 방식을 이식하지 않는다. Dry Baseline → Wet Presence + Distinct Event
  → Event Window → R0~R4 구조만 사용한다.
- Fog Score 파라미터는 Rain과 분리된 전용 설정(fog 섹션)으로 관리한다.
- TCS3448은 이번 Fusion 입력에 포함하지 않는다(저장/관찰만).
- 이 모듈은 L/R/F 계산과 Feature 노출까지만 담당한다. JPB로 나가는 제어 명령은 발행하지 않는다.
"""
import json
import os
from collections import deque

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'env_config.json')

_DEFAULT_CONFIG = {
    'light': {
        'threshold_l0_l1': 3500, 'threshold_l1_l2': 2500,
        'threshold_l2_l3': 1200, 'threshold_l3_l4': 400,
        'hysteresis': 100, 'hold_ms': 3000, 'ema_alpha': 0.25,
    },
    'rain': {
        'baseline_ir11': -4490, 'baseline_ir12': -5648,
        'baseline_ir21': -6056, 'baseline_ir22': -4637,
        'wet_enter_threshold': 300, 'wet_exit_threshold': 180,
        'wet_enter_hold_ms': 3000, 'dry_return_hold_ms': 5000,
        'event_enter_threshold': 500, 'event_exit_threshold': 200,
        'event_rearm_ms': 400, 'activity_window_ms': 10000,
        'r2_event_count': 1, 'r3_event_count': 3, 'r4_event_count': 6,
        'level_hold_ms': 3000,
    },
    'fog': {
        'humid_high_rh': 88.0, 'humid_low_rh': 83.0,
        'humid_high_hold_ms': 30000, 'humid_low_hold_ms': 60000,
        'w_persistence': 1.5, 'w_low_variation': 1.0,
        'w_mic_quiet': 0.5, 'w_impulse_penalty': 1.5,
        'score_up': [0.6, 1.2, 1.9, 2.6], 'score_down': [0.4, 1.0, 1.7, 2.4],
        'hold_ms': 6000, 'presence_floor': 300.0, 'var_norm': 500.0, 'impulse_k': 2.5,
    },
    'ncv': {'window_n': 15, 'sample_interval_ms': 200},
    'mic': {
        'sample_interval_ms': 500, 'baseline_window_n': 5,
        'impulse_ratio': 3.0, 'active_ratio': 1.5, 'continuous_hold_ms': 2000,
    },
}


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        print(f'[FUSION] 설정 파일 로드: {CONFIG_PATH}')
        return cfg
    except Exception as e:
        print(f'[FUSION] 설정 파일 로드 실패({e}) — 기본값 사용')
        return json.loads(json.dumps(_DEFAULT_CONFIG))


CONFIG = _load_config()


def _bin_with_hysteresis(value, up_th, down_th, current):
    """오름차순 지표(Fog Score)용 Schmitt-trigger 5단계(0~4) 비닝."""
    lvl = current
    while lvl < 4 and value >= up_th[lvl]:
        lvl += 1
    while lvl > 0 and value < down_th[lvl - 1]:
        lvl -= 1
    return lvl


def _light_bin_with_hysteresis(value, fall_th, rise_th, current):
    """LS2(밝을수록 값이 큼)용 — 값이 내림차순 threshold 아래로 떨어지면 더 어두운
    레벨(lvl 증가)로, hysteresis 폭만큼 다시 올라오면 밝은 레벨(lvl 감소)로 되돌아간다."""
    lvl = current
    while lvl < 4 and value < fall_th[lvl]:
        lvl += 1
    while lvl > 0 and value >= rise_th[lvl - 1]:
        lvl -= 1
    return lvl


class _LevelFilter:
    """target 레벨이 hold_ms 동안 유지돼야 committed 레벨로 확정하는 Persistence 필터."""

    def __init__(self):
        self.committed_level = 0
        self.pending_level = 0
        self.pending_since_ms = 0
        self.pending_active = False

    def update(self, target_level: int, now_ms: int, hold_ms: int) -> int:
        if target_level == self.committed_level:
            self.pending_active = False
            return self.committed_level
        if not self.pending_active or self.pending_level != target_level:
            self.pending_active = True
            self.pending_level = target_level
            self.pending_since_ms = now_ms
            return self.committed_level
        if now_ms - self.pending_since_ms >= hold_ms:
            self.committed_level = target_level
            self.pending_active = False
        return self.committed_level


class FusionEngine:
    def __init__(self):
        self.light_cal = CONFIG['light']
        self.rain_cal = CONFIG['rain']
        self.fog_cal = CONFIG['fog']
        self.ncv_cfg = CONFIG['ncv']
        self.mic_cfg = CONFIG['mic']

        self.rs1_buf = deque(maxlen=self.ncv_cfg['window_n'])
        self.rs2_buf = deque(maxlen=self.ncv_cfg['window_n'])

        self.ncv_valid = False
        self.light_ema_seeded = False
        self.light_raw = 0.0
        self.light_filtered = 0.0
        self.light_target_level = 0

        self.rain_delta_seeded = False
        self.prev_ir11 = self.prev_ir12 = self.prev_ir21 = self.prev_ir22 = 0
        self.prev_rs_combined = 0.0

        self.rs1_ir = 0
        self.rs2_ir = 0
        self.rs_mean = 0.0
        self.rs_diff = 0.0
        self.rs_variation = 0.0
        self.rs_impulse_ratio = 0.0
        self.rs_persistence = 0.0

        self.dist11 = self.dist12 = self.dist21 = self.dist22 = 0.0
        self.wet_distance = 0.0
        self.wet_present = False
        self.wet_above_since = 0
        self.wet_below_since = 0

        self.event_mag = 0.0
        self.event_state = 'IDLE'
        self.event_rearm_since_ms = 0
        self.event_active = False
        self.event_buf = []  # [(t_ms, mag), ...]
        self.event_count_window = 0
        self.event_max_window = 0.0
        self.rain_target_level = 0

        self.mic_buf = deque(maxlen=self.mic_cfg['baseline_window_n'])
        self.mic_active_since_ms = 0
        self.mic_rms = 0.0
        self.mic_peak = 0.0
        self.mic_crest = 0.0
        self.mic_impulse = False
        self.mic_event_duration_ms = 0
        self.mic_state = 'QUIET'

        self.bme_valid = False
        self.temperature_c = 0.0
        self.humidity_rh = 0.0
        self.pressure_hpa = 0.0
        self.humid_above_since = 0
        self.humid_below_since = 0
        self.humid_ready = False

        self.fog_score = 0.0
        self.fog_target_level = 0

        self.light_filter = _LevelFilter()
        self.rain_filter = _LevelFilter()
        self.fog_filter = _LevelFilter()

    # ------------------------------------------------------------
    def update_ncv_sample(self, ncv: dict, t_ms: int):
        """NCV76124 sample 1개(≈200ms 간격) 처리 — Delta/Event Latch/Wet Distance/
        Light EMA/rolling window를 전부 이 단위로 갱신한다."""
        r1l1, r2l1 = ncv['R1L1'], ncv['R2L1']
        r1l2, r2l2 = ncv['R1L2'], ncv['R2L2']
        r1dc, r2dc = ncv['R1DC'], ncv['R2DC']

        ir11 = r1l1 - r1dc
        ir12 = r1l2 - r1dc
        ir21 = r2l1 - r2dc
        ir22 = r2l2 - r2dc
        self.rs1_ir = ir11
        self.rs2_ir = ir22

        combined_now = (ir11 + ir22) / 2.0
        if not self.rain_delta_seeded:
            ir11_delta = ir12_delta = ir21_delta = ir22_delta = 0.0
            self.rain_delta_seeded = True
        else:
            ir11_delta = ir11 - self.prev_ir11
            ir12_delta = ir12 - self.prev_ir12
            ir21_delta = ir21 - self.prev_ir21
            ir22_delta = ir22 - self.prev_ir22
        self.prev_ir11, self.prev_ir12 = ir11, ir12
        self.prev_ir21, self.prev_ir22 = ir21, ir22
        self.prev_rs_combined = combined_now

        rc = self.rain_cal
        self.dist11 = abs(ir11 - rc['baseline_ir11'])
        self.dist12 = abs(ir12 - rc['baseline_ir12'])
        self.dist21 = abs(ir21 - rc['baseline_ir21'])
        self.dist22 = abs(ir22 - rc['baseline_ir22'])
        self.wet_distance = (self.dist11 + self.dist12 + self.dist21 + self.dist22) / 4.0

        mag = max(abs(ir11_delta), abs(ir12_delta), abs(ir21_delta), abs(ir22_delta))
        self.event_mag = mag

        distinct_event = False
        if self.event_state == 'IDLE':
            if mag >= rc['event_enter_threshold']:
                distinct_event = True
                self.event_state = 'ACTIVE'
                self.event_rearm_since_ms = 0
        else:  # ACTIVE
            if mag <= rc['event_exit_threshold']:
                if self.event_rearm_since_ms == 0:
                    self.event_rearm_since_ms = t_ms
                elif t_ms - self.event_rearm_since_ms >= rc['event_rearm_ms']:
                    self.event_state = 'IDLE'
                    self.event_rearm_since_ms = 0
            else:
                self.event_rearm_since_ms = 0

        self.event_active = distinct_event
        if distinct_event:
            self.event_buf.append((t_ms, mag))

        self.rs1_buf.append(ir11)
        self.rs2_buf.append(ir22)

        n = len(self.rs1_buf)
        avgs = [(self.rs1_buf[i] + self.rs2_buf[i]) / 2.0 for i in range(n)]
        self.rs_mean = sum(avgs) / n
        self.rs_diff = sum(abs(self.rs1_buf[i] - self.rs2_buf[i]) for i in range(n)) / n
        self.rs_variation = sum(abs(a - self.rs_mean) for a in avgs) / n

        impulse_floor = self.fog_cal['impulse_k'] * self.rs_variation
        presence_floor = self.fog_cal['presence_floor']
        impulse_cnt = sum(1 for a in avgs if abs(a - self.rs_mean) > impulse_floor)
        presence_cnt = sum(1 for a in avgs if a > presence_floor)
        self.rs_impulse_ratio = impulse_cnt / n
        self.rs_persistence = presence_cnt / n

        self.light_raw = float(ncv['LS2'])
        alpha = self.light_cal['ema_alpha']
        if not self.light_ema_seeded:
            self.light_filtered = self.light_raw
            self.light_ema_seeded = True
        else:
            self.light_filtered = alpha * self.light_raw + (1.0 - alpha) * self.light_filtered

        self.ncv_valid = True

    # ------------------------------------------------------------
    def update_mic_sample(self, rms: float, peak: float, t_ms: int):
        """MIC 시간 샘플 1개(≈500ms 간격, 단일 채널) 처리."""
        rms = float(rms)
        peak = float(peak)
        self.mic_rms = rms
        self.mic_peak = peak
        self.mic_crest = (peak / rms) if rms > 0 else 0.0

        baseline = (sum(self.mic_buf) / len(self.mic_buf)) if self.mic_buf else rms

        mc = self.mic_cfg
        self.mic_impulse = baseline > 0 and rms > baseline * mc['impulse_ratio']
        active_now = baseline > 0 and rms > baseline * mc['active_ratio']

        if active_now:
            if self.mic_active_since_ms == 0:
                self.mic_active_since_ms = t_ms
            self.mic_event_duration_ms = t_ms - self.mic_active_since_ms
        else:
            self.mic_active_since_ms = 0
            self.mic_event_duration_ms = 0

        if self.mic_impulse and self.mic_event_duration_ms < mc['continuous_hold_ms']:
            self.mic_state = 'IMPULSE'
        elif self.mic_event_duration_ms >= mc['continuous_hold_ms']:
            self.mic_state = 'CONTINUOUS_ACTIVITY'
        else:
            self.mic_state = 'QUIET'

        self.mic_buf.append(rms)

    # ------------------------------------------------------------
    def update_bme(self, valid: bool, temp_c: float, hum_rh: float, pres_hpa: float, t_ms: int):
        self.bme_valid = valid
        if not valid:
            return
        self.temperature_c = temp_c
        self.humidity_rh = hum_rh
        self.pressure_hpa = pres_hpa

        fc = self.fog_cal
        if hum_rh >= fc['humid_high_rh']:
            if self.humid_above_since == 0:
                self.humid_above_since = t_ms
            self.humid_below_since = 0
            if t_ms - self.humid_above_since >= fc['humid_high_hold_ms']:
                self.humid_ready = True
        elif hum_rh <= fc['humid_low_rh']:
            if self.humid_below_since == 0:
                self.humid_below_since = t_ms
            self.humid_above_since = 0
            if t_ms - self.humid_below_since >= fc['humid_low_hold_ms']:
                self.humid_ready = False
        else:
            self.humid_above_since = 0
            self.humid_below_since = 0

    # ------------------------------------------------------------
    def _update_wet_presence(self, now_ms: int):
        rc = self.rain_cal
        if self.wet_distance >= rc['wet_enter_threshold']:
            if self.wet_above_since == 0:
                self.wet_above_since = now_ms
            self.wet_below_since = 0
            if now_ms - self.wet_above_since >= rc['wet_enter_hold_ms']:
                self.wet_present = True
        elif self.wet_distance <= rc['wet_exit_threshold']:
            if self.wet_below_since == 0:
                self.wet_below_since = now_ms
            self.wet_above_since = 0
            if now_ms - self.wet_below_since >= rc['dry_return_hold_ms']:
                self.wet_present = False
        else:
            self.wet_above_since = 0
            self.wet_below_since = 0

    def _update_event_window(self, now_ms: int):
        window_ms = self.rain_cal['activity_window_ms']
        self.event_buf = [(t, m) for (t, m) in self.event_buf if now_ms - t <= window_ms]
        self.event_count_window = len(self.event_buf)
        self.event_max_window = max((m for (_, m) in self.event_buf), default=0.0)

    def classify(self, now_ms: int):
        """패킷(1초)당 1회 — Light/Rain/Fog 최종 레벨을 확정한다."""
        # ---- Light ----
        if self.ncv_valid:
            lc = self.light_cal
            fall_th = [lc['threshold_l0_l1'], lc['threshold_l1_l2'],
                       lc['threshold_l2_l3'], lc['threshold_l3_l4']]
            rise_th = [th + lc['hysteresis'] for th in fall_th]
            target = _light_bin_with_hysteresis(
                self.light_filtered, fall_th, rise_th, self.light_filter.committed_level)
            self.light_target_level = target
            self.light_filter.update(target, now_ms, lc['hold_ms'])

        # ---- Rain: Dry Baseline -> Wet Presence + Distinct Event -> Event Window -> R0~R4 ----
        if self.ncv_valid:
            rc = self.rain_cal
            self._update_wet_presence(now_ms)
            self._update_event_window(now_ms)

            if self.event_count_window >= rc['r4_event_count']:
                target = 4
            elif self.event_count_window >= rc['r3_event_count']:
                target = 3
            elif self.event_count_window >= rc['r2_event_count']:
                target = 2
            elif self.wet_present:
                target = 1
            else:
                target = 0
            self.rain_target_level = target
            self.rain_filter.update(target, now_ms, rc['level_hold_ms'])

        # ---- Fog: BME680 RH Gate + NCV persistence/variation/impulse (Fog 전용 파라미터) ----
        fc = self.fog_cal
        if not self.humid_ready or not self.ncv_valid:
            self.fog_score = 0.0
        else:
            low_var = 1.0 - (self.rs_variation / fc['var_norm'])
            if low_var < 0.0:
                low_var = 0.0
            mic_quiet = fc['w_mic_quiet'] if self.mic_state == 'QUIET' else 0.0
            score = (fc['w_persistence'] * self.rs_persistence +
                     fc['w_low_variation'] * low_var +
                     mic_quiet -
                     fc['w_impulse_penalty'] * self.rs_impulse_ratio)
            self.fog_score = max(score, 0.0)

        target = _bin_with_hysteresis(self.fog_score, fc['score_up'], fc['score_down'],
                                       self.fog_filter.committed_level)
        self.fog_target_level = target
        self.fog_filter.update(target, now_ms, fc['hold_ms'])

    def snapshot(self) -> dict:
        return {
            'ncv_valid': self.ncv_valid,
            'light': {
                'raw': self.light_raw,
                'filtered': round(self.light_filtered, 1),
                'target_level': self.light_target_level,
                'level': self.light_filter.committed_level,
            },
            'rain': {
                'wet_distance': round(self.wet_distance, 1),
                'wet_present': self.wet_present,
                'event_mag': round(self.event_mag, 1),
                'event_state': self.event_state,
                'event_active': self.event_active,
                'event_count_window': self.event_count_window,
                'event_max_window': round(self.event_max_window, 1),
                'target_level': self.rain_target_level,
                'level': self.rain_filter.committed_level,
            },
            'fog': {
                'humid_ready': self.humid_ready,
                'score': round(self.fog_score, 3),
                'target_level': self.fog_target_level,
                'level': self.fog_filter.committed_level,
            },
            'ncv_features': {
                'rs1_ir': self.rs1_ir,
                'rs2_ir': self.rs2_ir,
                'rs_mean': round(self.rs_mean, 1),
                'rs_diff': round(self.rs_diff, 1),
                'rs_variation': round(self.rs_variation, 1),
                'rs_impulse_ratio': round(self.rs_impulse_ratio, 3),
                'rs_persistence': round(self.rs_persistence, 3),
            },
            'mic_feature': {
                'rms': self.mic_rms,
                'peak': self.mic_peak,
                'crest': round(self.mic_crest, 2),
                'state': self.mic_state,
                'event_duration_ms': self.mic_event_duration_ms,
            },
            'bme_feature': {
                'valid': self.bme_valid,
                'temp_c': self.temperature_c,
                'humidity_rh': self.humidity_rh,
                'pressure_hpa': self.pressure_hpa,
            },
        }


_engine = FusionEngine()


def process_jsb_packet(sensor: dict) -> dict:
    """JSB 1초 패킷(파싱된 sensor dict, env_fusion._parse_jsb_fields 결과 형식)을 받아
    NCV/MIC 200ms/500ms 단위 sample 처리 후 패킷당 1회 Classify()를 수행하고
    L/R/F + Feature 스냅샷을 반환한다. 제어 명령은 발행하지 않는다."""
    uptime = sensor.get('uptime', 0)

    ncv_list = sensor.get('ncv', [])
    n = len(ncv_list)
    if n > 0:
        interval = _engine.ncv_cfg['sample_interval_ms']
        for i, ncv in enumerate(ncv_list):
            t_ms = uptime - (n - 1 - i) * interval
            _engine.update_ncv_sample(ncv, t_ms)
    else:
        _engine.ncv_valid = False

    mic_list = sensor.get('mic', [])
    m = len(mic_list)
    if m > 0:
        interval = _engine.mic_cfg['sample_interval_ms']
        for j, mic in enumerate(mic_list):
            t_ms = uptime - (m - 1 - j) * interval
            _engine.update_mic_sample(mic['rms'], mic['peak'], t_ms)

    bme = sensor.get('bme', {})
    _engine.update_bme(bme.get('valid', False), bme.get('temp', 0.0),
                        bme.get('hum', 0.0), bme.get('pres', 0.0), uptime)

    _engine.classify(uptime)

    return _engine.snapshot()

#!/usr/bin/env python3
"""
LANEON PoC — 6시간 Environment Analyzer / Markdown Report 생성기

SQLite → Python 통계분석 → Markdown Report → (선택) Email 만 수행한다.
LLM/AI를 사용하지 않는다. 분석 결과로 env_config.json/threshold/AUTO 정책을
자동으로 바꾸지 않는다 — 항상 "Tuning Candidate"로만 제안하고 사람이 승인해야
env_config.json 등을 수정한다.

운영 제어 경로(app.py의 UART/Fusion/MANUAL/AUTO)와는 완전히 분리된 별도
프로세스로 실행된다. SQLite는 WAL 모드라 이 read-only 접근이 운영 writer를
장시간 lock하지 않는다.

사용 예:
    python3 backend/env_reporter.py --hours 6
    python3 backend/env_reporter.py --since "2026-09-05 10:00" --until "2026-09-05 16:00"
    python3 backend/env_reporter.py            # 인자 없음: 직전에 끝난 6시간 KST 블록
                                                # (00-06/06-12/12-18/18-24) 자동 분석 — systemd timer용
"""
import argparse
import json
import math
import os
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DB_PATH     = os.path.join(BASE_DIR, 'elms.db')
ENV_CFG_PATH    = os.path.join(BASE_DIR, 'env_config.json')
REPORT_CFG_PATH = os.path.join(BASE_DIR, 'report_config.json')

TS_FMT = '%Y-%m-%d %H:%M:%S'


# ──────────────────────────────────────────
# 설정 로드 (하드코딩 금지 — env_config.json/report_config.json을 그대로 읽는다)
# ──────────────────────────────────────────
def load_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'[env_reporter] 설정 로드 실패({path}): {e}', file=sys.stderr)
        return default or {}


# ──────────────────────────────────────────
# 통계 유틸 (numpy/pandas 없이 stdlib만 사용)
# ──────────────────────────────────────────
def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def stats_summary(values):
    """min/max/mean/median/P5/P50/P95 — None 값은 제외하고 계산."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return {
        'count':  len(vals),
        'min':    round(vals[0], 2),
        'max':    round(vals[-1], 2),
        'mean':   round(statistics.mean(vals), 2),
        'median': round(statistics.median(vals), 2),
        'p5':     round(percentile(vals, 5), 2),
        'p50':    round(percentile(vals, 50), 2),
        'p95':    round(percentile(vals, 95), 2),
    }


def fmt_stats(s, unit=''):
    if s is None:
        return '(데이터 없음)'
    return (f"min={s['min']}{unit} max={s['max']}{unit} mean={s['mean']}{unit} "
            f"median={s['median']}{unit} P5={s['p5']}{unit} P50={s['p50']}{unit} P95={s['p95']}{unit} "
            f"(n={s['count']})")


def parse_ts(ts_str):
    return datetime.strptime(ts_str, TS_FMT)


def dwell_times(rows_ts_level, levels=(0, 1, 2, 3, 4)):
    """[(ts, level), ...] (시간순 정렬됨)에서 각 level에 머문 총 시간을 초 단위로 계산.
    마지막 구간은 다음 row까지의 간격을 그대로 사용(마지막 row는 반영 안 함)."""
    dwell = {lv: 0.0 for lv in levels}
    for i in range(len(rows_ts_level) - 1):
        ts0, lv0 = rows_ts_level[i]
        ts1, _   = rows_ts_level[i + 1]
        dt = (ts1 - ts0).total_seconds()
        if 0 < dt < 60:  # 60초 이상 벌어지면 결측 구간으로 보고 dwell에 포함하지 않는다
            dwell[lv0] = dwell.get(lv0, 0.0) + dt
    return dwell


def count_transitions(levels_seq):
    """연속값 리스트에서 값이 바뀐 횟수."""
    cnt = 0
    for i in range(1, len(levels_seq)):
        if levels_seq[i] != levels_seq[i - 1]:
            cnt += 1
    return cnt


def find_gaps(ts_list, gap_threshold_sec=5):
    """정렬된 timestamp 리스트에서 gap_threshold_sec 이상 벌어진 구간을 찾는다."""
    gaps = []
    for i in range(1, len(ts_list)):
        dt = (ts_list[i] - ts_list[i - 1]).total_seconds()
        if dt >= gap_threshold_sec:
            gaps.append((ts_list[i - 1], ts_list[i], dt))
    return gaps


# ──────────────────────────────────────────
# DB 접근 — 운영 writer를 막지 않도록 read-only 성격으로 짧게 연결
# ──────────────────────────────────────────
def open_ro_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=5000')
    try:
        conn.execute('PRAGMA query_only=1')
    except sqlite3.OperationalError:
        pass
    return conn


def fetch_window(conn, table, start_ts, end_ts, extra_where=''):
    sql = f"SELECT * FROM {table} WHERE ts >= ? AND ts < ? {extra_where} ORDER BY ts"
    return conn.execute(sql, (start_ts.strftime(TS_FMT), end_ts.strftime(TS_FMT))).fetchall()


# ──────────────────────────────────────────
# 분석 시간창 결정
# ──────────────────────────────────────────
def resolve_window(args):
    now = datetime.now()
    if args.since or args.until:
        if not (args.since and args.until):
            print('--since와 --until은 함께 지정해야 합니다.', file=sys.stderr)
            sys.exit(1)
        start = datetime.strptime(args.since, TS_FMT)
        end   = datetime.strptime(args.until, TS_FMT)
        return start, end
    if args.hours:
        end = now.replace(microsecond=0)
        start = end - timedelta(hours=args.hours)
        return start, end
    # 인자 없음: 직전에 끝난 6시간 KST 블록 (00/06/12/18시 경계) — systemd timer 기본 동작
    boundary_hour = (now.hour // 6) * 6
    end = now.replace(hour=boundary_hour, minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=6)
    return start, end


# ──────────────────────────────────────────
# 1. System / Data Health
# ──────────────────────────────────────────
def analyze_health(conn, start, end, db_queue_drop_count):
    window_sec = (end - start).total_seconds()
    pkt_rows = fetch_window(conn, 'jsb_packet_history', start, end)
    jpb_rows = fetch_window(conn, 'jpb_status_history', start, end)

    expected = int(window_sec)  # 1 packet/sec 설계
    actual = len(pkt_rows)
    reception_rate = round(actual / expected * 100, 1) if expected else None

    seqs = [r['jsb_seq'] for r in pkt_rows if r['jsb_seq'] is not None]
    seq_gaps = 0
    for i in range(1, len(seqs)):
        d = seqs[i] - seqs[i - 1]
        if d > 1:
            seq_gaps += (d - 1)

    ts_list = [parse_ts(r['ts']) for r in pkt_rows]
    gaps = find_gaps(ts_list, gap_threshold_sec=5)

    gps_valid_ratio = None
    bme_valid_ratio = None
    tcs_valid_ratio = None
    ncv_total = 0
    mic_total = 0
    chunk_error_delta = None
    incomplete_delta = None
    if pkt_rows:
        gps_valid_ratio = round(sum(1 for r in pkt_rows if r['gps_valid']) / len(pkt_rows) * 100, 1)
        bme_valid_ratio = round(sum(1 for r in pkt_rows if r['bme_valid']) / len(pkt_rows) * 100, 1)
        tcs_valid_ratio = round(sum(1 for r in pkt_rows if r['tcs_valid']) / len(pkt_rows) * 100, 1)
        ncv_total = sum(r['ncv_count'] or 0 for r in pkt_rows)
        mic_total = sum(r['mic_count'] or 0 for r in pkt_rows)
        ce = [r['chunk_error_count_cum'] for r in pkt_rows if r['chunk_error_count_cum'] is not None]
        ig = [r['incomplete_group_count_cum'] for r in pkt_rows if r['incomplete_group_count_cum'] is not None]
        if ce:
            chunk_error_delta = ce[-1] - ce[0]
        if ig:
            incomplete_delta = ig[-1] - ig[0]

    return {
        'window_sec': window_sec,
        'expected_packets': expected,
        'actual_packets': actual,
        'reception_rate_pct': reception_rate,
        'seq_gap_total': seq_gaps,
        'chunk_error_count_delta': chunk_error_delta,
        'incomplete_group_count_delta': incomplete_delta,
        'gps_valid_ratio_pct': gps_valid_ratio,
        'bme_valid_ratio_pct': bme_valid_ratio,
        'tcs_valid_ratio_pct': tcs_valid_ratio,
        'ncv_sample_total': ncv_total,
        'mic_sample_total': mic_total,
        'jpb_status_rows': len(jpb_rows),
        'db_gaps': gaps,
        'db_queue_drop_count': db_queue_drop_count,
    }


# ──────────────────────────────────────────
# 2. Light 분석
# ──────────────────────────────────────────
def analyze_light(conn, start, end, env_cfg):
    rows = fetch_window(conn, 'fusion_history', start, end)
    light_cal = env_cfg.get('light', {})

    raw_vals = [r['light_raw'] for r in rows]
    filt_vals = [r['light_filtered'] for r in rows]

    ts_level = [(parse_ts(r['ts']), r['light_level']) for r in rows if r['light_level'] is not None]
    dwell = dwell_times(ts_level)

    target_seq = [r['light_target'] for r in rows if r['light_target'] is not None]
    level_seq  = [r['light_level'] for r in rows if r['light_level'] is not None]

    diffs = [abs((r['light_raw'] or 0) - (r['light_filtered'] or 0)) for r in rows]

    return {
        'raw_stats': stats_summary(raw_vals),
        'filtered_stats': stats_summary(filt_vals),
        'raw_vs_ema_diff_stats': stats_summary(diffs),
        'dwell_sec': dwell,
        'target_transitions': count_transitions(target_seq),
        'committed_transitions': count_transitions(level_seq),
        'thresholds': {
            'l0_l1': light_cal.get('threshold_l0_l1'),
            'l1_l2': light_cal.get('threshold_l1_l2'),
            'l2_l3': light_cal.get('threshold_l2_l3'),
            'l3_l4': light_cal.get('threshold_l3_l4'),
            'hysteresis': light_cal.get('hysteresis'),
            'hold_ms': light_cal.get('hold_ms'),
        },
        'row_count': len(rows),
    }


# ──────────────────────────────────────────
# 3. Rain 분석
# ──────────────────────────────────────────
def analyze_rain(conn, start, end, env_cfg):
    fusion_rows = fetch_window(conn, 'fusion_history', start, end)
    pkt_rows = fetch_window(conn, 'jsb_packet_history', start, end)
    rain_cal = env_cfg.get('rain', {})

    ir11s, ir12s, ir21s, ir22s = [], [], [], []
    b11, b12, b21, b22 = (rain_cal.get('baseline_ir11', 0), rain_cal.get('baseline_ir12', 0),
                          rain_cal.get('baseline_ir21', 0), rain_cal.get('baseline_ir22', 0))
    for r in pkt_rows:
        try:
            ncv_list = json.loads(r['ncv_json']) if r['ncv_json'] else []
        except Exception:
            ncv_list = []
        for ncv in ncv_list:
            ir11s.append(ncv['R1L1'] - ncv['R1DC'])
            ir12s.append(ncv['R1L2'] - ncv['R1DC'])
            ir21s.append(ncv['R2L1'] - ncv['R2DC'])
            ir22s.append(ncv['R2L2'] - ncv['R2DC'])

    wet_dist_vals = [r['rain_wet_distance'] for r in fusion_rows]
    event_mag_vals = [r['rain_event_mag'] for r in fusion_rows]
    event_count_vals = [r['rain_event_count_window'] for r in fusion_rows]

    wet_present_seq = [r['rain_wet_present'] for r in fusion_rows if r['rain_wet_present'] is not None]
    wet_present_ratio = round(sum(wet_present_seq) / len(wet_present_seq) * 100, 1) if wet_present_seq else None

    event_state_counter = Counter(r['rain_event_state'] for r in fusion_rows if r['rain_event_state'])

    target_seq = [r['rain_target'] for r in fusion_rows if r['rain_target'] is not None]
    level_seq  = [r['rain_level'] for r in fusion_rows if r['rain_level'] is not None]

    # "Dry 추정 구간" = rain_level == 0 인 row들 (사람이 실제 비를 뿌린 test_marker가 없다면
    # 전체 창을 Dry로 간주해도 무방 — 현재 랩/PoC 단계에서 안전한 근사)
    dry_wet_dist = [r['rain_wet_distance'] for r in fusion_rows if r['rain_level'] == 0]

    return {
        'ir11_stats': stats_summary(ir11s), 'ir12_stats': stats_summary(ir12s),
        'ir21_stats': stats_summary(ir21s), 'ir22_stats': stats_summary(ir22s),
        'wet_distance_stats': stats_summary(wet_dist_vals),
        'wet_distance_dry_stats': stats_summary(dry_wet_dist),
        'event_mag_stats': stats_summary(event_mag_vals),
        'event_count_window_stats': stats_summary(event_count_vals),
        'wet_present_ratio_pct': wet_present_ratio,
        'event_state_counts': dict(event_state_counter),
        'target_transitions': count_transitions(target_seq),
        'committed_transitions': count_transitions(level_seq),
        'baseline': {'ir11': b11, 'ir12': b12, 'ir21': b21, 'ir22': b22},
        'wet_enter_threshold': rain_cal.get('wet_enter_threshold'),
        'wet_exit_threshold': rain_cal.get('wet_exit_threshold'),
        'row_count': len(fusion_rows),
    }


# ──────────────────────────────────────────
# 4. Fog 분석
# ──────────────────────────────────────────
def analyze_fog(conn, start, end, env_cfg):
    fusion_rows = fetch_window(conn, 'fusion_history', start, end)
    pkt_rows = fetch_window(conn, 'jsb_packet_history', start, end)
    fog_cal = env_cfg.get('fog', {})

    rh_vals = [r['bme_hum_x10'] / 10.0 for r in pkt_rows if r['bme_valid'] and r['bme_hum_x10'] is not None]

    humid_ready_seq = [r['fog_humid_ready'] for r in fusion_rows if r['fog_humid_ready'] is not None]
    gate_open_sec = 0.0
    gate_closed_sec = 0.0
    ts_ready = [(parse_ts(r['ts']), r['fog_humid_ready']) for r in fusion_rows if r['fog_humid_ready'] is not None]
    for i in range(len(ts_ready) - 1):
        dt = (ts_ready[i + 1][0] - ts_ready[i][0]).total_seconds()
        if 0 < dt < 60:
            if ts_ready[i][1]:
                gate_open_sec += dt
            else:
                gate_closed_sec += dt

    rs_mean_vals = [r['rs_mean'] for r in fusion_rows]
    rs_var_vals = [r['rs_variation'] for r in fusion_rows]
    rs_impulse_vals = [r['rs_impulse_ratio'] for r in fusion_rows]
    rs_persist_vals = [r['rs_persistence'] for r in fusion_rows]
    fog_score_vals = [r['fog_score'] for r in fusion_rows]

    mic_state_counter = Counter(r['mic_state'] for r in fusion_rows if r['mic_state'])

    target_seq = [r['fog_target'] for r in fusion_rows if r['fog_target'] is not None]
    level_seq  = [r['fog_level'] for r in fusion_rows if r['fog_level'] is not None]

    ever_fog_observed = any(lv and lv > 0 for lv in level_seq)

    return {
        'rh_stats': stats_summary(rh_vals),
        'humid_ready_ratio_pct': round(sum(humid_ready_seq) / len(humid_ready_seq) * 100, 1) if humid_ready_seq else None,
        'gate_open_sec': gate_open_sec,
        'gate_closed_sec': gate_closed_sec,
        'rs_mean_stats': stats_summary(rs_mean_vals),
        'rs_variation_stats': stats_summary(rs_var_vals),
        'rs_impulse_ratio_stats': stats_summary(rs_impulse_vals),
        'rs_persistence_stats': stats_summary(rs_persist_vals),
        'fog_score_stats': stats_summary(fog_score_vals),
        'mic_state_counts': dict(mic_state_counter),
        'target_transitions': count_transitions(target_seq),
        'committed_transitions': count_transitions(level_seq),
        'ever_fog_observed': ever_fog_observed,
        'row_count': len(fusion_rows),
    }


# ──────────────────────────────────────────
# 5. MIC 분석 (MIC0/MIC1 = 한 MIC의 500ms 시간순 샘플, 채널 아님)
# ──────────────────────────────────────────
def analyze_mic(conn, start, end):
    pkt_rows = fetch_window(conn, 'jsb_packet_history', start, end)
    fusion_rows = fetch_window(conn, 'fusion_history', start, end)

    rms_series = []  # [(ts, rms), ...] — MIC0=ts-500ms, MIC1=ts
    peak_series = []
    for r in pkt_rows:
        try:
            mic_list = json.loads(r['mic_json']) if r['mic_json'] else []
        except Exception:
            mic_list = []
        base_ts = parse_ts(r['ts'])
        n = len(mic_list)
        for j, m in enumerate(mic_list):
            t = base_ts - timedelta(milliseconds=(n - 1 - j) * 500)
            rms_series.append((t, m.get('rms')))
            peak_series.append((t, m.get('peak')))

    rms_vals = [v for _, v in rms_series if v is not None]
    peak_vals = [v for _, v in peak_series if v is not None]

    def stats_with_p99(values):
        vals = sorted(v for v in values if v is not None)
        if not vals:
            return None
        s = stats_summary(vals)
        s['p99'] = round(percentile(vals, 99), 2)
        return s

    mic_state_counter = Counter(r['mic_state'] for r in fusion_rows if r['mic_state'])

    # 상위 5개 RMS 스파이크 + 그 시점 근방(±30s) Rain Event / test_marker 상관관계는
    # build_report()에서 test_marker 조회 결과와 합쳐서 표시한다 (여기서는 스파이크만 추출)
    top_spikes = sorted(rms_series, key=lambda x: (x[1] if x[1] is not None else -1), reverse=True)[:5]

    return {
        'rms_stats': stats_with_p99(rms_vals),
        'peak_stats': stats_with_p99(peak_vals),
        'mic_state_counts': dict(mic_state_counter),
        'top_rms_spikes': [(t.strftime(TS_FMT), v) for t, v in top_spikes],
        'sample_count': len(rms_series),
    }


# ──────────────────────────────────────────
# 6. AUTO Control 분석
# ──────────────────────────────────────────
def analyze_auto_control(conn, start, end):
    auto_rows = fetch_window(conn, 'auto_control_history', start, end)
    jpb_rows  = fetch_window(conn, 'jpb_status_history', start, end)
    event_rows = fetch_window(conn, 'control_event_log', start, end)

    auto_mode_rows = [r for r in auto_rows if r['mode'] == 'AUTO']
    match_vals = [r['control_match'] for r in auto_mode_rows if r['control_match'] is not None]
    match_ratio = round(sum(match_vals) / len(match_vals) * 100, 1) if match_vals else None
    mismatch_count = sum(1 for v in match_vals if v == 0)

    # mismatch 지속시간: 연속 mismatch(0) 구간의 길이 합산/최대
    ts_match = [(parse_ts(r['ts']), r['control_match']) for r in auto_mode_rows if r['control_match'] is not None]
    mismatch_durations = []
    run_start = None
    for i, (ts, m) in enumerate(ts_match):
        if m == 0 and run_start is None:
            run_start = ts
        if (m == 1 or i == len(ts_match) - 1) and run_start is not None:
            end_ts = ts if m == 1 else ts
            mismatch_durations.append((end_ts - run_start).total_seconds())
            run_start = None

    # "AUTO 제어 횟수" 등은 실제로 AUTO 모드에서 나간 명령만 집계한다(MANUAL 명령 제외).
    # MODE_CHANGE는 모드 전환 자체를 세는 것이므로 모드 필터를 적용하지 않는다.
    auto_events = [r for r in event_rows if r['mode'] == 'AUTO']
    onoff_events = [r for r in auto_events if r['event_type'] == 'ONOFF']
    bright_events = [r for r in auto_events if r['event_type'] == 'BRIGHTNESS']
    mode_events = [r for r in event_rows if r['event_type'] == 'MODE_CHANGE']

    on_transitions = sum(1 for r in onoff_events if r['value'] == 1)
    off_transitions = sum(1 for r in onoff_events if r['value'] == 0)

    # Lane별 실측 dwell/변화 (배선 이상 등 감지용 — 3-Lane이 서로 다르게 움직이는지)
    per_lane = {}
    for lane in (1, 2, 3):
        active_seq = [r[f'lane{lane}_active'] for r in jpb_rows if r[f'lane{lane}_active'] is not None]
        bright_seq = [r[f'lane{lane}_bright'] for r in jpb_rows if r[f'lane{lane}_bright'] is not None]
        volt_vals  = [r[f'lane{lane}_voltage_mv'] for r in jpb_rows]
        cur_vals   = [r[f'lane{lane}_current_ma'] for r in jpb_rows]
        per_lane[lane] = {
            'active_on_ratio_pct': round(sum(active_seq) / len(active_seq) * 100, 1) if active_seq else None,
            'bright_transitions': count_transitions(bright_seq),
            'voltage_stats': stats_summary(volt_vals),
            'current_stats': stats_summary(cur_vals),
        }

    return {
        'auto_mode_rows': len(auto_mode_rows),
        'control_match_ratio_pct': match_ratio,
        'control_mismatch_count': mismatch_count,
        'mismatch_durations_sec': mismatch_durations,
        'auto_command_count': len(onoff_events) + len(bright_events),
        'on_transitions': on_transitions,
        'off_transitions': off_transitions,
        'brightness_change_count': len(bright_events),
        'mode_change_count': len(mode_events),
        'per_lane': per_lane,
    }


# ──────────────────────────────────────────
# 7. Test Marker
# ──────────────────────────────────────────
def analyze_markers(conn, start, end):
    rows = fetch_window(conn, 'test_marker', start, end)
    fusion_rows = fetch_window(conn, 'fusion_history', start, end)

    marker_context = []
    for r in rows:
        mt = parse_ts(r['ts'])
        nearest = min(fusion_rows, key=lambda fr: abs((parse_ts(fr['ts']) - mt).total_seconds()), default=None)
        ctx = None
        if nearest is not None and abs((parse_ts(nearest['ts']) - mt).total_seconds()) <= 10:
            ctx = {'light_level': nearest['light_level'], 'rain_level': nearest['rain_level'],
                   'fog_level': nearest['fog_level']}
        marker_context.append({'ts': r['ts'], 'label': r['label'], 'memo': r['memo'], 'env_at_marker': ctx})
    return marker_context


# ──────────────────────────────────────────
# Tuning Candidate 판정 — 3단계 분류만 한다. Config를 직접 바꾸지 않는다.
# ──────────────────────────────────────────
NO_CHANGE = 'NO CHANGE RECOMMENDED'
OBSERVE   = 'OBSERVATION REQUIRED'
CANDIDATE = 'TUNING CANDIDATE — USER APPROVAL REQUIRED'


def tuning_candidates(health, light, rain, fog, mic, auto, markers):
    candidates = []

    # Rain: Dry 구간 wet_distance vs threshold margin
    wd = rain['wet_distance_dry_stats']
    we, wx = rain['wet_enter_threshold'], rain['wet_exit_threshold']
    if wd and we is not None:
        margin = we - wd['p95']
        if margin > we * 0.3:
            verdict = NO_CHANGE
            note = f"Dry 구간 WET_DISTANCE P95={wd['p95']}, wet_enter={we} — margin 충분(여유 {margin:.0f})"
        elif margin > 0:
            verdict = OBSERVE
            note = f"Dry 구간 WET_DISTANCE P95={wd['p95']}가 wet_enter={we}에 근접(여유 {margin:.0f}) — 계속 관찰 필요"
        else:
            verdict = CANDIDATE
            note = (f"Dry 구간인데도 WET_DISTANCE P95={wd['p95']}가 wet_enter={we}를 초과 — "
                    f"오탐(false wet) 가능성, Dry Baseline 재확인 필요")
        candidates.append(('Rain — Dry Baseline/Threshold', verdict, note))
    else:
        candidates.append(('Rain — Dry Baseline/Threshold', OBSERVE, '이번 창에 Rain 관련 데이터 부족'))

    # Light: L0/L1 경계 bouncing 여부(짧은 시간 내 committed level 전환이 잦은지)
    committed_tr = light['committed_transitions']
    if light['row_count'] > 0:
        tr_per_hour = committed_tr / (health['window_sec'] / 3600.0)
        if tr_per_hour <= 2:
            candidates.append(('Light — Threshold/Hysteresis', NO_CHANGE,
                                f'Committed Level 전환 {committed_tr}회({tr_per_hour:.1f}/h) — 안정적'))
        elif tr_per_hour <= 6:
            candidates.append(('Light — Threshold/Hysteresis', OBSERVE,
                                f'Committed Level 전환 {committed_tr}회({tr_per_hour:.1f}/h) — 경계 부근 변동 관찰 필요'))
        else:
            candidates.append(('Light — Threshold/Hysteresis', CANDIDATE,
                                f'Committed Level 전환 {committed_tr}회({tr_per_hour:.1f}/h) — bouncing 의심, '
                                f'hysteresis/hold_ms 상향 검토 필요'))
    else:
        candidates.append(('Light — Threshold/Hysteresis', OBSERVE, '이번 창에 Light 데이터 부족'))

    # Fog: 실제 Fog(level>0)가 관측되지 않았으면 threshold 변경 권고하지 않는다(지시사항 준수)
    if not fog['ever_fog_observed']:
        candidates.append(('Fog — Score/Threshold', NO_CHANGE,
                            '이번 창에서 Fog Level>0 관측 없음 — 실제 안개 데이터 없이 threshold 변경 권고하지 않음'))
    else:
        candidates.append(('Fog — Score/Threshold', OBSERVE,
                            'Fog Level>0 관측됨 — 다음 창까지 추가 데이터로 재검토 권고'))

    # MIC: threshold 자동변경 금지 — 항상 관찰만
    if mic['rms_stats']:
        candidates.append(('MIC — Threshold', OBSERVE,
                            f"RMS P95={mic['rms_stats']['p95']} — MIC threshold 자동변경은 금지, 참고용 관찰만"))

    # AUTO Control mismatch
    ratio = auto['control_match_ratio_pct']
    if ratio is not None:
        if ratio >= 98:
            candidates.append(('AUTO Control — CONTROL_MATCH', NO_CHANGE, f'CONTROL_MATCH 비율 {ratio}% — 정상'))
        elif ratio >= 90:
            candidates.append(('AUTO Control — CONTROL_MATCH', OBSERVE,
                                f'CONTROL_MATCH 비율 {ratio}%, mismatch {auto["control_mismatch_count"]}건 — 관찰 필요'))
        else:
            candidates.append(('AUTO Control — CONTROL_MATCH', CANDIDATE,
                                f'CONTROL_MATCH 비율 {ratio}%로 낮음, mismatch {auto["control_mismatch_count"]}건 — '
                                f'원인 조사 필요(페이드 지연 vs 실제 오동작 구분 필요)'))

    if health['db_queue_drop_count']:
        candidates.append(('DB Writer — Queue Drop', OBSERVE,
                            f"db_queue_drop_count={health['db_queue_drop_count']} — DB 기록 일부 유실, "
                            f"제어에는 영향 없었으나 원인(디스크/부하) 점검 권고"))

    return candidates


# ──────────────────────────────────────────
# Markdown Report 조립
# ──────────────────────────────────────────
def build_report_md(start, end, health, light, rain, fog, mic, auto, markers, candidates):
    L = []
    L.append('# LANEON ELMS Environment Analysis Report')
    L.append('')
    L.append('## 1. 분석시간')
    L.append(f"- 시작: {start.strftime(TS_FMT)} KST")
    L.append(f"- 종료: {end.strftime(TS_FMT)} KST")
    L.append(f"- 창 길이: {health['window_sec']/3600:.2f} 시간")
    L.append('')

    L.append('## 2. System / Data Health')
    L.append(f"- 총 JSB packet 수: {health['actual_packets']} / 기대치 {health['expected_packets']} "
              f"(수신율 {health['reception_rate_pct']}%)")
    L.append(f"- SEQ gap 총합: {health['seq_gap_total']}")
    L.append(f"- chunk_error_count 증가량: {health['chunk_error_count_delta']}")
    L.append(f"- incomplete_group_count 증가량: {health['incomplete_group_count_delta']}")
    L.append(f"- GPS valid 비율: {health['gps_valid_ratio_pct']}%")
    L.append(f"- BME valid 비율: {health['bme_valid_ratio_pct']}%")
    L.append(f"- TCS valid 비율: {health['tcs_valid_ratio_pct']}%")
    L.append(f"- NCV sample 총합: {health['ncv_sample_total']}")
    L.append(f"- MIC sample 총합: {health['mic_sample_total']}")
    L.append(f"- JPB Status(0x30) 수신 행 수: {health['jpb_status_rows']}")
    L.append(f"- db_queue_drop_count: {health['db_queue_drop_count']}")
    if health['db_gaps']:
        L.append(f"- DB 기록 누락 구간 ({len(health['db_gaps'])}건):")
        for g_start, g_end, dur in health['db_gaps'][:20]:
            L.append(f"  - {g_start.strftime(TS_FMT)} ~ {g_end.strftime(TS_FMT)} ({dur:.0f}초)")
    else:
        L.append("- DB 기록 누락 구간: 없음")
    L.append('')

    L.append('## 3. Light')
    L.append(f"- LS2 RAW: {fmt_stats(light['raw_stats'])}")
    L.append(f"- LS2 EMA(filtered): {fmt_stats(light['filtered_stats'])}")
    L.append(f"- RAW-EMA 차이: {fmt_stats(light['raw_vs_ema_diff_stats'])}")
    L.append(f"- 현재 Threshold(env_config.json): L0/1={light['thresholds']['l0_l1']}, "
              f"L1/2={light['thresholds']['l1_l2']}, L2/3={light['thresholds']['l2_l3']}, "
              f"L3/4={light['thresholds']['l3_l4']}, hysteresis={light['thresholds']['hysteresis']}, "
              f"hold_ms={light['thresholds']['hold_ms']}")
    L.append(f"- Level 체류시간(초): " + ', '.join(f"L{k}={v:.0f}" for k, v in sorted(light['dwell_sec'].items())))
    L.append(f"- Target 전환 횟수: {light['target_transitions']}, Committed Level 전환 횟수: {light['committed_transitions']}")
    L.append('')

    L.append('## 4. Rain')
    L.append(f"- IR11: {fmt_stats(rain['ir11_stats'])}")
    L.append(f"- IR12: {fmt_stats(rain['ir12_stats'])}")
    L.append(f"- IR21: {fmt_stats(rain['ir21_stats'])}")
    L.append(f"- IR22: {fmt_stats(rain['ir22_stats'])}")
    L.append(f"- 현재 Dry Baseline(env_config.json): {rain['baseline']}")
    L.append(f"- WET_DISTANCE (전체): {fmt_stats(rain['wet_distance_stats'])}")
    L.append(f"- WET_DISTANCE (Dry 추정구간, rain_level=0): {fmt_stats(rain['wet_distance_dry_stats'])}")
    L.append(f"  - wet_exit={rain['wet_exit_threshold']}, wet_enter={rain['wet_enter_threshold']}")
    L.append(f"- WET_PRESENT 비율: {rain['wet_present_ratio_pct']}%")
    L.append(f"- EVENT_MAG: {fmt_stats(rain['event_mag_stats'])}")
    L.append(f"- EVENT_COUNT(window): {fmt_stats(rain['event_count_window_stats'])}")
    L.append(f"- EVENT_STATE 분포: {rain['event_state_counts']}")
    L.append(f"- Target 전환 횟수: {rain['target_transitions']}, Committed Level 전환 횟수: {rain['committed_transitions']}")
    L.append("- 참고: 현재 Dry Baseline은 랩 임시값이며, 실제 현장 설치 후 완전 Dry 조건에서 재보정 대상입니다.")
    L.append('')

    L.append('## 5. Fog')
    L.append(f"- BME RH: {fmt_stats(fog['rh_stats'])} %")
    L.append(f"- HUMID_READY(Gate) 비율: {fog['humid_ready_ratio_pct']}% "
              f"(열림 {fog['gate_open_sec']:.0f}s / 닫힘 {fog['gate_closed_sec']:.0f}s)")
    L.append(f"- RS_MEAN: {fmt_stats(fog['rs_mean_stats'])}")
    L.append(f"- RS_VARIATION: {fmt_stats(fog['rs_variation_stats'])}")
    L.append(f"- RS_IMPULSE_RATIO: {fmt_stats(fog['rs_impulse_ratio_stats'])}")
    L.append(f"- RS_PERSISTENCE: {fmt_stats(fog['rs_persistence_stats'])}")
    L.append(f"- MIC_STATE 분포: {fog['mic_state_counts']}")
    L.append(f"- FOG_SCORE: {fmt_stats(fog['fog_score_stats'])}")
    L.append(f"- Target 전환 횟수: {fog['target_transitions']}, Committed Level 전환 횟수: {fog['committed_transitions']}")
    L.append(f"- 실제 Fog(Level>0) 관측 여부: {'있음' if fog['ever_fog_observed'] else '없음'}")
    L.append('')

    L.append('## 6. MIC')
    L.append(f"- RMS: {fmt_stats(mic['rms_stats'])} (P99={mic['rms_stats']['p99'] if mic['rms_stats'] else '-'})")
    L.append(f"- PEAK: {fmt_stats(mic['peak_stats'])} (P99={mic['peak_stats']['p99'] if mic['peak_stats'] else '-'})")
    L.append(f"- MIC_STATE 분포: {mic['mic_state_counts']}")
    L.append(f"- 표본 수: {mic['sample_count']} (MIC0=t-500ms, MIC1=t 시간순 샘플)")
    if mic['top_rms_spikes']:
        L.append("- 상위 RMS 스파이크:")
        for ts, v in mic['top_rms_spikes']:
            L.append(f"  - {ts}  RMS={v}")
    L.append('')

    L.append('## 7. AUTO Control')
    L.append(f"- AUTO 모드 유지 행 수: {auto['auto_mode_rows']}")
    L.append(f"- CONTROL_MATCH 비율: {auto['control_match_ratio_pct']}%")
    L.append(f"- CONTROL_MISMATCH 횟수: {auto['control_mismatch_count']}")
    if auto['mismatch_durations_sec']:
        L.append(f"- MISMATCH 지속시간(초): {[round(d,1) for d in auto['mismatch_durations_sec']]}")
    L.append(f"- 실제 전송된 AUTO 제어 명령 수: {auto['auto_command_count']} "
              f"(ON전환 {auto['on_transitions']}, OFF전환 {auto['off_transitions']}, "
              f"Brightness변경 {auto['brightness_change_count']})")
    L.append(f"- MODE 전환 횟수: {auto['mode_change_count']}")
    for lane, d in auto['per_lane'].items():
        L.append(f"- Lane{lane}: ON비율={d['active_on_ratio_pct']}%, Brightness 전환={d['bright_transitions']}회, "
                  f"전압={fmt_stats(d['voltage_stats'])}, 전류={fmt_stats(d['current_stats'])}")
    L.append('')

    L.append('## 8. Test Marker / Environment Events')
    if markers:
        for m in markers:
            ctx = m['env_at_marker']
            ctx_str = f"(당시 L={ctx['light_level']} R={ctx['rain_level']} F={ctx['fog_level']})" if ctx else '(당시 센서 데이터 없음)'
            L.append(f"- {m['ts']}  **{m['label']}**  {m['memo']}  {ctx_str}")
    else:
        L.append("- 이번 창에 등록된 Test Marker 없음")
    L.append('')

    L.append('## 9. 이상사항')
    anomalies = []
    if health['reception_rate_pct'] is not None and health['reception_rate_pct'] < 95:
        anomalies.append(f"JSB packet 수신율 낮음: {health['reception_rate_pct']}%")
    if health['db_gaps']:
        anomalies.append(f"DB 기록 누락 구간 {len(health['db_gaps'])}건 발생")
    if health['db_queue_drop_count']:
        anomalies.append(f"db_queue_drop_count={health['db_queue_drop_count']} (DB 기록 일부 유실, 제어는 정상)")
    if auto['control_mismatch_count']:
        anomalies.append(f"AUTO CONTROL_MISMATCH {auto['control_mismatch_count']}건")
    if anomalies:
        for a in anomalies:
            L.append(f"- {a}")
    else:
        L.append("- 특이사항 없음")
    L.append('')

    L.append('## 10. Tuning Candidate')
    for name, verdict, note in candidates:
        L.append(f"- **{name}** — `{verdict}`")
        L.append(f"  - {note}")
    L.append('')
    L.append("> 본 Report는 튜닝 후보만 제안합니다. env_config.json/AUTO 정책/Firmware는 "
              "이 Report만으로 자동 변경되지 않으며, 사용자 승인 후 별도로 수정합니다.")
    L.append('')

    L.append('## 11. 다음 6시간 관찰 권고')
    recs = []
    if not fog['ever_fog_observed']:
        recs.append("Fog 관련 실제 데이터가 아직 없습니다 — 안개/고습 조건에서의 데이터 확보를 권고합니다.")
    if rain['wet_present_ratio_pct'] and rain['wet_present_ratio_pct'] > 5 and not markers:
        recs.append("WET_PRESENT 비율이 낮지 않은데 Test Marker가 없습니다 — 실제 강우였는지 Dry 오탐인지 "
                     "확인할 수 있도록 다음 관찰 구간에는 Test Marker를 남겨주세요.")
    if not recs:
        recs.append("특이사항 없음 — 동일 조건 유지하며 계속 관찰합니다.")
    for r in recs:
        L.append(f"- {r}")
    L.append('')

    return '\n'.join(L)


# ──────────────────────────────────────────
# main
# ──────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='LANEON PoC 6시간 Environment Analyzer')
    parser.add_argument('--hours', type=float, default=None, help='지금부터 N시간 전까지 분석')
    parser.add_argument('--since', type=str, default=None, help='"YYYY-MM-DD HH:MM:SS" 시작')
    parser.add_argument('--until', type=str, default=None, help='"YYYY-MM-DD HH:MM:SS" 종료')
    parser.add_argument('--no-email', action='store_true', help='이메일 발송 생략(테스트용)')
    args = parser.parse_args()

    start, end = resolve_window(args)
    print(f'[env_reporter] 분석 구간: {start.strftime(TS_FMT)} ~ {end.strftime(TS_FMT)}')

    env_cfg = load_json(ENV_CFG_PATH)
    report_cfg = load_json(REPORT_CFG_PATH, default={'report_dir': 'reports/environment'})

    conn = open_ro_conn(DB_PATH)
    try:
        # db_queue_drop_count는 실행 중인 app.py 프로세스의 인메모리 카운터라 여기서는
        # 직접 읽을 수 없다 — /api/monitor를 통해 조회 시도하고, 실패하면 알 수 없음 처리.
        db_queue_drop_count = fetch_drop_count_best_effort()

        health = analyze_health(conn, start, end, db_queue_drop_count)
        light  = analyze_light(conn, start, end, env_cfg)
        rain   = analyze_rain(conn, start, end, env_cfg)
        fog    = analyze_fog(conn, start, end, env_cfg)
        mic    = analyze_mic(conn, start, end)
        auto   = analyze_auto_control(conn, start, end)
        markers = analyze_markers(conn, start, end)
    finally:
        conn.close()

    candidates = tuning_candidates(health, light, rain, fog, mic, auto, markers)
    report_md = build_report_md(start, end, health, light, rain, fog, mic, auto, markers, candidates)

    report_dir = os.path.join(BASE_DIR, '..', report_cfg.get('report_dir', 'reports/environment'))
    report_dir = os.path.normpath(report_dir)
    os.makedirs(report_dir, exist_ok=True)
    fname = f"{start.strftime('%Y-%m-%d_%H%M')}-{end.strftime('%H%M')}.md"
    fpath = os.path.join(report_dir, fname)
    with open(fpath, 'w', encoding='utf-8') as f:
        f.write(report_md)
    print(f'[env_reporter] Report 생성: {fpath}')

    email_status = 'SKIPPED'
    if not args.no_email:
        try:
            import report_mailer
            subject = f"[LANEON ELMS] Environment Report {start.strftime('%Y-%m-%d %H:%M')}~{end.strftime('%H:%M')}"
            summary = build_email_summary(health, light, rain, fog, auto, candidates)
            ok = report_mailer.send_report(subject, summary, report_md, fpath)
            email_status = 'SENT' if ok else 'FAILED'
        except Exception as e:
            print(f'[env_reporter] 이메일 발송 실패(리포트 생성/DB는 정상 완료됨): {e}', file=sys.stderr)
            email_status = 'FAILED'
    print(f'[env_reporter] email_status={email_status}')

    return fpath, email_status


def fetch_drop_count_best_effort():
    try:
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:5000/api/monitor', timeout=2) as r:
            data = json.load(r)
        return data.get('auto', {}).get('db_queue_drop_count')
    except Exception:
        return None


def build_email_summary(health, light, rain, fog, auto, candidates):
    lines = [
        f"수신율: {health['reception_rate_pct']}% ({health['actual_packets']}/{health['expected_packets']})",
        f"Light Committed 전환: {light['committed_transitions']}회",
        f"Rain Level 전환: {rain['committed_transitions']}회, WET_PRESENT 비율: {rain['wet_present_ratio_pct']}%",
        f"Fog 관측: {'있음' if fog['ever_fog_observed'] else '없음'}",
        f"AUTO CONTROL_MATCH 비율: {auto['control_match_ratio_pct']}%",
        '',
        'Tuning Candidate 요약:',
    ]
    for name, verdict, _ in candidates:
        lines.append(f"  - {name}: {verdict}")
    return '\n'.join(lines)


if __name__ == '__main__':
    main()

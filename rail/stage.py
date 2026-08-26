"""리니어 스테이지(볼스크류 + 스텝모터) mm 단위 제어.

하드웨어:
    스테이지   LSM4-NK235630x-1610  (볼스크류 φ16, 리드 10mm)
    드라이버   MSD-224              (분주 1/8, 전류 2.8~3.0A)
    모터       1.8도 2상, 3A/phase
    전원       24V 5A (HT-AD036)


펄스는 pigpio의 wave API로 만든다. set_PWM_frequency()는 요청 주파수를
샘플레이트 기준 이산값으로 스냅시켜서, 요청값으로 계산한 시간만큼 기다리면
실제로 나간 펄스 수가 어긋난다. 왕복할수록 오차가 누적되므로 위치 제어에는
쓰지 않는다. wave는 보낼 펄스 수를 직접 지정하므로 스텝 수가 보장된다.

리미트스위치가 없으므로 끝단은 소프트웨어로만 막는다. 위치를 신뢰할 수 없게
되면(이동 중 중단 등) 이동을 거부하고 set_position_mm() 재선언을 요구한다.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pigpio

# ===== 설정값 (본인 환경에 맞게 수정) =====
PUL_PIN = 22
DIR_PIN = 27
ENA_PIN = 17          # ENA 안 쓰면 None 으로

LEAD_MM     = 10      # 모델 -1610 = 리드 10mm (1605=5, 1610=10, 1620=20, 1204=4)
MICROSTEP   = 8       # MSD-224 DIP S1~S3 = 1/8 (1600 pulse/rev)
STEPS_PER_REV = 200   # 1.8도 모터 = 200스텝 (고정)

INVERT_DIR  = False   # 방향 반대면 True

# --- 소프트 리밋 (리미트스위치 없음) ---
# ⚠️ STROKE_MM 은 실제 유효 스트로크로 반드시 맞출 것. 이 값보다 크면 소프트
#    리밋이 캐리지의 끝단 충돌을 막지 못한다.
STROKE_MM         = 100.0          # 스테이지 유효 스트로크
START_POSITION_MM = STROKE_MM / 2  # 전원 인가 시 캐리지를 중앙에 두고 시작한다고 가정

# 모델별 매뉴얼 상한: 1605=50, 1610=100, 1620=200, 1204=40 (mm/s)
# 이 값을 넘기면 탈조(스텝 씹힘)로 위치를 잃는다.
MAX_SPEED_MM_S = 100.0

MAX_PULSE_HZ = 100000   # 드라이버 한계(127kHz) 아래로 제한
MIN_PULSE_HZ = 100

# --- 보정 ---
# 실측한 스트로크와 마지막 위치를 여기 저장한다. 서버를 껐다 켜도 남는다.
CALIBRATION_PATH = Path(__file__).with_name("calibration.json")

# 보정 중에는 소프트 리밋이 없다(스트로크를 아직 모르니까). 대신 한 번에
# 움직일 수 있는 거리와 누적 거리를 제한해 폭주를 막는다.
MAX_JOG_MM = 20.0
CALIB_MAX_TRAVEL_MM = 2000.0
MIN_STROKE_MM = 1.0     # 이보다 짧으면 실수로 본다
# =========================================

STEPS_PER_MM = (STEPS_PER_REV * MICROSTEP) / LEAD_MM

# 보정값이 있으면 STROKE_MM 을 실측치로 덮어쓴다. 아래 _load_calibration() 참고.
_position_mm = START_POSITION_MM

# 임포트 시점에는 캐리지가 어디 있는지 알 수 없다. 전원이 꺼진 사이 손으로
# 밀렸을 수도 있으므로, 저장된 위치가 있어도 사용자가 확인해 주기 전까지는
# 모르는 것으로 취급한다. resume_position() 또는 set_position_mm() 이 필요하다.
_position_known = False

# 보정 모드 상태. 사용자가 begin_calibration() 을 부를 때만 켜진다.
_calibrating = False
_calib_travel_mm = 0.0      # 누적 이동 거리(폭주 감지용, 절대값 합)
_calibration = None         # 파일에서 읽은 보정 정보(없으면 None)


class SoftLimitError(RuntimeError):
    """소프트 리밋을 벗어나거나, 현재 위치를 신뢰할 수 없는 상태."""


pi = pigpio.pi()
if not pi.connected:
    raise RuntimeError("pigpiod 안 켜져 있음. sudo systemctl start pigpiod")

pi.set_mode(PUL_PIN, pigpio.OUTPUT)
pi.set_mode(DIR_PIN, pigpio.OUTPUT)
pi.write(PUL_PIN, 0)
if ENA_PIN is not None:
    pi.set_mode(ENA_PIN, pigpio.OUTPUT)
    pi.write(ENA_PIN, 0)      # 0 = 모터 활성 유지


def _save_calibration():
    """스트로크와 현재 위치를 파일에 쓴다. 쓰기 실패는 치명적이지 않다."""
    data = {
        "stroke_mm": round(STROKE_MM, 4),
        # 분주나 리드를 바꾸면 이전 보정값의 mm 환산이 달라진다. 같이 적어
        # 두고 불일치하면 무시한다.
        "steps_per_mm": round(STEPS_PER_MM, 6),
        "position_mm": round(_position_mm, 4),
        "calibrated_at": _calibration.get("calibrated_at") if _calibration else None,
        "saved_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    try:
        # 쓰다 죽어도 기존 파일이 깨지지 않도록 임시 파일에 쓰고 바꿔치기한다.
        tmp = CALIBRATION_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, CALIBRATION_PATH)
    except OSError:
        pass
    return data


def _load_calibration():
    """저장된 보정값을 읽어 STROKE_MM 과 마지막 위치에 반영한다.

    위치는 불러오되 _position_known 은 False 로 둔다. 전원이 꺼진 사이
    캐리지를 손으로 밀 수 있으므로, 저장돼 있다는 것만으로는 신뢰할 수 없다.
    """
    global STROKE_MM, _position_mm, _calibration

    try:
        data = json.loads(CALIBRATION_PATH.read_text())
    except (OSError, ValueError):
        return None

    saved_spm = data.get("steps_per_mm")
    if saved_spm is not None and abs(saved_spm - STEPS_PER_MM) > 1e-6:
        # LEAD_MM / MICROSTEP 을 바꾼 경우. mm 값이 더 이상 같은 뜻이 아니다.
        print(
            f"[stage] 보정값 무시: steps_per_mm 불일치 "
            f"(저장 {saved_spm} vs 현재 {STEPS_PER_MM}). 다시 보정하세요."
        )
        return None

    stroke = data.get("stroke_mm")
    if isinstance(stroke, (int, float)) and stroke >= MIN_STROKE_MM:
        STROKE_MM = float(stroke)

    pos = data.get("position_mm")
    if isinstance(pos, (int, float)) and 0.0 <= pos <= STROKE_MM:
        _position_mm = float(pos)

    _calibration = data
    return data


def calibration():
    """현재 보정 상태. 서버가 그대로 내보낸다."""
    return {
        "stroke_mm": STROKE_MM,
        "steps_per_mm": STEPS_PER_MM,
        # 보정을 실제로 마친 적이 있는가. 파일만 있고 calibrated_at 이 없으면
        # 위치만 저장된 것이므로 스트로크는 여전히 기본값이다.
        "calibrated": bool(_calibration and _calibration.get("calibrated_at")),
        "calibrated_at": _calibration.get("calibrated_at") if _calibration else None,
        "calibrating": _calibrating,
        "calibration_travel_mm": round(_calib_travel_mm, 3) if _calibrating else None,
        "position_mm": round(_position_mm, 3),
        "position_known": _position_known,
    }


def position_mm():
    """현재 추정 위치(mm)."""
    return _position_mm


def set_position_mm(value):
    """캐리지를 손으로 옮겼거나 위치를 잃었을 때 현재 위치를 다시 알려준다."""
    global _position_mm, _position_known
    value = float(value)
    if not 0.0 <= value <= STROKE_MM:
        raise SoftLimitError(
            f"{value:.2f}mm 는 허용 범위(0.00~{STROKE_MM:.2f}mm) 밖입니다."
        )
    _position_mm = value
    _position_known = True
    _save_calibration()
    return _position_mm


def resume_position():
    """저장된 위치를 그대로 쓴다 — 전원이 꺼진 사이 캐리지를 건드리지 않았을 때.

    서버를 다시 띄우면 위치는 항상 '모름'으로 시작한다. 캐리지를 손대지
    않았다면 이걸 불러 그대로 이어 쓴다. 손댔다면 set_position_mm() 을 쓴다.
    """
    global _position_known
    if _calibration is None:
        raise SoftLimitError(
            "저장된 위치가 없습니다. 보정을 하거나 set_position_mm()으로 알려주세요."
        )
    _position_known = True
    return _position_mm


# ===== 보정 =====
# 리미트 스위치가 없어서 원점을 기계적으로 찾을 수 없다. 대신 사용자가 양 끝을
# 직접 지정한다. 보정 중에는 절대 위치가 필요 없다 — 상대 이동만 쓰기 때문이다.


def begin_calibration():
    """보정 모드 시작. 지금 캐리지가 있는 자리를 기준점(0)으로 잡는다."""
    global _calibrating, _calib_travel_mm, _position_mm, _position_known
    if _calibrating:
        raise SoftLimitError("이미 보정 중입니다.")
    _calibrating = True
    _calib_travel_mm = 0.0
    _position_mm = 0.0
    _position_known = True   # 보정 안에서만 통하는 상대 기준
    return calibration()


def cancel_calibration():
    """보정을 버린다. 위치는 다시 '모름'이 된다."""
    global _calibrating, _calib_travel_mm, _position_known
    if not _calibrating:
        raise SoftLimitError("보정 중이 아닙니다.")
    _calibrating = False
    _calib_travel_mm = 0.0
    _position_known = False
    return calibration()


def end_calibration():
    """지금 자리를 반대쪽 끝으로 확정하고 스트로크를 저장한다.

    시작점에서의 순수 변위가 스트로크가 된다. 뒤로 갔다면(변위가 음수) 지금
    자리가 더 낮은 쪽이므로 그쪽을 0 으로 삼는다. 즉 0 은 항상 두 지점 중
    좌표가 낮은 쪽이고, mm 는 정방향으로 증가한다.
    """
    global STROKE_MM, _calibrating, _calib_travel_mm, _position_mm, _calibration

    if not _calibrating:
        raise SoftLimitError("보정 중이 아닙니다. begin_calibration() 부터 부르세요.")

    net = _position_mm
    stroke = abs(net)
    if stroke < MIN_STROKE_MM:
        raise SoftLimitError(
            f"이동 거리가 {stroke:.2f}mm 뿐입니다. 두 지점이 같은 자리인지 "
            "확인하세요. (취소하려면 cancel_calibration())"
        )

    STROKE_MM = stroke
    _position_mm = stroke if net > 0 else 0.0
    _calibrating = False
    _calib_travel_mm = 0.0
    _calibration = {
        "calibrated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    _save_calibration()
    return calibration()


def jog_mm(distance_mm, speed_mm_s=5.0):
    """보정용 상대 이동. 절대 기준이 없어도 동작한다.

    보정 중에는 소프트 리밋을 적용하지 않는다(스트로크를 아직 모른다).
    대신 한 번에 MAX_JOG_MM, 누적 CALIB_MAX_TRAVEL_MM 로 제한한다.
    보정 중이 아니면 그냥 move_mm() 과 같다.
    """
    global _calib_travel_mm

    if abs(distance_mm) > MAX_JOG_MM:
        raise SoftLimitError(
            f"한 번에 {MAX_JOG_MM}mm 까지만 움직일 수 있습니다 "
            f"(요청 {distance_mm}mm). 나눠서 보내세요."
        )
    if not _calibrating:
        return move_mm(distance_mm, speed_mm_s=speed_mm_s)

    if _calib_travel_mm + abs(distance_mm) > CALIB_MAX_TRAVEL_MM:
        raise SoftLimitError(
            f"보정 중 누적 이동이 {CALIB_MAX_TRAVEL_MM}mm 를 넘습니다. "
            "끝단을 지나쳤을 수 있으니 확인하고 다시 시작하세요."
        )

    _move_raw(distance_mm, speed_mm_s)
    _calib_travel_mm += abs(distance_mm)
    return _position_mm


def _send_pulses(steps, half_period_us):
    """정확히 steps개의 펄스를 전송하고 완료까지 기다린다."""
    pi.wave_clear()
    pi.wave_add_generic([
        pigpio.pulse(1 << PUL_PIN, 0, half_period_us),
        pigpio.pulse(0, 1 << PUL_PIN, half_period_us),
    ])
    wid = pi.wave_create()
    try:
        remaining = steps
        while remaining > 0:
            # wave_chain의 루프 반복 횟수는 16비트까지만 지정할 수 있다
            chunk = min(remaining, 65535)
            pi.wave_chain([255, 0, wid, 255, 1, chunk & 0xFF, chunk >> 8])
            while pi.wave_tx_busy():
                time.sleep(0.001)
            remaining -= chunk
    finally:
        pi.wave_delete(wid)


def _step_distance(distance_mm):
    """요청 거리를 실제로 나갈 펄스 수와 그에 해당하는 거리로 바꾼다.

    스텝은 정수라서 요청한 거리와 실제 이동 거리가 미세하게 다르다. 위치는
    요청값이 아니라 이 값으로 누적해야 왕복 시 오차가 쌓이지 않는다.
    """
    steps = int(round(abs(distance_mm) * STEPS_PER_MM))
    actual_mm = (steps / STEPS_PER_MM) * (1 if distance_mm > 0 else -1)
    return steps, actual_mm


def _move_raw(distance_mm, speed_mm_s):
    """펄스를 실제로 내보낸다. 소프트 리밋과 위치 신뢰 여부는 보지 않는다.

    보정 중에는 스트로크를 모르므로 리밋을 걸 수 없다. 검사는 호출자가 한다.
    """
    global _position_mm, _position_known

    if speed_mm_s > MAX_SPEED_MM_S:
        raise SoftLimitError(
            f"속도 {speed_mm_s}mm/s 는 상한 {MAX_SPEED_MM_S}mm/s 를 넘습니다. "
            "탈조로 위치를 잃습니다."
        )

    steps, actual_mm = _step_distance(distance_mm)
    if steps == 0:
        return _position_mm

    forward = distance_mm > 0
    if INVERT_DIR:
        forward = not forward
    pi.write(DIR_PIN, 1 if forward else 0)
    time.sleep(0.005)   # 방향 신호 안정화 대기

    freq = speed_mm_s * STEPS_PER_MM
    freq = max(MIN_PULSE_HZ, min(freq, MAX_PULSE_HZ))
    half_period_us = max(1, int(round(1000000 / (2 * freq))))

    try:
        _send_pulses(steps, half_period_us)
    except BaseException:
        # 중간에 끊기면 몇 펄스가 나갔는지 알 수 없다
        pi.wave_tx_stop()
        _position_known = False
        raise

    _position_mm += actual_mm
    time.sleep(0.05)
    return _position_mm


def move_mm(distance_mm, speed_mm_s=10.0):
    """distance_mm: +는 정방향, -는 역방향 / speed_mm_s: 이동 속도"""
    if _calibrating:
        raise SoftLimitError(
            "보정 중입니다. jog_mm() 으로 움직이거나 보정을 끝내세요."
        )

    if not _position_known:
        raise SoftLimitError(
            "현재 위치를 알 수 없음. 캐리지 위치를 눈으로 확인한 뒤 "
            "set_position_mm()으로 알려주세요. (건드리지 않았다면 resume_position())"
        )

    steps, actual_mm = _step_distance(distance_mm)
    if steps == 0:
        return _position_mm

    target_mm = _position_mm + actual_mm
    if not 0.0 <= target_mm <= STROKE_MM:
        raise SoftLimitError(
            f"이동 거부: {_position_mm:.2f}mm → {target_mm:.2f}mm "
            f"(허용 0.00~{STROKE_MM:.2f}mm)"
        )

    _move_raw(distance_mm, speed_mm_s)
    # 다음 부팅에서 이어 쓸 수 있도록 남긴다. 신뢰 여부는 그때 다시 묻는다.
    _save_calibration()
    return _position_mm


def move_to_mm(target_mm, speed_mm_s=10.0):
    """절대 위치로 이동."""
    move_mm(target_mm - _position_mm, speed_mm_s=speed_mm_s)


def cleanup():
    if _position_known:
        _save_calibration()
    pi.wave_tx_stop()
    pi.wave_clear()
    pi.write(PUL_PIN, 0)
    pi.stop()


# 저장된 보정값이 있으면 STROKE_MM 과 마지막 위치를 여기서 되살린다.
# 위치는 불러오기만 하고 신뢰하지는 않는다(_position_known 은 False 그대로).
_load_calibration()


if __name__ == "__main__":
    print(f"1mm당 {STEPS_PER_MM:.1f} 스텝 / 현재 위치 {position_mm():.2f}mm")

    try:
        print("정방향 10mm")
        move_mm(10, speed_mm_s=5)
        print(f"  위치 {position_mm():.2f}mm")
        time.sleep(1)

        print("역방향 10mm (원위치)")
        move_mm(-10, speed_mm_s=5)
        print(f"  위치 {position_mm():.2f}mm")

    except SoftLimitError as e:
        print(f"소프트 리밋: {e}")
    except KeyboardInterrupt:
        print("중단 - 캐리지 위치를 확인하고 set_position_mm()으로 다시 알려주세요")
    finally:
        cleanup()

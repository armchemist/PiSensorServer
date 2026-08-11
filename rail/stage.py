"""리니어 스테이지(볼스크류 + 스텝모터) mm 단위 제어.

펄스는 pigpio의 wave API로 만든다. set_PWM_frequency()는 요청 주파수를
샘플레이트 기준 이산값으로 스냅시켜서, 요청값으로 계산한 시간만큼 기다리면
실제로 나간 펄스 수가 어긋난다. 왕복할수록 오차가 누적되므로 위치 제어에는
쓰지 않는다. wave는 보낼 펄스 수를 직접 지정하므로 스텝 수가 보장된다.

리미트스위치가 없으므로 끝단은 소프트웨어로만 막는다. 위치를 신뢰할 수 없게
되면(이동 중 중단 등) 이동을 거부하고 set_position_mm() 재선언을 요구한다.
"""

import pigpio
import time

# ===== 설정값 (본인 환경에 맞게 수정) =====
PUL_PIN = 22
DIR_PIN = 27
ENA_PIN = 17          # ENA 안 쓰면 None 으로

LEAD_MM     = 5       # 리드 (1605=5, 1610=10, 1620=20, 1204=4)
MICROSTEP   = 8       # 드라이버 분주 설정
STEPS_PER_REV = 200   # 1.8도 모터 = 200스텝 (고정)

INVERT_DIR  = False   # 방향 반대면 True

# --- 소프트 리밋 (리미트스위치 없음) ---
STROKE_MM         = 100.0          # 스테이지 유효 스트로크
START_POSITION_MM = STROKE_MM / 2  # 전원 인가 시 캐리지를 중앙에 두고 시작한다고 가정

MAX_PULSE_HZ = 100000   # 드라이버 한계(127kHz) 아래로 제한
MIN_PULSE_HZ = 100
# =========================================

STEPS_PER_MM = (STEPS_PER_REV * MICROSTEP) / LEAD_MM

_position_mm = START_POSITION_MM
_position_known = True


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


def position_mm():
    """현재 추정 위치(mm)."""
    return _position_mm


def set_position_mm(value):
    """캐리지를 손으로 옮겼거나 위치를 잃었을 때 현재 위치를 다시 알려준다."""
    global _position_mm, _position_known
    _position_mm = float(value)
    _position_known = True


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


def move_mm(distance_mm, speed_mm_s=10.0):
    """distance_mm: +는 정방향, -는 역방향 / speed_mm_s: 이동 속도"""
    global _position_mm, _position_known

    if not _position_known:
        raise SoftLimitError(
            "현재 위치를 알 수 없음. 캐리지 위치를 눈으로 확인한 뒤 "
            "set_position_mm()으로 알려주세요."
        )

    steps = int(round(abs(distance_mm) * STEPS_PER_MM))
    if steps == 0:
        return

    # 스텝은 정수이므로 요청 거리가 아니라 실제로 나갈 펄스 수로 위치를 계산한다
    actual_mm = (steps / STEPS_PER_MM) * (1 if distance_mm > 0 else -1)
    target_mm = _position_mm + actual_mm
    if not 0.0 <= target_mm <= STROKE_MM:
        raise SoftLimitError(
            f"이동 거부: {_position_mm:.2f}mm → {target_mm:.2f}mm "
            f"(허용 0.00~{STROKE_MM:.2f}mm)"
        )

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

    _position_mm = target_mm
    time.sleep(0.05)


def move_to_mm(target_mm, speed_mm_s=10.0):
    """절대 위치로 이동."""
    move_mm(target_mm - _position_mm, speed_mm_s=speed_mm_s)


def cleanup():
    pi.wave_tx_stop()
    pi.wave_clear()
    pi.write(PUL_PIN, 0)
    pi.stop()


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

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
# =========================================

STEPS_PER_MM = (STEPS_PER_REV * MICROSTEP) / LEAD_MM

pi = pigpio.pi()
if not pi.connected:
    raise RuntimeError("pigpiod 안 켜져 있음. sudo systemctl start pigpiod")

pi.set_mode(PUL_PIN, pigpio.OUTPUT)
pi.set_mode(DIR_PIN, pigpio.OUTPUT)
if ENA_PIN is not None:
    pi.set_mode(ENA_PIN, pigpio.OUTPUT)
    pi.write(ENA_PIN, 0)      # 0 = 모터 활성 유지

def move_mm(distance_mm, speed_mm_s=10.0):
    """distance_mm: +는 정방향, -는 역방향 / speed_mm_s: 이동 속도"""
    steps = int(round(abs(distance_mm) * STEPS_PER_MM))
    if steps == 0:
        return

    forward = distance_mm > 0
    if INVERT_DIR:
        forward = not forward
    pi.write(DIR_PIN, 1 if forward else 0)
    time.sleep(0.005)   # 방향 신호 안정화 대기

    # 속도 → 주파수 계산
    freq = speed_mm_s * STEPS_PER_MM
    freq = max(100, min(freq, 100000))   # 드라이버 한계(127kHz) 아래로 제한

    # pigpio 하드웨어 PWM으로 정확한 펄스 생성
    pi.set_PWM_frequency(PUL_PIN, int(freq))
    pi.set_PWM_dutycycle(PUL_PIN, 128)   # 50% duty

    duration = steps / freq
    time.sleep(duration)

    pi.set_PWM_dutycycle(PUL_PIN, 0)     # 정지
    time.sleep(0.05)

if __name__ == "__main__":
    print(f"1mm당 {STEPS_PER_MM:.1f} 스텝")

    try:
        print("정방향 10mm")
        move_mm(10, speed_mm_s=5)
        time.sleep(1)

        print("역방향 10mm (원위치)")
        move_mm(-10, speed_mm_s=5)

    except KeyboardInterrupt:
        pi.set_PWM_dutycycle(PUL_PIN, 0)
        print("중단")
    finally:
        pi.set_PWM_dutycycle(PUL_PIN, 0)
        pi.stop()

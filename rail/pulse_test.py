"""배선 확인용 최소 테스트 — 200펄스(= 1.25mm)만 보낸다.

stage.py 를 쓰기 전에 배선과 방향을 확인하는 용도다. 보정도 소프트 리밋도
없으므로 **캐리지를 양쪽으로 여유 있는 자리에 두고** 실행할 것.

    python3 pulse_test.py          정방향
    python3 pulse_test.py -        역방향
"""

import sys
import time

import lgpio

PUL = 22
DIR = 27
ENA = 17          # 안 쓰면 None

CHIP = 0
STEPS = 200       # 1/8 분주 · 리드 10mm 기준 약 1.25mm
HALF_US = 500     # 1kHz → 6.25mm/s

forward = "-" not in sys.argv[1:]

h = lgpio.gpiochip_open(CHIP)
lgpio.gpio_claim_output(h, PUL, 0)
lgpio.gpio_claim_output(h, DIR, 0)
if ENA is not None:
    lgpio.gpio_claim_output(h, ENA, 0)     # 0 = 모터 활성

try:
    lgpio.gpio_write(h, DIR, 1 if forward else 0)
    time.sleep(0.005)                       # 방향 신호 안정화

    print(f"{'정' if forward else '역'}방향 {STEPS}펄스 (약 1.25mm)")
    # pulse_cycles 로 개수를 지정하므로 정확히 STEPS 개만 나간다.
    lgpio.tx_pulse(h, PUL, HALF_US, HALF_US, pulse_offset=0, pulse_cycles=STEPS)

    expected = STEPS * 2 * HALF_US / 1_000_000
    time.sleep(expected * 0.9)
    while lgpio.tx_busy(h, PUL, lgpio.TX_PWM):
        time.sleep(0.001)
    print(f"완료 ({expected:.2f}s 예상)")
finally:
    # tx_pulse(0, 0) 은 문서와 달리 거부된다. 라인을 놓으면 전송도 정리된다.
    lgpio.gpio_free(h, PUL)
    lgpio.gpio_claim_output(h, PUL, 0)
    lgpio.gpio_write(h, PUL, 0)
    if ENA is not None:
        lgpio.gpio_write(h, ENA, 1)         # 모터 여자 해제
    lgpio.gpiochip_close(h)

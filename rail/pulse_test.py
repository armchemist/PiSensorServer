import pigpio, time

PUL = 22
DIR = 27

pi = pigpio.pi()
pi.set_mode(PUL, pigpio.OUTPUT)
pi.set_mode(DIR, pigpio.OUTPUT)

pi.write(DIR, 1)   # 방향

# 200펄스만 보내기 (아주 조금)
for _ in range(200):
    pi.write(PUL, 1)
    time.sleep(0.0005)   # 500us
    pi.write(PUL, 0)
    time.sleep(0.0005)

pi.stop()

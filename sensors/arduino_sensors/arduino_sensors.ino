/*
 * pH(SEN0161) + 전도도/TDS(SEN0244) 값을 읽어 JSON 한 줄씩 시리얼로 내보낸다.
 *
 * 라즈베리파이에는 ADC가 없어서 아날로그 센서를 직접 못 읽는다. 우노가 대신
 * 읽고 USB 시리얼로 넘기는 구조. 파이 쪽은 sensors/read_sensors.py 참고.
 *
 * 배선 (자세한 건 sensors/README.md):
 *   SEN0161 (pH)   신호 -> A0,  VCC -> 5V,  GND -> GND
 *   SEN0244 (TDS)  신호 -> A1,  VCC -> 5V,  GND -> GND
 *
 * ⚠️ PH_SLOPE / PH_INTERCEPT 는 프로브마다 다르다. 반드시 캘리브레이션 후
 *    값을 바꿔서 다시 업로드할 것. 기본값은 이론상 출발점일 뿐이다.
 */

const uint8_t PH_PIN  = A0;
const uint8_t TDS_PIN = A1;

const float   ADC_REF_V = 5.0;    // 우노 기준 전압
const int     ADC_MAX   = 1023;   // 10비트ㅈ
const uint8_t SAMPLES   = 30;     // 중앙값 필터 표본 수
const unsigned long PERIOD_MS = 1000;

// --- pH 캘리브레이션 (README의 절차로 구한 값으로 교체) ---
// pH = PH_SLOPE * 전압 + PH_INTERCEPT
// 기본값은 "pH 7.00 = 2.5V, 기울기 3.5" 라는 이론값 기준
const float PH_SLOPE     = 3.5;
const float PH_INTERCEPT = -1.75;

// 수온. DS18B20 같은 수온 센서를 붙이기 전까지는 상온으로 가정한다.
// pH와 TDS 둘 다 수온에 민감해서, 정확한 값이 필요하면 실측해야 한다.
const float WATER_TEMP_C = 25.0;

// 표본을 정렬해 중앙값을 취한다. 평균과 달리 튀는 값 하나에 흔들리지 않는다.
float readVoltage(uint8_t pin) {
  int buf[SAMPLES];
  for (uint8_t i = 0; i < SAMPLES; i++) {
    buf[i] = analogRead(pin);
    delay(2);
  }
  for (uint8_t i = 1; i < SAMPLES; i++) {      // 삽입 정렬
    int key = buf[i];
    int j = i;
    while (j > 0 && buf[j - 1] > key) {
      buf[j] = buf[j - 1];
      j--;
    }
    buf[j] = key;
  }
  int median = (SAMPLES % 2)
      ? buf[SAMPLES / 2]
      : (buf[SAMPLES / 2 - 1] + buf[SAMPLES / 2]) / 2;
  return (float)median * ADC_REF_V / ADC_MAX;
}

void setup() {
  Serial.begin(9600);
  pinMode(PH_PIN, INPUT);
  pinMode(TDS_PIN, INPUT);
}

void loop() {
  float phV  = readVoltage(PH_PIN);
  float tdsV = readVoltage(TDS_PIN);

  float ph = PH_SLOPE * phV + PH_INTERCEPT;

  // DFRobot SEN0244 공식. 25도 기준으로 보정한 뒤 3차식으로 EC를 구한다.
  float coeff = 1.0 + 0.02 * (WATER_TEMP_C - 25.0);
  float cv = tdsV / coeff;
  float ec  = 133.42 * cv * cv * cv - 255.86 * cv * cv + 857.39 * cv;  // uS/cm
  float tds = ec * 0.5;                                               // ppm

  // 파싱하기 쉽게 JSON 한 줄로. 원시 전압도 같이 보내야 캘리브레이션이 가능하다.
  Serial.print(F("{\"ph_v\":"));  Serial.print(phV, 4);
  Serial.print(F(",\"ph\":"));    Serial.print(ph, 2);
  Serial.print(F(",\"tds_v\":")); Serial.print(tdsV, 4);
  Serial.print(F(",\"ec\":"));    Serial.print(ec, 1);
  Serial.print(F(",\"tds\":"));   Serial.print(tds, 1);
  Serial.print(F(",\"temp\":"));  Serial.print(WATER_TEMP_C, 1);
  Serial.println(F("}"));

  delay(PERIOD_MS);
}

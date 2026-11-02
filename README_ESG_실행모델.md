# ESG 규칙 기반 평가 실행 모델

이 프로그램은 `Rule Book`의 **잠정 1점**을 그대로 최종 점수로 사용하지 않습니다. 기업의 공식 PDF에서 규칙별 후보 근거를 찾고, 그 근거를 사람 또는 LLM이 검증한 뒤 `1 / 0 / N/A / 미확인`으로 최종 판정합니다.

## 1. 입력 준비

평가할 기업별로 PDF 또는 DART/KIND에서 저장한 HTML 공시문서를 한 폴더에 넣습니다.

```text
project/
├─ esg_rule_engine.py
├─ 삼성전자,sk하닉_rulebook_v.1.1.xlsx
└─ input_docs/
   └─ 삼성SDI/
      ├─ 삼성SDI_지속가능경영보고서.pdf
      ├─ 삼성SDI_사업보고서.pdf
      └─ 삼성SDI_기업지배구조보고서.pdf
```

권장 원본은 회사가 공개한 지속가능경영보고서, 사업보고서, 기업지배구조보고서입니다. 평가연도가 **2025년**이면, 보고서 제목이나 발행일이 아니라 문서 안의 실제 성과 기준연도가 2025년인지 검증합니다. 표지에서 다른 성과연도가 명확히 감지된 문서는 자동으로 점수 근거에서 제외하고 결과 파일에 별도 기록합니다.

현재 공유된 Rule Book에는 일부 `1점 인정조건`이 2024년 기준으로 남아 있습니다. 실행 시 `--target-year 2025`를 주면 결과 파일의 `실행 1점 인정조건` 열에 2025년 기준을 별도로 기록합니다. 발표 전에는 팀이 원본 Rule Book의 기준연도를 하나로 동결하는 것이 필요합니다.

## 2. 설치

```bash
pip install pandas openpyxl pypdf
```

LLM 자동 판정도 사용할 때만 아래를 추가합니다.

```bash
pip install openai
```

ZIP을 푼 폴더에 있는 `.env` 파일을 열어 한 줄만 채웁니다. API 키는
`esg_rule_engine.py`에 직접 쓰지 않습니다.

```text
OPENAI_API_KEY=발급받은_API_키
```

`.env`는 외부 공유·PPT 첨부 시 함께 보내지 않습니다.

## 3. 1차 실행: 후보 근거 검색 + 사람이 최종 검토

```bash
python esg_rule_engine.py \
  --rulebook "삼성전자,sk하닉_rulebook_v.1.1.xlsx" \
  --pdf-dir "input_docs/삼성SDI" \
  --company "삼성SDI" \
  --target-year 2025 \
  --output "삼성SDI_검증결과.xlsx" \
  --review-mode manual
```

생성 파일의 `판정결과` 시트에서 `후보근거`, `후보문서·페이지`를 보고 사람이 `최종판정`을 확정합니다. `점수요약`은 최종판정만 집계합니다.

## 4. 2차 실행: LLM 자동 판정

```bash
python esg_rule_engine.py \
  --rulebook "삼성전자,sk하닉_rulebook_v.1.1.xlsx" \
  --pdf-dir "input_docs/삼성SDI" \
  --company "삼성SDI" \
  --target-year 2025 \
  --output "삼성SDI_LLM검증결과.xlsx" \
  --review-mode llm
```

LLM에는 PDF 전체가 아니라 규칙별로 검색된 상위 3개 페이지의 일부 발췌문만 전달됩니다. 따라서 토큰 사용량을 제어하면서도, `최종문서`, `최종페이지`, `최종인용문`을 결과에 남깁니다.

## 5. 결과 해석

| 값 | 의미 |
|---|---|
| `1` | 원문이 규칙의 1점 인정조건을 충족 |
| `0` | 원문에서 미충족 사실을 확인 |
| `N/A` | 그 기업/업종에는 적용되지 않음을 확인 |
| `미확인` | 공식 PDF 근거가 없거나 부족 |
| `검토필요` | 후보 근거는 찾았으나 사람 또는 LLM 검증 전 |

`미확인`과 `검토필요`를 임의로 0점 처리하지 않습니다. 발표에서는 점수와 함께 미확인 비율을 보여 주는 편이 타당합니다.

## 6. 미확인 항목만 재검증

기존 결과에서 `미확인` 또는 `검토필요`였던 규칙만 더 넓은 원문 발췌와 후보 5페이지로 다시 판정합니다. 기존에 확정된 `1 / 0 / N/A` 결과는 유지합니다.

```bash
python esg_rule_engine.py \
  --rulebook "반도체,배터리,자동차,화학_rulebook_v.1.xlsx" \
  --pdf-dir "input_docs/삼성SDI" \
  --company "삼성SDI" \
  --target-year 2025 \
  --output "삼성SDI_재검증결과.xlsx" \
  --review-mode llm \
  --model "gpt-6-luna" \
  --refine-unconfirmed "삼성SDI_최종결과.xlsx"
```

`점수요약`의 `근거확인률`이 80% 미만이면 `참고 1점률(판정완료 기준)`은 기업 ESG 점수가 아니라, 확인된 항목 안에서의 비율로만 해석합니다.

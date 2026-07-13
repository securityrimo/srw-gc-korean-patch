# 텍스트 번역 JSON 수정 가이드

## 결론

일반 대사와 메뉴 번역을 고칠 때는 생성된 `*_replacements.json`을 직접
수정하지 않고 다음 교정 원본에 안정 ID별 변경을 기록합니다.

- 일반 대사·메뉴·도감: `data/pdf_translation_quality_overrides.json`
- 정신기 명칭·축약·설명: `data/spirit_command_corrections.json`

두 파일은 Git에 포함되는 검토 원본입니다. 입력 해시, 일본어 원문,
교정 전 문구와 안정 ID를 함께 검사하므로 다른 레코드에 번역문이 들어가는
오류를 막을 수 있습니다.

## 일반 대사와 메뉴

`data/pdf_translation_quality_overrides.json`에는 세 가지 교정 방식이 있습니다.

| 항목 | 용도 |
| --- | --- |
| `dialogue_overrides` | 시나리오 대사처럼 레코드 하나를 안정 ID로 직접 교정 |
| `payload_overrides` | 메뉴·도감·고정 문구를 안정 ID로 직접 교정 |
| `context_replacements` | 같은 오역을 일본어 원문·화자 조건에 맞는 여러 레코드에서 교정 |

대사 한 건을 수정하는 기본 형식은 다음과 같습니다.

```json
{
  "id": "add01:0125:0000",
  "japanese": "일본어 원문",
  "before": "현재 한국어",
  "after": "수정할 한국어",
  "pdf": "검토 근거",
  "page": 0
}
```

메뉴나 도감 문구도 같은 형식을 사용하되 `payload_overrides`에 넣습니다.
새 항목을 추가했다면 파일 끝의 `expected_payload_override_count`,
`expected_dialogue_override_count`, `expected_changed_record_count`를 실제 결과와
맞춰야 합니다.

대사의 `KK`, `<AA>`, `<FF>`, `<TT>` 같은 값은 게임의 구조 토큰입니다.
번역하면서 삭제하거나 개수를 바꾸면 안 됩니다. 교정 도구는 구조 토큰 수가
달라지면 빌드를 중단합니다.

## 정신기

정신기는 `data/spirit_command_corrections.json`에서 수정합니다.

- `full_names_korean`: 전체 명칭 30건의 최종 순서
- `short_labels_korean`: 1글자 축약 30건의 최종 순서
- `descriptions_korean`: 화면에 표시되는 설명 29건의 최종 순서
- `corrections`: 안정 ID별 `japanese`·`before`·`after` 교정

정신기 설명의 마지막 일본판 슬롯 `？？？`는 숨김 데이터이므로 번역 항목을
추가하지 않습니다. 보이는 설명은 최대 2줄, 한 줄 23자·46바이트 이내로
유지해야 합니다. 항목을 추가하거나 제거했다면 파일 위쪽의
`expected_*_correction_count` 값도 함께 조정합니다.

## 전체 텍스트 마스터와 실제 빌드 맵

로컬 분석 작업에는 모든 일본어·한국어 레코드를 모은
`srw_gc_all_japanese_korean_master_v2.json`이 있습니다. 전체 62,884건을 한 번에
검토할 때는 `records[].id`, `records[].japanese`, `records[].final_korean`을
사용합니다. 이 파일은 원문 전체 덤프이므로 Git 저장소와 릴리스에는 포함하지
않습니다.

바이너리 조립기가 직접 읽는 생성 맵은 다음과 같습니다.

| 생성 맵 | 주요 내용 |
| --- | --- |
| `add01_replacements.json` | 시나리오·이벤트 대사 |
| `add02_replacements.json` | 메뉴·시스템·정신기·고정 설명 |
| `bpilot_replacements.json` | 인물·로봇도감과 파일럿 데이터 |
| `dol_all_replacements.json` | `Start.dol` 내부 표시 문구 |
| `dol_name_input_replacements.json` | 이름 입력 화면 문자표 |

이 파일들은 교정 도구가 만드는 산출물입니다. 직접 수정하면 다음 재생성 때
덮어써지고 입력 해시 검사도 실패하므로, 최종 수정은 반드시 `data/`의 교정
JSON에 기록합니다.

## 적용 순서

1. 일본판 원문 레코드에서 수정할 안정 ID와 정확한 `japanese`를 찾습니다.
2. 현재 생성 맵의 값을 `before`에 그대로 넣습니다.
3. 검토한 번역을 `after`에 넣고 구조 토큰과 표시 폭을 확인합니다.
4. 일반 교정은 `apply_translation_quality_overrides.py`, 정신기 교정은
   `apply_spirit_command_corrections.py`로 새 출력 디렉터리에 적용합니다.
5. `assemble_japanese_binaries.py`와 `audit_japanese_binaries.py`로 재조립 및
   바이너리 검증을 수행합니다.

교정 도구는 기존 출력 디렉터리를 덮어쓰지 않습니다. 항상 새 출력 경로를
사용해야 합니다.

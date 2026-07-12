# UI 이미지 한국어화

메뉴 글자는 단순 문자열이 아니라 `add00dat.bin`의 GX I4 아틀라스와
`SCR` 타일맵으로 구성되어 있습니다. 기존 방식처럼 공유 아틀라스 위에
글자를 덧그리면 한 타일을 함께 쓰는 다른 메뉴까지 오염될 수 있으므로,
각 `SCR`을 독립 이미지로 렌더링한 뒤 타일을 다시 묶습니다.

## 글꼴

- 글꼴: 맑은 고딕 Bold (`malgunbd.ttf`)
- SHA-256: `E8CBC0B2AFCC14FB45DFB6086D5102C0B23A96E7B6E708F3122ACDE1B86C9082`
- 런타임 대사 글꼴과 UI 이미지 글꼴은 같은 파일을 사용합니다.

글꼴 파일은 Microsoft Windows의 시스템 글꼴이므로 저장소나 릴리스에
재배포하지 않습니다. 빌더는 `%WINDIR%\Fonts\malgunbd.ttf`를 기본값으로
사용하며, 다른 위치에 있는 합법적으로 취득한 같은 파일은 `--font`로
지정할 수 있습니다. 어느 경우든 해시가 다르면 작업을 중단합니다.

## 처리 대상

- 대형 UI 아틀라스 `518`: SCR 399개 중 문자 381개 재작성, 비문자 18개 보존
- 소형 UI 아틀라스 10개: SCR 75개 중 문자 66개 재작성, 비문자 9개 보존
- 타이틀 메뉴 아틀라스 `3489`: 문자 5개 재작성, 선택 강조 이미지 5개 보존
- 로딩 이미지 SPR `3508`: `Now Loading...`을 `불러오는 중...`으로 재작성

일본 정식판 표시 의미를 기준으로 번역했고, 영문판에서 의미가 달라진 항목도
일본어 원문에 맞춰 교정했습니다. 예를 들어 `UNIT LIST`로 잘못 옮겨진
`連続攻撃`은 `연속 공격`, `Ctr` 뒤에 잘못 들어간 `지휘`는 `반격`으로
고쳤습니다.

## 재구성 순서

아래 명령의 일본판·영문 참고 `add00dat.bin`은 사용자가 직접 준비해야 하며
저장소에는 포함되지 않습니다.

```powershell
python -X utf8 .\tools\repack_direct_scr_atlas.py `
  .\input\add00dat.bin .\build\add00_step1.bin `
  --translations .\data\ui_block518_ko.json `
  --japanese .\reference\japanese\add00dat.bin `
  --english .\reference\english\add00dat.bin `
  --bitmap 518 `
  --font "$env:WINDIR\Fonts\malgunbd.ttf" `
  --report .\build\ui_block518_audit.json `
  --preview-dir .\build\ui_block518_preview

python -X utf8 .\tools\repack_small_ui_atlases.py `
  .\build\add00_step1.bin .\build\add00_step2.bin `
  --japanese .\reference\japanese\add00dat.bin `
  --english .\reference\english\add00dat.bin `
  --mapping .\data\ui_small_and_title_ko.json `
  --font "$env:WINDIR\Fonts\malgunbd.ttf" `
  --report .\build\ui_small_title_audit.json `
  --preview-dir .\build\ui_small_title_preview

python -X utf8 .\tools\patch_now_loading_spr.py `
  .\build\add00_step2.bin .\build\add00_final.bin `
  --font "$env:WINDIR\Fonts\malgunbd.ttf" `
  --logical-payload .\build\now_loading_ko_312x40.c4 `
  --report .\build\now_loading_audit.json
```

참조 제작 환경은 Python `3.13.1`, Pillow `11.1.0`, FreeType `2.13.3`입니다.
감사 JSON에는 입력·참고 파일, 매핑 JSON, 글꼴의 SHA-256과 렌더러 버전이
기록됩니다. 동일 입력으로 재현했을 때의 주요 해시는 다음과 같습니다.

| 단계 | SHA-256 |
| --- | --- |
| UI 적용 전 `add00dat.bin` | `D2CF4A8B231A3028207B2BD5D2019FB4E080492B90933A1F10CB1F37F21AA6AB` |
| 일본판 참고 `add00dat.bin` | `7B592F335EDDD016198A8324EF98DEFC942D072D4FB96321AE58BF4E8504E0EF` |
| 영문판 참고 `add00dat.bin` | `E64985BD142EE15AAC4C1967E403F856FB1DE4003F93AC606D497AD33C08AB3B` |
| 대형 아틀라스 매핑 JSON | `5F3DDD240ECF76153DE3902165ABE9A9C4EC3FD7371B7BE7EEB30CCC32796436` |
| 소형·타이틀 매핑 JSON | `2CD0F3224AEE84E0EBC5B0D3788AD86A2FCBE6D0E4D78D8FAFF8BE945233E6AB` |
| 대형 아틀라스 적용 결과 | `9A9B845AE5406FBFC2073C078B594514C42E7A0AF5DB0338EB927D3C0093BF7D` |
| 소형·타이틀 적용 결과 | `A89DDDBE4F936F570FD2D8E7DEEBEFFA4F81D530FB7879CE2E2A29D2870AA87D` |
| 로딩 이미지까지 적용한 최종 결과 | `1286C77D3AF64DB27EFA3E3C2A1A0300C423915797FBA19E154C5F8EA91F33AC` |

## 형식과 검증

- 아틀라스 `518`: 하위 14비트 타일 인덱스, 비트 14/15 좌우·상하 반전
- 나머지 아틀라스: 하위 10비트 타일 인덱스, 비트 10/11 좌우·상하 반전
- 로딩 SPR: 312×40 논리 이미지를 `256/32/16/8` 폭과 `32/8` 높이의 C4 텍스처 8개로 분할
- 모든 8×8 타일을 중복 제거해 원래 아틀라스 용량 안에 재배치
- 팔레트, BMP·SPR 헤더, CEL, 외부 포인터 테이블, 블록 오프셋과 전체 파일 크기 보존
- 재구성한 모든 SCR을 다시 렌더링해 목표 이미지와 픽셀 단위 비교
- 허용한 아틀라스·SCR 외 블록 변경 금지

타이틀의 한국어 `슈퍼로봇대전 GC` 로고와 일본판 기반 이벤트 데이터는 이
작업에서 건드리지 않습니다.

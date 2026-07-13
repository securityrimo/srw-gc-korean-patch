# 슈퍼로봇대전 GC 한글패치

닌텐도 게임큐브 일본판 **슈퍼로봇대전 GC**용 비공식 한국어 패치입니다. 대상 게임 ID는 `GRWJD9`이며, 다른 지역판이나 이미 수정된 이미지에는 적용할 수 없습니다.

최신 배포본은 [v1.0.3 릴리스](https://github.com/snake7594/srw-gc-korean-patch/releases/tag/v1.0.3)에서 받을 수 있습니다.

**v1.0.1은 전투 화면의 동적 타일 번호를 손상할 수 있으므로 사용하지 마세요.**

## v1.0.3 주요 내용

- 대사와 메뉴의 한국어화
- 공략집 PDF 2권과 일본어 원문을 교차 검토해 실제 텍스트 2,778건 교정
- 초반·중반 주요 장면을 포함한 대사 95건을 문맥과 화자 말투에 맞게 직접 교정
- 시나리오 제목 39건을 재검토하고, 이미 정확했던 2건을 제외한 37개 제목 이미지를 같은 굵은 맑은 고딕으로 교체
- 고유명사 뒤에 조사가 붙어 교정에서 빠졌던 이름과 도감의 심각한 기계번역·내부 placeholder를 정리
- 출력 맵 전체에서 일본어 문자와 내부 placeholder 잔존 0건 확인
- 한국어 글꼴과 런타임 문자 코드 대응
- 일본판을 기준으로 이벤트 흐름과 화자 순서를 유지
- 영문판 패치에서 유입됐던 타이틀·배너·문자열 흔적 제거
- 영문 UI·타이틀 메뉴 이미지 452개를 일본판 의미 기준의 한국어 이미지로 교체
- `Now Loading...` 이미지를 같은 굵은 맑은 고딕의 `불러오는 중...`으로 교체
- 원본 UI 아틀라스 타일 번호를 전부 보존해 전투 유닛 그래픽 손상 수정
- 한국어 이미지가 필요한 SCR만 확장 영역을 참조하는 copy-on-write 재패킹 적용
- 주인공 이름 통일
  - 남성 주인공: **아카츠키 아키미**
  - 여성 주인공: **아카츠키 아케미**

## 패치 대상

| 항목 | 값 |
| --- | --- |
| 지역 | 일본 |
| 게임 ID | `GRWJD9` |
| 원본 이미지 크기 | `1,459,978,240` 바이트 |
| 원본 SHA-256 | `AD4CB99FFB3C0383802A2AB87963F98BA417DFC5184ED3FE3DFE077DA02DB229` |
| 결과 이미지 크기 | `1,459,978,240` 바이트 |
| 결과 SHA-256 | `5F289065E0AEE0947DF6A7C7C42A62ECD6BFB5E3E52DF688F8870D8F766C7885` |
| xdelta 크기 | `109,515,300` 바이트 |
| xdelta SHA-256 | `167EB20110AE784D6553126CC6DE2857B35BBA2096CF6B919B80C33A172E7BC1` |

소유한 정품 디스크에서 직접 만든, 수정되지 않은 원본 ISO만 사용하세요. 적용 스크립트가 원본 SHA-256을 검사하므로 파일명이 달라도 정확한 원본이면 사용할 수 있습니다.

## 적용 방법

1. [xdelta3 공식 프로젝트](https://github.com/jmacd/xdelta-gpl/releases)에서 Windows용 `xdelta3.exe`를 준비합니다.
2. 다음 파일을 같은 폴더에 둡니다.
   - `SRW_GC_Korean_v1.0.3.xdelta`
   - `APPLY_PATCH.bat`
   - `apply_patch.ps1`
   - `xdelta3.exe` — 또는 `xdelta3`/`xdelta`를 `PATH`에 등록
3. 명령 프롬프트에서 아래처럼 실행합니다.

```bat
APPLY_PATCH.bat "D:\Games\Super Robot Taisen GC.iso"
```

출력 경로를 직접 정하려면 두 번째 인수를 지정합니다.

```bat
APPLY_PATCH.bat "D:\Games\Super Robot Taisen GC.iso" "D:\Games\Super Robot Taisen GC Korean.iso"
```

출력 경로를 생략하면 원본 ISO와 같은 폴더에 `Super Robot Taisen GC_Korean_v1.0.3.iso`가 만들어집니다. 스크립트는 패치 전 원본과 패치 후 결과의 SHA-256을 모두 검사합니다.

PowerShell에서 직접 실행할 수도 있습니다.

```powershell
.\apply_patch.ps1 -SourceIso 'D:\Games\Super Robot Taisen GC.iso'
```

## 저장소 구성

- `tools/`: 일본판 데이터 재구성, 문자열 재배치 및 독립 감사를 위한 소스 코드
- `tools/repack_preserve_indices.py`: 원본 타일 번호를 보존하는 최종 UI 재패커
- `tools/audit_shared_atlas_refs.py`: 동적·공유 타일 손상 여부 감사 도구
- `tools/apply_translation_quality_overrides.py`: PDF·일본어 원문 교정값을 안정 ID 기준으로 적용하고 구조·문자 잔존을 검사하는 도구
- `tools/patch_episode_title_graphics.py`: 허용된 시나리오 제목 BMP만 고정 레이아웃으로 다시 그리는 도구
- `docs/기술_문서.md`: 패치 구조와 검증 절차
- `docs/UI_이미지_패치.md`: 메뉴·타이틀·로딩 이미지의 재패킹 방식과 재현값
- `data/`: UI 이미지 매핑과 검토된 PDF 번역 품질 교정 JSON
- `APPLY_PATCH.bat`, `apply_patch.ps1`: Windows용 패치 적용 도구
- `release_manifest.json`, `SHA256SUMS.txt`: 릴리스 식별값과 파일 검증값

게임 ROM, 추출한 게임 바이너리, 원문 전체 덤프, 전체 대사 데이터, 참조 PDF 및 xdelta 실행 파일은 이 저장소에 포함하지 않습니다. 참조 PDF는 파일명·페이지·SHA-256만 교정 근거로 기록합니다.

## 문제 해결

- **원본 해시가 다름**: 다른 지역판, 압축 해제 과정에서 변형된 이미지 또는 기존 패치가 적용된 이미지일 수 있습니다.
- **xdelta3를 찾지 못함**: `xdelta3.exe`를 적용 스크립트와 같은 폴더에 두거나 실행 파일을 `PATH`에 등록하세요.
- **출력 파일이 이미 있음**: 데이터 손상을 막기 위해 자동으로 덮어쓰지 않습니다. 기존 파일을 다른 곳으로 옮기거나 다른 출력 경로를 지정하세요.

## 법적 고지

이 프로젝트는 원저작권자 및 관련 회사와 관계없는 팬 번역 프로젝트입니다. 패치에는 원본 게임 ISO가 포함되지 않으며, 원본 게임에 관한 모든 권리는 각 권리자에게 있습니다. 패치 사용자는 자신이 합법적으로 소유한 정품에서 원본 이미지를 직접 준비해야 합니다. ROM 이미지의 공유·판매·재배포를 금합니다.

저장소에서 독자적으로 작성한 도구 소스 코드의 라이선스는 [LICENSE](LICENSE)를 확인하세요. xdelta3의 라이선스와 배포 위치는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 정리되어 있습니다.

# 슈퍼로봇대전 GC 한글패치

닌텐도 게임큐브 일본판 **슈퍼로봇대전 GC**용 비공식 한국어 패치입니다. 대상 게임 ID는 `GRWJD9`이며, 다른 지역판이나 이미 수정된 이미지에는 적용할 수 없습니다.

최신 배포본은 [v1.0.1 릴리스](https://github.com/snake7594/srw-gc-korean-patch/releases/tag/v1.0.1)에서 받을 수 있습니다.

## v1.0.1 주요 내용

- 대사와 메뉴의 한국어화
- 한국어 글꼴과 런타임 문자 코드 대응
- 일본판을 기준으로 이벤트 흐름과 화자 순서를 유지
- 영문판 패치에서 유입됐던 타이틀·배너·문자열 흔적 제거
- 영문 UI·타이틀 메뉴 이미지 452개를 일본판 의미 기준의 한국어 이미지로 교체
- `Now Loading...` 이미지를 같은 굵은 맑은 고딕의 `불러오는 중...`으로 교체
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
| 결과 SHA-256 | `600AF6247FBDE1AE4C855ACFE6995BD0B3CD76536D3A1BB01366D8806D56451A` |
| xdelta 크기 | `109,690,375` 바이트 |
| xdelta SHA-256 | `687F3D076820259F015C54DE1CACBE5AD9AA7E37DED5E647B34EC1AF5386374D` |

소유한 정품 디스크에서 직접 만든, 수정되지 않은 원본 ISO만 사용하세요. 적용 스크립트가 원본 SHA-256을 검사하므로 파일명이 달라도 정확한 원본이면 사용할 수 있습니다.

## 적용 방법

1. [xdelta3 공식 프로젝트](https://github.com/jmacd/xdelta-gpl/releases)에서 Windows용 `xdelta3.exe`를 준비합니다.
2. 다음 파일을 같은 폴더에 둡니다.
   - `SRW_GC_Korean_v1.0.1.xdelta`
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

출력 경로를 생략하면 원본 ISO와 같은 폴더에 `Super Robot Taisen GC_Korean_v1.0.1.iso`가 만들어집니다. 스크립트는 패치 전 원본과 패치 후 결과의 SHA-256을 모두 검사합니다.

PowerShell에서 직접 실행할 수도 있습니다.

```powershell
.\apply_patch.ps1 -SourceIso 'D:\Games\Super Robot Taisen GC.iso'
```

## 저장소 구성

- `tools/`: 일본판 데이터 재구성, 문자열 재배치 및 독립 감사를 위한 소스 코드
- `docs/기술_문서.md`: 패치 구조와 검증 절차
- `docs/UI_이미지_패치.md`: 메뉴·타이틀·로딩 이미지의 재패킹 방식과 재현값
- `data/`: UI 이미지 한국어 매핑 JSON
- `APPLY_PATCH.bat`, `apply_patch.ps1`: Windows용 패치 적용 도구
- `release_manifest.json`, `SHA256SUMS.txt`: 릴리스 식별값과 파일 검증값

게임 ROM, 추출한 게임 바이너리, 원문 전체 덤프, 전체 대사 데이터 및 xdelta 실행 파일은 이 저장소에 포함하지 않습니다.

## 문제 해결

- **원본 해시가 다름**: 다른 지역판, 압축 해제 과정에서 변형된 이미지 또는 기존 패치가 적용된 이미지일 수 있습니다.
- **xdelta3를 찾지 못함**: `xdelta3.exe`를 적용 스크립트와 같은 폴더에 두거나 실행 파일을 `PATH`에 등록하세요.
- **출력 파일이 이미 있음**: 데이터 손상을 막기 위해 자동으로 덮어쓰지 않습니다. 기존 파일을 다른 곳으로 옮기거나 다른 출력 경로를 지정하세요.

## 법적 고지

이 프로젝트는 원저작권자 및 관련 회사와 관계없는 팬 번역 프로젝트입니다. 패치에는 원본 게임 ISO가 포함되지 않으며, 원본 게임에 관한 모든 권리는 각 권리자에게 있습니다. 패치 사용자는 자신이 합법적으로 소유한 정품에서 원본 이미지를 직접 준비해야 합니다. ROM 이미지의 공유·판매·재배포를 금합니다.

저장소에서 독자적으로 작성한 도구 소스 코드의 라이선스는 [LICENSE](LICENSE)를 확인하세요. xdelta3의 라이선스와 배포 위치는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 정리되어 있습니다.

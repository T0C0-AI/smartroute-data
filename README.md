# smartroute-data

[SmartRoute](https://github.com/T0C0-AI/SmartRoute) 안드로이드 앱이 서버 없이 읽어가는 장소 데이터 저장소.

- 원본: 공공데이터포털(행정안전부 지방행정 인허가 데이터) — 전국일반음식점표준데이터
- `tools/build_places_data.py`가 매주 자동으로 최신 데이터를 받아 `data/`를 갱신함 (GitHub Actions)
- `data/version.json`, `data/places_<region>.json`을 앱이 `raw.githubusercontent.com`으로 직접 읽는다 — 이 저장소가 공개(public)인 이유

## 편의시설 데이터 관련

기본 데이터셋(`places_<region>.json`)에는 주차·아기의자·반려동물 동반 같은 편의시설 정보가 없다.
`has`/`traits`/`verifiedClear` 필드는 전부 0(모름)으로 채워져 있다 — 확인 안 된 정보를 추측으로
채우지 않는다.

### 보강 데이터 (`enrich_<region>.json`)

한국관광공사 TourAPI(공공데이터포털)에서 주차 정보를 받아 이름+좌표(100m 이내)로 정확히 매칭된
곳만 `data/enrich_<region>.json`에 별도로 담는다. `tools/build_enrichment.py`로 만든다.

- **커버리지가 낮다**: TourAPI는 관광공사가 별도로 등록·관리하는 곳만 있어서, 강남구 실측 기준
  우리 데이터 14,029곳 중 181곳만 TourAPI에 있고, 그 중 실제로 매칭+주차 가능이 확인된 곳은
  59곳(약 0.4%)이다. 매칭 안 된 나머지는 여전히 `has=0`(모름)이다.
- **왜 별도 파일인가**: `build_places_data.py`가 매주 `places_<region>.json`을 통째로 다시 쓰면서
  `has`를 0으로 초기화하기 때문에, 같은 파일에 넣으면 매주 보강 데이터가 사라진다. 그래서
  `enrich_<region>.json`을 완전히 분리하고, 앱이 두 파일을 따로 받아서 합친다.
  - `enrich_<region>.json`은 `[{"id": "<장소 id>", "has": <비트마스크>}]` 형태이고, has 값이 있는
    곳만 담는다(0인 곳은 아예 안 넣음).
  - `data/version.json`의 `enrichRegions`에 지역별 버전을 따로 관리한다.
- `kidsfacility` 필드는 표본에서 값이 전부 비어 있어서 이번엔 안 썼다.

#### 전국 확대 진행 상황

TourAPI 개발계정은 하루 호출 한도가 1,000건이라(전국 전체 예상 호출량 약 9,000건), 하루치씩
나눠서 `tools/build_enrichment.py`를 여러 번 실행해 진행한다. 실행할 때마다 완료된 지역이
`tools/enrichment_progress.json`에 저장되고, 다음 실행은 자동으로 그 다음 지역부터 이어간다.

**진행 상황: 24 / 226 지역 완료 (2026-08-08 기준)** — 서울 24개 구 완료, 나머지 지역 남음.
이어서 하려면: `python3 tools/build_enrichment.py`

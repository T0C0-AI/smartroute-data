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
- 같은 detailIntro2 응답에서 `smoking`(금연 매장)·`packing`(포장 가능)·`chkcreditcardfood`
  (카드 결제 가능) 값도 같이 뽑아서 반영한다 — 상세조회 호출을 조건 개수만큼 늘릴 필요 없이
  한 번의 응답에서 다 나온다.

#### 전국 확대 진행 상황 (TourAPI, 주차·금연·포장·카드결제)

TourAPI 개발계정은 하루 호출 한도가 1,000건이라(전국 전체 예상 호출량 약 9,000건), 하루치씩
나눠서 `tools/build_enrichment.py`를 여러 번 실행해 진행한다. 실행할 때마다 완료된 지역이
`tools/enrichment_progress.json`에 저장되고, 다음 실행은 자동으로 그 다음 지역부터 이어간다.

**진행 상황: 40 / 226 지역 완료 (2026-08-09 기준)** — 서울 전체 + 인천·대구 일부 완료.
이어서 하려면: `python3 tools/build_enrichment.py`

### 반려동물 실내 (`tools/build_pet_enrichment.py`)

식품안전나라(식약처)의 "반려동물 동반출입 음식점" 등록 현황(2026.3.1. 시행된 제도, 전국
2,440개소)을 이름+주소로 매칭해서 반영한다. 별도 API·호출 한도가 없어서 한 번에 전국을 다
처리했다 — **전국 174개 지역, 1,962곳 반영 완료(2026-08-09 기준)**. 매칭 기준은 이름이
유일하게(중복 없이) 일치하는 경우만 — 오매칭이 데이터 없음보다 나쁘다고 보고 안전하게 처리.

### 아기의자 (`tools/build_highchair_enrichment.py`)

한국문화정보원_전국 세계 음식점 데이터(공공데이터포털)의 "유아의자 대여여부" 필드로 반영한다.
이 데이터는 로그인해야 다운로드되는 파일이라(오픈API도 별도 활용신청 필요, 401 확인함)
자동으로 매번 새로 못 받는다 — `tools/source_snapshots/`에 다운로드한 CSV를 스냅샷으로 같이
커밋해뒀다. 전체 9,502곳 중 92곳만 "유아의자 대여여부=Y"였고, 이름+좌표(100m 이내)로 매칭해서
**전국 14개 지역, 22곳 반영(2026-08-09 기준)**. "무료주차 가능여부" 컬럼은 9,502곳 전부 Y로
찍혀있어 실제 신호가 아니라고 판단해 안 썼다.

### 유모차 진입 (`tools/build_stroller_enrichment.py`)

한국관광공사 무장애 여행 정보 API(KorWithService2, TourAPI와 같은 제공기관·다른 상품이라
data.go.kr에서 별도 활용신청 필요 — 개발계정은 자동승인이라 즉시 됨)의 `detailWithTour2`
(무장애정보조회)에서 주출입구 접근성(`exit` 필드)을 받아 반영한다.

- API 응답에 `stroller`(유모차)라는 필드가 스키마엔 있는데 실측해보니 항상 빈 값이라 못 썼다.
  대신 `exit` 필드에 "턱이 없어 휠체어 접근 가능함", "경사로 있음" 같은 실제 문구가 들어있어서
  이걸로 판단한다 — 문턱 없는 출입구는 유모차도 당연히 들어갈 수 있으니 물리적으로 같은 조건이라고
  보고 대체 지표로 썼다. "턱이 있어"·"불가"·"어려움" 같은 부정 표현이 있으면 반영 안 한다.
- 같은 응답의 `babysparechair`(여벌 유아용 보조의자) 필드도 실제 값이 있어서 아기의자 조건에
  같이 반영한다(위 아기의자 데이터와 별개로 추가 보강).
- `detailWithTour2`는 `contentTypeId` 파라미터를 받지 않는다 — 넣으면
  `INVALID_REQUEST_PARAMETER_ERROR`가 난다(공식 매뉴얼로 확인, 삽질 좀 했음).
- 이름이 우리 데이터와 유일하게(중복 없이) 일치하는 곳만 반영. **전국 42개 지역 반영
  시작(2026-08-09 기준)**. 이어서 하려면: `python3 tools/build_stroller_enrichment.py`

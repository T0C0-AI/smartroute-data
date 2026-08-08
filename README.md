# smartroute-data

[SmartRoute](https://github.com/T0C0-AI/SmartRoute) 안드로이드 앱이 서버 없이 읽어가는 장소 데이터 저장소.

- 원본: 공공데이터포털(행정안전부 지방행정 인허가 데이터) — 전국일반음식점표준데이터
- `tools/build_places_data.py`가 매주 자동으로 최신 데이터를 받아 `data/`를 갱신함 (GitHub Actions)
- `data/version.json`, `data/places_<region>.json`을 앱이 `raw.githubusercontent.com`으로 직접 읽는다 — 이 저장소가 공개(public)인 이유

## 편의시설 데이터 관련

이 데이터셋에는 주차·아기의자·반려동물 동반 같은 편의시설 정보가 없다. `has`/`traits`/`verifiedClear`
필드는 전부 0(모름)으로 채워져 있다 — 확인 안 된 정보를 추측으로 채우지 않는다.

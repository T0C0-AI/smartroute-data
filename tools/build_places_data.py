#!/usr/bin/env python3
"""
공공데이터포털(행정안전부 지방행정 인허가 데이터)에서 전국 장소 CSV를 받아
앱이 쓰는 JSON 스키마(core.Place / PlaceEntity)로 변환한다.

확인된 사실 (2026-08-08, 실제 다운로드로 검증):
- 다운로드 URL은 로그인·API 키 없이 접근 가능하지만, 목록 페이지를 먼저 GET해서
  세션 쿠키(JSESSIONID)를 받아야 한다. 쿠키 없이 다운로드 엔드포인트로 바로 가면
  차단(403)된다.
- 좌표계는 EPSG:5174(Bessel 중부원점TM)다. WGS84가 아니라서 그대로 쓰면 지도에
  엉뚱한 위치로 찍힌다. pyproj로 변환한 뒤 강남구 역삼동 실제 주소 좌표와
  대조해 오차가 없는지 확인했다.
- 편의시설(주차·아기의자 등) 데이터는 이 데이터셋에 없다. has/traits/verifiedClear를
  전부 0(모름)으로 채운다 — 추측으로 채우면 안전 필터 로직이 무너진다.
- 카페 카테고리("식품_휴게음식점")에는 편의점·백화점·패스트푸드도 섞여 있다.
  코스에 쓸 만한 업태(커피숍·다방·전통찻집·떡카페·키즈카페)만 골랐다.
- **버그였던 것(2026-08-10 발견·수정)**: 맥도날드·버거킹·롯데리아·KFC 같은 패스트푸드
  체인은 "휴게음식점" 안에서 업태구분명이 "패스트푸드"인데, 위 화이트리스트에 없어서
  전국에서 통째로 빠져 있었다(강남구 실측: 맥도날드 9곳·버거킹 6곳·롯데리아 6곳이 전부
  누락). 배스킨라빈스·던킨 같은 디저트 체인도 업태구분명이 "과자점"·"아이스크림"이라
  마찬가지로 빠져 있었다. 이 3개 업태를 추가해서 다시 받는다 — 패스트푸드는 실제로
  "식사"이므로 cafe가 아니라 meal 카테고리로 분류한다(업태구분명별로 카테고리가 갈리는
  유일한 경우라 REST_CAFE_TYPE_TO_CATEGORY로 따로 관리).
  파리바게뜨는 이 카테고리에 아예 없다("제과점영업"이라는 완전히 다른 인허가 구분이라
  별도 데이터 소스가 필요함, 이번 수정 범위 밖) — docs/ROADMAP.md에 다음 작업으로 남김.
- orgCode에 "_ALL"을 붙이면(예: 6110000_ALL) 그 시/도 전체를 한 번에 받을 수 있고,
  각 행에는 자기 시군구 코드(개방자치단체코드)가 그대로 들어있다 — 그래서 245개
  시군구를 하나씩 받을 필요 없이 17개 시/도 단위로만 받고, 파싱 단계에서
  행 단위로 실제 시군구에 재배정한다. (서울 전체 실측: 537,140행, 161MB)
- 지역 키는 "seoul_gangnam"처럼 사람이 영문 이름을 짓는 대신, 원래 행정 코드를
  그대로 쓴다(예: "3220000") — 245개 넘는 시군구 이름을 전부 손으로 로마자화하면
  실수하기 쉽고, 코드는 공식 값이라 절대 안 겹친다. 한글 이름은 region_names.json에
  따로 둔다.
"""
from __future__ import annotations  # 로컬 Python 3.9에서도 `set[str] | None` 표기 쓰려고

import csv
import hashlib
import io
import json
import sys
import time
from pathlib import Path

import requests
from pyproj import Transformer

LIST_PAGE_URL = "https://file.localdata.go.kr/file/{slug}/info"
DOWNLOAD_URL = "https://file.localdata.go.kr/file/download/{slug}/info"
ACTIVE_STATUS = "영업/정상"

# 17개 시/도. "_ALL"을 쓰면 그 시/도 전체를 한 번에 받는다.
# 공공데이터포털 지역 선택 드롭다운에서 실측 확인(2026-08-08), 임의로 만들지 않았다.
UPPER_REGIONS = {
    "서울특별시": "6110000_ALL",
    "부산광역시": "6260000_ALL",
    "대구광역시": "6270000_ALL",
    "인천광역시": "6280000_ALL",
    "전남광주통합특별시": "6130000_ALL",
    "대전광역시": "6300000_ALL",
    "울산광역시": "6310000_ALL",
    "세종특별자치시": "5690000_ALL",
    "경기도": "6410000_ALL",
    "강원특별자치도": "6530000_ALL",
    "충청북도": "6430000_ALL",
    "충청남도": "6440000_ALL",
    "전북특별자치도": "6540000_ALL",
    "경상북도": "6470000_ALL",
    "경상남도": "6480000_ALL",
    "제주특별자치도": "6500000_ALL",
}

# 시/도 사이에 두는 대기시간(초). 17개 시/도 × 2개 카테고리 = 34번 요청 —
# 서울 하나만 받아도 161MB짜리 응답이라 상대 서버 부담이 이미 크다.
REGION_DELAY_SECONDS = 5

# slug는 실제 사이트에서 카테고리 선택 시 열리는 하위 페이지 경로에서 확인했다.
# types가 None이면 영업상태만 보고 전부 채택, 아니면 업태구분명이 목록에 있는 것만.
CATEGORIES = {
    "meal": {"slug": "general_restaurants", "types": None},
    "cafe": {"slug": "rest_cafes", "types": None},  # 실제 카테고리는 REST_CAFE_TYPE_TO_CATEGORY가 정함
}

# "휴게음식점"(rest_cafes) 안에서 업태구분명별로 실제 카테고리가 갈린다 — 코스에 쓸 만한
# 업태만 골랐고, 그중 패스트푸드는 "식사"라 cafe가 아니라 meal로 보낸다. 목록에 없는
# 업태(편의점·백화점·일반조리판매·기타 휴게음식점·푸드트럭·철도역구내·관광호텔 등)는
# 프랜차이즈인지 아닌지 표본만으로 확신할 수 없어서 이번엔 그대로 뺐다.
REST_CAFE_TYPE_TO_CATEGORY = {
    "커피숍": "cafe", "다방": "cafe", "전통찻집": "cafe", "떡카페": "cafe", "키즈카페": "cafe",
    "패스트푸드": "meal", "과자점": "cafe", "아이스크림": "cafe",
}

_transformer = Transformer.from_crs("EPSG:5174", "EPSG:4326", always_xy=True)


def _fetch_once(slug: str, org_code: str) -> bytes:
    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SmartRouteDataBot/1.0)"}
    list_url = LIST_PAGE_URL.format(slug=slug)
    # 1단계: 목록 페이지 방문 → 세션 쿠키 확보 (이게 없으면 2단계가 403)
    session.get(list_url, headers=headers, timeout=30)
    # 2단계: 실제 다운로드. 시/도 전체 다운로드는 응답이 수백MB일 수 있어 넉넉히 잡는다.
    resp = session.get(
        DOWNLOAD_URL.format(slug=slug),
        params={"orgCode": org_code},
        headers={**headers, "Referer": list_url},
        timeout=300,
    )
    resp.raise_for_status()
    if len(resp.content) < 1000:
        raise RuntimeError(f"응답이 너무 작음 ({len(resp.content)} bytes) — 차단됐을 가능성")
    return resp.content


def fetch_csv(slug: str, org_code: str) -> str:
    # 실측: GitHub Actions에서 연속 두 번 실행했을 때 1회는 연결 타임아웃, 1회는 정상 성공했다
    # (2026-08-08). 이 사이트가 가끔 응답을 안 준다는 뜻이라 한 번은 자동으로 더 시도한다.
    # solo-ops.md 정책대로 재시도는 최대 1회만 — 무한 재시도로 사이트에 부담 주지 않는다.
    try:
        raw = _fetch_once(slug, org_code)
    except requests.exceptions.RequestException as e:
        print(f"1차 시도 실패({e}), 15초 뒤 1회 재시도...", file=sys.stderr)
        time.sleep(15)
        raw = _fetch_once(slug, org_code)
    return raw.decode("euc-kr", errors="ignore")


def parse_places(
    csv_text: str,
    category: str,
    types: set[str] | None,
    type_to_category: dict[str, str] | None = None,
) -> dict[str, list[dict]]:
    """행마다 자기 시군구 코드(개방자치단체코드)로 결과를 나눠서 돌려준다.

    type_to_category가 있으면(휴게음식점처럼 업태구분명별로 실제 카테고리가 갈리는 경우)
    그 매핑에 없는 업태는 건너뛰고, 있으면 고정 category 대신 매핑된 값을 쓴다.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    by_region: dict[str, list[dict]] = {}
    seen_ids: set[str] = set()
    for row in reader:
        if row.get("영업상태명") != ACTIVE_STATUS:
            continue
        row_type = row.get("업태구분명", "").strip()
        if type_to_category is not None:
            if row_type not in type_to_category:
                continue
            row_category = type_to_category[row_type]
        else:
            if types is not None and row_type not in types:
                continue
            row_category = category
        x = (row.get("좌표정보(X)") or "").strip()
        y = (row.get("좌표정보(Y)") or "").strip()
        if not x or not y:
            continue
        place_id = row.get("관리번호", "").strip()
        name = row.get("사업장명", "").strip()
        region_code = row.get("개방자치단체코드", "").strip()
        # 도로명주소를 우선 쓴다 — 카카오톡 위치 템플릿 등 외부 공유에 표준 주소가 더 잘 맞는다.
        # 도로명주소가 비어있는 옛날 데이터는 지번주소로 대체한다.
        address = (row.get("도로명주소") or "").strip() or (row.get("지번주소") or "").strip()
        if not place_id or not name or not address or not region_code or place_id in seen_ids:
            continue
        seen_ids.add(place_id)
        try:
            lng, lat = _transformer.transform(float(x), float(y))
        except ValueError:
            continue
        by_region.setdefault(region_code, []).append({
            "id": place_id,
            "region": region_code,
            "category": row_category,
            "name": name,
            "address": address,
            "lat": round(lat, 7),
            "lng": round(lng, 7),
            "has": 0,
            "traits": 0,
            "verifiedClear": 0,
        })
    return by_region


def write_region(region: str, places: list[dict], out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"places_{region}.json"
    body = json.dumps(places, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(body)
    return {
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
        "count": len(places),
    }


def bump_version(existing: dict, region: str, new_sha: str) -> int:
    prev = existing.get("regions", {}).get(region)
    if prev and prev.get("sha256") == new_sha:
        return prev["v"]  # 내용이 안 바뀌었으면 버전도 그대로 — 앱이 재다운로드 안 하게
    return (prev["v"] + 1) if prev else 1


def fetch_upper_region(org_code: str) -> dict[str, list[dict]]:
    """시/도 하나를 카테고리별로 받아서, 실제 시군구 코드별로 합쳐서 돌려준다."""
    combined: dict[str, list[dict]] = {}
    for category, spec in CATEGORIES.items():
        print(f"  [{category}] 다운로드 중 (slug={spec['slug']}, orgCode={org_code})...", file=sys.stderr)
        csv_text = fetch_csv(spec["slug"], org_code)
        type_to_category = REST_CAFE_TYPE_TO_CATEGORY if category == "cafe" else None
        by_region = parse_places(csv_text, category, spec["types"], type_to_category)
        total = sum(len(v) for v in by_region.values())
        print(f"  [{category}] {total}건, {len(by_region)}개 시군구", file=sys.stderr)
        for region_code, places in by_region.items():
            combined.setdefault(region_code, []).extend(places)
    return combined


def main():
    out_dir = Path(__file__).resolve().parent.parent / "data"
    version_path = out_dir / "version.json"
    existing = json.loads(version_path.read_text()) if version_path.exists() else {"schema": 1, "regions": {}}

    regions_meta = dict(existing.get("regions", {}))
    failed: list[str] = []
    upper_items = list(UPPER_REGIONS.items())

    for i, (upper_name, upper_code) in enumerate(upper_items):
        print(f"[{upper_name}] 시작 (orgCode={upper_code})", file=sys.stderr)
        # 시/도 하나가 실패해도(사이트 타임아웃 등) 나머지는 계속 진행한다 —
        # 17곳 중 1곳 때문에 전체 갱신을 통째로 실패시키는 건 손해가 크다.
        try:
            combined = fetch_upper_region(upper_code)
        except Exception as e:
            print(f"[{upper_name}] 실패, 건너뜀: {e}", file=sys.stderr)
            failed.append(upper_name)
            continue

        if not combined:
            print(f"[{upper_name}] 결과 0건 — 파싱이 깨졌을 가능성, 건너뜀", file=sys.stderr)
            failed.append(upper_name)
            continue

        for region_code, places in combined.items():
            meta = write_region(region_code, places, out_dir)
            v = bump_version(existing, region_code, meta["sha256"])
            regions_meta[region_code] = {"v": v, "sha256": meta["sha256"], "bytes": meta["bytes"]}

        print(f"[{upper_name}] 완료: {len(combined)}개 시군구, 합계 {sum(len(v) for v in combined.values()):,}건", file=sys.stderr)

        if i < len(upper_items) - 1:
            time.sleep(REGION_DELAY_SECONDS)

    # existing을 그대로 이어 쓴다 — "regions"만 덮어쓰고 나머지 키(enrichRegions 등)는
    # 그대로 보존한다. 예전엔 {"schema":1,"regions":...}로 통째로 새로 써서, 이 스크립트를
    # 실행할 때마다 보강 데이터(enrichRegions)의 버전 기록이 통째로 지워지는 버그가 있었다
    # (2026-08-10 발견 — 버전이 리셋되면 이미 동기화한 기기가 "이미 최신"이라고 착각해서
    # 바뀐 보강 데이터를 다시 안 받아간다. 실물 데이터는 안 지워졌지만 버전 추적이 깨졌었다).
    existing["schema"] = 1
    existing["regions"] = regions_meta
    version_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n")

    if failed:
        print(f"\n실패한 시/도 {len(failed)}개: {', '.join(failed)}", file=sys.stderr)
        sys.exit(1)  # CI에서 실패를 놓치지 않게 — 단, 성공한 지역의 데이터는 이미 저장됐다


if __name__ == "__main__":
    main()

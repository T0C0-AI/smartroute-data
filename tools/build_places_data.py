#!/usr/bin/env python3
"""
공공데이터포털(행정안전부 지방행정 인허가 데이터)에서 지역별 장소 CSV를 받아
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
  코스에 쓸 만한 업태(커피숍·다방·전통찻집·떡카페·키즈카페)만 골랐다 —
  나머지를 "카페"로 잘못 라벨링하면 코스 추천이 엉뚱한 곳을 붙인다.
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

# 앱의 DEFAULT_REGION("seoul_gangnam")과 반드시 일치해야 한다 (MainActivity.kt 참고).
REGIONS = {
    "seoul_gangnam": "3220000",
}

# slug는 실제 사이트에서 카테고리 선택 시 열리는 하위 페이지 경로에서 확인했다.
# types가 None이면 영업상태만 보고 전부 채택, 아니면 업태구분명이 목록에 있는 것만.
CATEGORIES = {
    "meal": {"slug": "general_restaurants", "types": None},
    "cafe": {"slug": "rest_cafes", "types": {"커피숍", "다방", "전통찻집", "떡카페", "키즈카페"}},
}

_transformer = Transformer.from_crs("EPSG:5174", "EPSG:4326", always_xy=True)


def _fetch_once(slug: str, org_code: str) -> bytes:
    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SmartRouteDataBot/1.0)"}
    list_url = LIST_PAGE_URL.format(slug=slug)
    # 1단계: 목록 페이지 방문 → 세션 쿠키 확보 (이게 없으면 2단계가 403)
    session.get(list_url, headers=headers, timeout=30)
    # 2단계: 실제 다운로드
    resp = session.get(
        DOWNLOAD_URL.format(slug=slug),
        params={"orgCode": org_code},
        headers={**headers, "Referer": list_url},
        timeout=60,
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
        print(f"1차 시도 실패({e}), 10초 뒤 1회 재시도...", file=sys.stderr)
        time.sleep(10)
        raw = _fetch_once(slug, org_code)
    return raw.decode("euc-kr", errors="ignore")


def parse_places(csv_text: str, region: str, category: str, types: set[str] | None) -> list[dict]:
    reader = csv.DictReader(io.StringIO(csv_text))
    out = []
    seen_ids = set()
    for row in reader:
        if row.get("영업상태명") != ACTIVE_STATUS:
            continue
        if types is not None and row.get("업태구분명", "").strip() not in types:
            continue
        x = (row.get("좌표정보(X)") or "").strip()
        y = (row.get("좌표정보(Y)") or "").strip()
        if not x or not y:
            continue
        place_id = row.get("관리번호", "").strip()
        name = row.get("사업장명", "").strip()
        # 도로명주소를 우선 쓴다 — 카카오톡 위치 템플릿 등 외부 공유에 표준 주소가 더 잘 맞는다.
        # 도로명주소가 비어있는 옛날 데이터는 지번주소로 대체한다.
        address = (row.get("도로명주소") or "").strip() or (row.get("지번주소") or "").strip()
        if not place_id or not name or not address or place_id in seen_ids:
            continue
        seen_ids.add(place_id)
        try:
            lng, lat = _transformer.transform(float(x), float(y))
        except ValueError:
            continue
        out.append({
            "id": place_id,
            "region": region,
            "category": category,
            "name": name,
            "address": address,
            "lat": round(lat, 7),
            "lng": round(lng, 7),
            "has": 0,
            "traits": 0,
            "verifiedClear": 0,
        })
    return out


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


def main():
    out_dir = Path(__file__).resolve().parent.parent / "data"
    version_path = out_dir / "version.json"
    existing = json.loads(version_path.read_text()) if version_path.exists() else {"schema": 1, "regions": {}}

    regions_meta = dict(existing.get("regions", {}))
    for region, org_code in REGIONS.items():
        all_places: list[dict] = []
        for category, spec in CATEGORIES.items():
            print(f"[{region}/{category}] 다운로드 중 (slug={spec['slug']}, orgCode={org_code})...", file=sys.stderr)
            csv_text = fetch_csv(spec["slug"], org_code)
            places = parse_places(csv_text, region, category, spec["types"])
            print(f"[{region}/{category}] {len(places)}건", file=sys.stderr)
            all_places.extend(places)

        if len(all_places) < 100:
            raise RuntimeError(f"[{region}] 결과가 {len(all_places)}건뿐 — 파싱이 깨졌을 가능성, 배포 중단")

        meta = write_region(region, all_places, out_dir)
        v = bump_version(existing, region, meta["sha256"])
        regions_meta[region] = {"v": v, "sha256": meta["sha256"], "bytes": meta["bytes"]}
        print(f"[{region}] 합계 {meta['count']}건, v{v}, {meta['bytes']:,} bytes", file=sys.stderr)

    version_path.write_text(json.dumps({"schema": 1, "regions": regions_meta}, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()

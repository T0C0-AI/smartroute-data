#!/usr/bin/env python3
"""
한국관광공사 TourAPI(공공데이터포털)에서 주차 정보를 받아, 기존 places_<region>.json과
이름+좌표로 매칭해서 enrich_<region>.json을 만든다.

확인된 사실 (2026-08-08, 실제 API 호출로 검증):
- TourAPI는 관광공사가 별도로 등록·관리하는 곳만 있어서 커버리지가 낮다
  (강남구 실측: 우리 데이터 14,029곳 중 181곳, 약 1.3%).
- 그래도 커버된 곳은 데이터 품질이 좋다(표본 20곳 중 parkingfood 필드가 다 채워져 있었음).
- parkingfood 값은 "가능"/"불가능"/빈 문자열 세 종류이고, "가능 (발렛파킹)"처럼 부가 설명이
  붙기도 한다 — "가능"으로 시작하는지만 본다. "불가능"을 "가능"으로 잘못 읽으면 실제 조건보다
  더 위험한 오정보가 되니 접두어를 반드시 정확히 구분한다.
- kidsfacility 필드는 표본에서 전부 "0"이라 이번엔 신뢰할 신호가 아니어서 안 쓴다.
- 이름만으로 매칭하면 같은 상호가 여러 지점일 때 엉뚱한 곳과 매칭될 수 있어서, 이름이
  같고 좌표도 100m 이내로 가까운 경우만 매칭한다(오매칭이 데이터 없음보다 나쁘다).
- 이 스크립트는 지역 하나(예: 강남구)를 예시로 검증한 것이고, TourAPI의 areaCode/sigunguCode는
  우리 지역 코드(region_names.json)와 체계가 달라서 전국 230개 지역을 다 돌리려면
  그 매핑 표를 별도로 만들어야 한다 — 이번엔 강남구(areaCode=1, sigunguCode=1)만 확인했다.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BASE = "https://apis.data.go.kr/B551011/KorService2"
CONTENT_TYPE_FOOD = "39"
MATCH_RADIUS_M = 100
PARKING_BIT = 1 << 7  # Constraint.PARKING (core/Constraint.kt와 순서 반드시 일치)


def call(path: str, params: dict) -> dict:
    q = urllib.parse.urlencode(params, safe=",")
    url = f"{BASE}{path}?{q}"
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read())


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def fetch_tourapi_food(key: str, area_code: str, sigungu_code: str) -> list[dict]:
    first = call("/areaBasedList2", {
        "serviceKey": key, "MobileOS": "ETC", "MobileApp": "SmartRoute",
        "areaCode": area_code, "sigunguCode": sigungu_code, "contentTypeId": CONTENT_TYPE_FOOD,
        "numOfRows": "1", "pageNo": "1", "_type": "json",
    })
    total = first["response"]["body"]["totalCount"]
    resp = call("/areaBasedList2", {
        "serviceKey": key, "MobileOS": "ETC", "MobileApp": "SmartRoute",
        "areaCode": area_code, "sigunguCode": sigungu_code, "contentTypeId": CONTENT_TYPE_FOOD,
        "numOfRows": str(max(total, 1)), "pageNo": "1", "_type": "json",
    })
    items = resp["response"]["body"]["items"].get("item", [])
    return [items] if isinstance(items, dict) else items


def fetch_parking(key: str, content_id: str) -> str:
    detail = call("/detailIntro2", {
        "serviceKey": key, "MobileOS": "ETC", "MobileApp": "SmartRoute",
        "contentId": content_id, "contentTypeId": CONTENT_TYPE_FOOD, "_type": "json",
    })
    d_items = detail["response"]["body"]["items"].get("item", [])
    row = (d_items[0] if isinstance(d_items, list) else d_items) or {}
    return (row.get("parkingfood") or "").strip()


def build_region(key: str, region_code: str, area_code: str, sigungu_code: str) -> int:
    places_path = DATA_DIR / f"places_{region_code}.json"
    places = json.loads(places_path.read_text())
    by_name: dict[str, list[dict]] = {}
    for p in places:
        by_name.setdefault(p["name"], []).append(p)

    tour_items = fetch_tourapi_food(key, area_code, sigungu_code)
    print(f"TourAPI 음식점 {len(tour_items)}곳 (우리 데이터 {len(places)}곳 중 매칭 시도)", file=sys.stderr)

    enrich: dict[str, int] = {}
    for item in tour_items:
        name = item.get("title", "").strip()
        candidates = by_name.get(name)
        if not candidates:
            continue
        try:
            tx, ty = float(item["mapx"]), float(item["mapy"])
        except (KeyError, ValueError):
            continue
        matched = next(
            (c for c in candidates if haversine_m(c["lat"], c["lng"], ty, tx) <= MATCH_RADIUS_M),
            None,
        )
        if not matched:
            continue

        parking_text = fetch_parking(key, item.get("contentid", ""))
        time.sleep(0.1)  # 상대 서버 부담 줄이기
        if parking_text.startswith("가능"):
            enrich[matched["id"]] = enrich.get(matched["id"], 0) | PARKING_BIT

    out = [{"id": k, "has": v} for k, v in enrich.items()]
    out_path = DATA_DIR / f"enrich_{region_code}.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(f"[{region_code}] 매칭 성공 {len(out)}곳 → {out_path.name}", file=sys.stderr)
    return len(out)


def _load_key() -> str:
    key = os.environ.get("TOURS_API_KEY")
    if key:
        return key
    # 로컬 실행용: SmartRoute 앱 저장소의 .env에서 직접 읽는다(CI에서는 환경변수로 주입).
    env_path = Path.home() / "Desktop/GitHub/SmartRoute/.env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("TOURS_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("TOURS_API_KEY를 못 찾음 (환경변수 또는 SmartRoute/.env)")


def main():
    key = _load_key()
    # 이번엔 강남구(region_code=3220000, TourAPI areaCode=1/sigunguCode=1)만 검증.
    build_region(key, "3220000", "1", "1")


if __name__ == "__main__":
    main()

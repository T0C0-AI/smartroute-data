#!/usr/bin/env python3
"""
한국관광공사 TourAPI(공공데이터포털)에서 주차 정보를 받아, 기존 places_<region>.json과
이름+좌표로 매칭해서 enrich_<region>.json을 만든다. 전국 226개 지역을 대상으로 하되,
공공데이터포털 개발계정 트래픽 한도(하루 1,000건)를 넘지 않게 한 번 실행에 일부만 처리하고
나머지는 다음 실행(다음 날)에 이어서 한다.

확인된 사실 (2026-08-08, 실제 API 호출로 검증):
- TourAPI는 관광공사가 별도로 등록·관리하는 곳만 있어서 커버리지가 낮다
  (강남구 실측: 우리 데이터 14,029곳 중 181곳, 약 1.3%. 전국 226개 지역 합계로는
  음식점 8,504건 — 상세조회(detailIntro2)가 지역당 1건씩 필요해서 전국을 한 번에
  다 돌리면 호출이 약 9,000건 나온다. 개발계정 하루 한도 1,000건을 훌쩍 넘긴다).
- parkingfood 값은 "가능"/"불가능"/빈 문자열 세 종류다. "가능"으로 시작하는지만 본다.
  "불가능"을 "가능"으로 잘못 읽으면 실제 조건보다 더 위험한 오정보가 되니 접두어를
  반드시 정확히 구분한다.
- kidsfacility 필드는 표본에서 전부 "0"이라 이번엔 신뢰할 신호가 아니어서 안 쓴다.
- 이름만으로 매칭하면 같은 상호가 여러 지점일 때 엉뚱한 곳과 매칭될 수 있어서, 이름이
  같고 좌표도 100m 이내로 가까운 경우만 매칭한다(오매칭이 데이터 없음보다 나쁘다).
- area_mapping.json(TourAPI areaCode/sigunguCode ↔ 우리 지역코드)은 234개 TourAPI
  구/군 중 226개(97%)만 매칭됐다. 나머지 8개(인천 동구/서구/중구, 충북 청원군, 경남
  마산시·진해시, 제주 남제주군·북제주군)는 TourAPI가 옛날 행정구역(이미 오래전에
  다른 지역으로 통합된 곳들, 인천은 2026년 구 재편 이전 이름)을 그대로 쓰고 있어서
  현재 우리 데이터의 어떤 지역과도 이름이 안 맞는다 — 매칭 로직 문제가 아니라
  TourAPI 원본 데이터가 오래됐다.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
DATA_DIR = TOOLS_DIR.parent / "data"
MAPPING_PATH = TOOLS_DIR / "area_mapping.json"
PROGRESS_PATH = TOOLS_DIR / "enrichment_progress.json"
BASE = "https://apis.data.go.kr/B551011/KorService2"
CONTENT_TYPE_FOOD = "39"
MATCH_RADIUS_M = 100
PARKING_BIT = 1 << 7  # Constraint.PARKING (core/Constraint.kt와 순서 반드시 일치)

# 개발계정 하루 한도(1,000건)를 넘지 않게 여유를 두고 기본값을 잡는다.
# 실행할 때 ENRICHMENT_CALL_BUDGET 환경변수로 조절 가능.
CALL_BUDGET = int(os.environ.get("ENRICHMENT_CALL_BUDGET", "400"))


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


def fetch_tourapi_food(key: str, area_code: str, sigungu_code: str) -> tuple[list[dict], int]:
    first = call("/areaBasedList2", {
        "serviceKey": key, "MobileOS": "ETC", "MobileApp": "SmartRoute",
        "areaCode": area_code, "sigunguCode": sigungu_code, "contentTypeId": CONTENT_TYPE_FOOD,
        "numOfRows": "1", "pageNo": "1", "_type": "json",
    })
    total = first["response"]["body"]["totalCount"]
    if total == 0:
        return [], 1
    resp = call("/areaBasedList2", {
        "serviceKey": key, "MobileOS": "ETC", "MobileApp": "SmartRoute",
        "areaCode": area_code, "sigunguCode": sigungu_code, "contentTypeId": CONTENT_TYPE_FOOD,
        "numOfRows": str(total), "pageNo": "1", "_type": "json",
    })
    items = resp["response"]["body"]["items"].get("item", [])
    return ([items] if isinstance(items, dict) else items), 2


def fetch_parking(key: str, content_id: str) -> str:
    detail = call("/detailIntro2", {
        "serviceKey": key, "MobileOS": "ETC", "MobileApp": "SmartRoute",
        "contentId": content_id, "contentTypeId": CONTENT_TYPE_FOOD, "_type": "json",
    })
    d_items = detail["response"]["body"]["items"].get("item", [])
    row = (d_items[0] if isinstance(d_items, list) else d_items) or {}
    return (row.get("parkingfood") or "").strip()


def build_region(key: str, region_code: str, area_code: str, sigungu_code: str) -> tuple[int, int]:
    """반환값: (매칭된 곳 수, 이번에 실제로 쓴 API 호출 수)"""
    places_path = DATA_DIR / f"places_{region_code}.json"
    places = json.loads(places_path.read_text())
    by_name: dict[str, list[dict]] = {}
    for p in places:
        by_name.setdefault(p["name"], []).append(p)

    tour_items, calls_so_far = fetch_tourapi_food(key, area_code, sigungu_code)

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
        calls_so_far += 1
        time.sleep(0.1)
        if parking_text.startswith("가능"):
            enrich[matched["id"]] = enrich.get(matched["id"], 0) | PARKING_BIT

    out = [{"id": k, "has": v} for k, v in enrich.items()]
    out_path = DATA_DIR / f"enrich_{region_code}.json"
    body = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    out_path.write_bytes(body)

    version_path = DATA_DIR / "version.json"
    version = json.loads(version_path.read_text())
    version.setdefault("enrichRegions", {})
    sha = hashlib.sha256(body).hexdigest()
    prev = version["enrichRegions"].get(region_code)
    new_v = (prev["v"] + 1) if (prev and prev.get("sha256") != sha) else (prev["v"] if prev else 1)
    version["enrichRegions"][region_code] = {"v": new_v, "sha256": sha, "bytes": len(body)}
    version_path.write_text(json.dumps(version, ensure_ascii=False, indent=2) + "\n")

    print(f"[{region_code}] TourAPI {len(tour_items)}곳 중 매칭 {len(out)}곳, 호출 {calls_so_far}건", file=sys.stderr)
    return len(out), calls_so_far


def _load_key() -> str:
    key = os.environ.get("TOURS_API_KEY")
    if key:
        return key
    env_path = Path.home() / "Desktop/GitHub/SmartRoute/.env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("TOURS_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("TOURS_API_KEY를 못 찾음 (환경변수 또는 SmartRoute/.env)")


def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        return json.loads(PROGRESS_PATH.read_text())
    return {"done": []}


def save_progress(progress: dict) -> None:
    PROGRESS_PATH.write_text(json.dumps(progress, ensure_ascii=False, indent=2) + "\n")


def main():
    key = _load_key()
    mapping = json.loads(MAPPING_PATH.read_text())
    progress = load_progress()
    done = set(progress["done"])

    calls_used = 0
    processed_today = 0
    for region_code, info in mapping.items():
        if region_code in done:
            continue
        if calls_used >= CALL_BUDGET:
            break
        places_path = DATA_DIR / f"places_{region_code}.json"
        if not places_path.exists():
            print(f"[{region_code}] places_{region_code}.json 없음 — 스킵(완료 처리)", file=sys.stderr)
            done.add(region_code)
            progress["done"] = sorted(done)
            save_progress(progress)
            continue
        try:
            _, calls = build_region(key, region_code, info["areaCode"], info["sigunguCode"])
            calls_used += calls
            processed_today += 1
            done.add(region_code)
            progress["done"] = sorted(done)
            save_progress(progress)  # 지역 하나 끝날 때마다 저장 — 중간에 멈춰도 다음 실행이 이어감
        except Exception as e:
            print(f"[{region_code}] 실패, 다음 실행에서 재시도: {e}", file=sys.stderr)
            break

    remaining = len(mapping) - len(done)
    print(
        f"\n오늘 {processed_today}개 지역 처리(호출 {calls_used}건). "
        f"전체 {len(done)}/{len(mapping)} 완료, 남은 지역 {remaining}개",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
한국관광공사 무장애 여행 정보 API(KorWithService2)에서 출입구 접근성 정보를 받아
유모차 진입 조건을 채운다. 덤으로 babysparechair(여벌 유아용 보조의자) 정보로
아기의자 조건도 같이 보강한다.

확인된 사실 (2026-08-09, 실제 API 호출 + 공식 매뉴얼로 검증):
- 이 API는 처음엔 401/403이 났는데, data.go.kr에서 "활용신청"을 새로 넣어서 뚫었다
  (개발계정은 자동승인이라 즉시 됨).
- detailWithTour2(무장애정보조회)는 contentTypeId 파라미터를 안 받는다 — 넣으면
  "INVALID_REQUEST_PARAMETER_ERROR(contentTypeId)"가 난다. 공식 매뉴얼
  (한국관광공사_개방데이터_활용매뉴얼(무장애여행)_v4.3.docx)로 확인한 필수 파라미터는
  serviceKey/MobileOS/MobileApp/contentId뿐이다.
- 응답에 "stroller"라는 필드가 스키마에 있긴 한데, 실측(강남구 등 표본)에서 전부
  빈 값이라 못 쓴다. 대신 "exit"(주출입구 접근성) 필드에 "턱이 없어 휠체어 접근
  가능함", "경사로 있음" 같은 실제 문구가 들어있다 — 문턱 없는 출입구는 유모차도
  당연히 들어갈 수 있으니 이걸로 유모차 진입 여부를 판단한다(정확히 "유모차"라고
  적힌 필드는 아니지만, 물리적으로 같은 조건이라 안전한 대체 지표로 본다).
- "babysparechair"(여벌 유아용 보조의자) 필드는 실제 값이 있다("유아용보조의자
  있음") — 아기의자 조건에 그대로 쓸 수 있어서 같이 반영한다.
- exit 필드는 자유 텍스트라 "턱이 있어"/"불가"/"어려움" 같은 부정 표현이 있으면
  절대 긍정으로 안 읽는다.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
DATA_DIR = TOOLS_DIR.parent / "data"
MAPPING_PATH = TOOLS_DIR / "area_mapping.json"
PROGRESS_PATH = TOOLS_DIR / "stroller_progress.json"
BASE = "https://apis.data.go.kr/B551011/KorWithService2"
CONTENT_TYPE_FOOD = "39"

STROLLER_BIT = 1 << 5  # Constraint.STROLLER
HIGHCHAIR_BIT = 1 << 4  # Constraint.HIGHCHAIR

POSITIVE_EXIT_WORDS = ("턱이 없어", "단차가 없어", "경사로")
NEGATIVE_EXIT_WORDS = ("턱이 있어", "불가", "어려움", "단차가 있어")

CALL_BUDGET = int(__import__("os").environ.get("STROLLER_CALL_BUDGET", "400"))


def call(path: str, params: dict) -> dict:
    q = urllib.parse.urlencode(params, safe=",")
    url = f"{BASE}{path}?{q}"
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read())


def fetch_food_list(area_code: str, sigungu_code: str) -> tuple[list[dict], int]:
    key = _key
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


def bits_from_detail(content_id: str) -> int:
    detail = call("/detailWithTour2", {
        "serviceKey": _key, "MobileOS": "ETC", "MobileApp": "SmartRoute",
        "contentId": content_id, "_type": "json",
    })
    d_items = detail["response"]["body"]["items"].get("item", [])
    row = (d_items[0] if isinstance(d_items, list) else d_items) or {}

    bits = 0
    exit_text = (row.get("exit") or "").strip()
    if exit_text and not any(neg in exit_text for neg in NEGATIVE_EXIT_WORDS):
        if any(pos in exit_text for pos in POSITIVE_EXIT_WORDS):
            bits |= STROLLER_BIT
    if (row.get("babysparechair") or "").strip():
        bits |= HIGHCHAIR_BIT
    return bits


def build_region(region_code: str, area_code: str, sigungu_code: str) -> tuple[int, int]:
    places_path = DATA_DIR / f"places_{region_code}.json"
    places = json.loads(places_path.read_text())
    by_name: dict[str, list[dict]] = {}
    for p in places:
        by_name.setdefault(p["name"], []).append(p)

    food_items, calls_so_far = fetch_food_list(area_code, sigungu_code)

    enrich_updates: dict[str, int] = {}
    for item in food_items:
        name = item.get("title", "").strip()
        if name not in by_name or len(by_name[name]) != 1:
            continue  # 이름이 우리 데이터에 없거나 중복이면 오매칭 위험 — 건너뜀
        bits = bits_from_detail(item.get("contentid", ""))
        calls_so_far += 1
        time.sleep(0.1)
        if bits:
            enrich_updates[by_name[name][0]["id"]] = bits

    if not enrich_updates:
        return 0, calls_so_far

    enrich_path = DATA_DIR / f"enrich_{region_code}.json"
    existing = json.loads(enrich_path.read_text()) if enrich_path.exists() else []
    by_id = {e["id"]: e["has"] for e in existing}
    for pid, bits in enrich_updates.items():
        by_id[pid] = by_id.get(pid, 0) | bits

    out = [{"id": k, "has": v} for k, v in by_id.items()]
    body = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    enrich_path.write_bytes(body)

    version_path = DATA_DIR / "version.json"
    version = json.loads(version_path.read_text())
    version.setdefault("enrichRegions", {})
    sha = hashlib.sha256(body).hexdigest()
    prev = version["enrichRegions"].get(region_code)
    new_v = (prev["v"] + 1) if (prev and prev.get("sha256") != sha) else (prev["v"] if prev else 1)
    version["enrichRegions"][region_code] = {"v": new_v, "sha256": sha, "bytes": len(body)}
    version_path.write_text(json.dumps(version, ensure_ascii=False, indent=2) + "\n")

    print(f"[{region_code}] 유모차/아기의자 보강 {len(enrich_updates)}곳, 호출 {calls_so_far}건", file=sys.stderr)
    return len(enrich_updates), calls_so_far


def _load_key() -> str:
    import os
    key = os.environ.get("TOURS_API_KEY")
    if key:
        return key
    env_path = Path.home() / "Desktop/GitHub/SmartRoute/.env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("TOURS_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("TOURS_API_KEY를 못 찾음")


def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        return json.loads(PROGRESS_PATH.read_text())
    return {"done": []}


def save_progress(progress: dict) -> None:
    PROGRESS_PATH.write_text(json.dumps(progress, ensure_ascii=False, indent=2) + "\n")


_key = None


def main():
    global _key
    _key = _load_key()
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
            done.add(region_code)
            progress["done"] = sorted(done)
            save_progress(progress)
            continue
        try:
            _, calls = build_region(region_code, info["areaCode"], info["sigunguCode"])
            calls_used += calls
            processed_today += 1
            done.add(region_code)
            progress["done"] = sorted(done)
            save_progress(progress)
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

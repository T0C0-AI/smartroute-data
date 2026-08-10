#!/usr/bin/env python3
"""
맥도날드 공식 알레르기정보 API에서 메뉴별 알레르기 유발 식재료를 받아, 우리 매장
데이터의 맥도날드 지점에 메뉴 단위로 붙인다.

확인된 사실 (2026-08-10):
- https://www.mcdonalds.co.kr/api/v1/kor/product/allergy 는 로그인·키 없이 GET으로
  바로 받을 수 있는 공개 JSON API다(맥도날드가 어린이 식생활안전관리 특별법에 따라
  법적으로 공개 의무가 있는 정보). 189개 메뉴, 메뉴명(menuName)·상품코드(plu)·
  알레르기 유발가능 식재료 설명(allergyInfo) 필드를 준다.
- allergyInfo는 세트메뉴처럼 여러 구성품이 있으면 "빅맥® (난류, 우유, 대두, 밀,
  쇠고기), 후렌치 후라이 (대두), 케첩 (대두, 토마토)"처럼 구성품별로 괄호가 나뉘어
  있고, 단품이면 "난류, 우유, 대두, 밀, 쇠고기"처럼 그냥 콤마 목록이다. 구조가
  달라서 괄호를 정교하게 파싱하는 대신, 우리가 필요한 두 알레르기 단어(땅콩·갑각류
  계열)가 그 텍스트 안에 있는지만 본다 — 이 조건에서는 구조와 무관하게 항상 맞는
  방법이라 이쪽이 더 안전하다.
- "갑각류 알레르기"라는 조건 이름과 맞추려고, 새우·게(진짜 갑각류)뿐 아니라 조개·굴
  (연체동물이지만 맥도날드 자체가 이 넷을 같이 나열함, 알레르기 있는 사람이 "갑각류"를
  느슨하게 해산물 전반의 의미로 쓰는 경우가 흔함)도 전부 있으면 "확인 안 됨"으로 본다
  — 안전 조건이라 좁게 잡아서 놓치는 것보다 넓게 잡아서 과소평가하는 게 낫다.
- 맥도날드 전 지점이 같은 전국 메뉴를 쓴다고 보고(지역별 메뉴 차이를 알려주는 자료를
  찾지 못함), 이름에 "맥도날드"가 들어간 모든 매장에 같은 메뉴 189개를 그대로 붙인다.
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
DATA_DIR = TOOLS_DIR.parent / "data"
ALLERGY_API = "https://www.mcdonalds.co.kr/api/v1/kor/product/allergy"

NO_PEANUT_BIT = 1 << 2  # Constraint.NO_PEANUT (core/Constraint.kt와 순서 반드시 일치)
NO_SHELLFISH_BIT = 1 << 3  # Constraint.NO_SHELLFISH

PEANUT_WORDS = ("땅콩",)
SHELLFISH_WORDS = ("새우", "게", "조개", "굴")


def fetch_menu() -> list[dict]:
    req = urllib.request.Request(ALLERGY_API, headers={"User-Agent": "Mozilla/5.0 (compatible; SmartRouteDataBot/1.0)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read())
    return body["resultObject"]["list"]


def build_menu_items(raw_menu: list[dict]) -> list[dict]:
    """플레이스마다 재사용할 "기본" 메뉴 목록(placeId 없는 상태)을 만든다."""
    items = []
    seen_plu: set[str] = set()
    for m in raw_menu:
        plu = (m.get("plu") or "").strip()
        name = (m.get("menuName") or "").strip()
        if not plu or not name or plu in seen_plu:
            continue
        seen_plu.add(plu)
        info = m.get("allergyInfo") or ""
        allergen_free = 0
        if not any(w in info for w in PEANUT_WORDS):
            allergen_free |= NO_PEANUT_BIT
        if not any(w in info for w in SHELLFISH_WORDS):
            allergen_free |= NO_SHELLFISH_BIT
        items.append({"plu": plu, "name": name, "allergenFree": allergen_free})
    return items


def main():
    raw_menu = fetch_menu()
    base_items = build_menu_items(raw_menu)
    peanut_free = sum(1 for i in base_items if i["allergenFree"] & NO_PEANUT_BIT)
    shellfish_free = sum(1 for i in base_items if i["allergenFree"] & NO_SHELLFISH_BIT)
    print(
        f"맥도날드 메뉴 {len(base_items)}개 — 땅콩 없음 {peanut_free}개, 갑각류/조개류 없음 {shellfish_free}개",
        file=sys.stderr,
    )

    version_path = DATA_DIR / "version.json"
    version = json.loads(version_path.read_text())
    version.setdefault("enrichRegions", {})

    regions_touched = 0
    places_matched = 0
    for places_path in sorted(DATA_DIR.glob("places_*.json")):
        region_code = places_path.stem.removeprefix("places_")
        places = json.loads(places_path.read_text())
        mcd_places = [p for p in places if "맥도날드" in p["name"]]
        if not mcd_places:
            continue

        enrich_path = DATA_DIR / f"enrich_{region_code}.json"
        existing = json.loads(enrich_path.read_text()) if enrich_path.exists() else []
        by_id = {e["id"]: e for e in existing}

        for p in mcd_places:
            entry = by_id.setdefault(p["id"], {"id": p["id"]})
            entry["menuItems"] = [
                {"id": f"{p['id']}-mcd-{it['plu']}", "name": it["name"], "allergenFree": it["allergenFree"]}
                for it in base_items
            ]
        places_matched += len(mcd_places)

        out = list(by_id.values())
        body = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        enrich_path.write_bytes(body)

        sha = hashlib.sha256(body).hexdigest()
        prev = version["enrichRegions"].get(region_code)
        new_v = (prev["v"] + 1) if (prev and prev.get("sha256") != sha) else (prev["v"] if prev else 1)
        version["enrichRegions"][region_code] = {"v": new_v, "sha256": sha, "bytes": len(body)}
        regions_touched += 1
        print(f"[{region_code}] 맥도날드 {len(mcd_places)}곳에 메뉴 {len(base_items)}개씩 반영", file=sys.stderr)

    version_path.write_text(json.dumps(version, ensure_ascii=False, indent=2) + "\n")
    print(f"\n총 {regions_touched}개 지역, 맥도날드 {places_matched}곳에 알레르기 메뉴 데이터 반영", file=sys.stderr)


if __name__ == "__main__":
    main()

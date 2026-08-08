#!/usr/bin/env python3
"""
식품안전나라(식약처)의 "반려동물 동반 가능 업소 현황"을 받아 반려동물 실내 조건을 채운다.
https://www.foodsafetykorea.go.kr/portal/petKorea.do

확인된 사실 (2026-08-09, 실제로 페이지를 열어서 확인함):
- 2026.1.2. 「식품위생법 시행규칙」 개정(시행 2026.3.1.)으로 "반려동물 동반출입 음식점"
  등록 제도가 새로 생겼다. 이 페이지는 그 등록 현황을 실시간 DB 기준으로 보여준다 —
  TourAPI처럼 제3자가 고른 목록이 아니라 정부 등록 제도 자체의 공식 데이터다.
- 전국 2,440개소(2026-08-09 기준), 별도 API 없이 이 페이지 하나(GET 한 번)에 표(업소명·
  업종·지역·업소주소)가 통째로 서버 렌더링되어 있다 — TourAPI처럼 지역별로 나눠 부를
  필요도, 하루 호출 한도도 없다. 그래서 전국을 한 번에 다 처리한다.
- "지역" 컬럼은 시/도 단위까지만 있고 구/군은 없어서, "업소주소"에서 직접
  시/도+구/군을 추출해 우리 지역코드에 매칭한다.
- 이름만 겹치는 경우가(같은 상호 여러 지점) 있을 수 있어서, 지역코드로 좁힌 뒤에도
  이름이 유일하게 매칭될 때만 반영한다(강남구 표본 30곳 검증 시 이름 겹침 0건이었지만,
  전국 규모에서는 있을 수 있어 안전장치로 남겨둔다).
- PET_INDOOR 조건은 CONVENIENCE가 아니라 NEED 등급이라 이미 다른 조건(주차 등)과 같은
  enrich_<region>.json에 비트만 OR로 더한다 — 기존 파일을 덮어쓰지 않고 병합한다.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
DATA_DIR = TOOLS_DIR.parent / "data"
REGION_NAMES_URL = "https://raw.githubusercontent.com/T0C0-AI/smartroute-data/main/data/region_names.json"
PET_PAGE_URL = "https://www.foodsafetykorea.go.kr/portal/petKorea.do"
PET_INDOOR_BIT = 1 << 6  # Constraint.PET_INDOOR

# 주소 첫 토큰(전체 명칭) -> 우리 region_names.json에서 쓰는 짧은 접두사.
# RegionDropdown.kt의 PROVINCE_PREFIXES와 동일한 체계를 쓴다.
PROVINCE_FULLNAME_TO_PREFIX = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천",
    "광주광역시": "전남광주", "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종",
    "경기도": "경기", "강원특별자치도": "강원", "강원도": "강원",
    "충청북도": "충북", "충청남도": "충남", "전북특별자치도": "전북", "전라북도": "전북",
    "전라남도": "전남광주", "경상북도": "경북", "경상남도": "경남",
    "제주특별자치도": "제주",
}

ADDRESS_RE = re.compile(r"^(\S+?(?:특별시|광역시|특별자치시|특별자치도|도))\s*(\S+?(?:시|군|구))")


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; SmartRouteDataBot/1.0)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def region_code_for_address(address: str, region_names: dict[str, str]) -> str | None:
    m = ADDRESS_RE.match(address)
    if not m:
        return None
    prov_full, gugun = m.group(1), m.group(2)
    prefix = PROVINCE_FULLNAME_TO_PREFIX.get(prov_full)
    if not prefix:
        return None
    target = prefix + gugun
    candidates = [code for code, name in region_names.items() if name.startswith(target)]
    if len(candidates) == 1:
        return candidates[0]
    return None


def main():
    region_names = json.loads(fetch_text(REGION_NAMES_URL))
    html = fetch_text(PET_PAGE_URL)
    raw_rows = re.findall(
        r'<td data-label="연번">(\d+)</td>\s*'
        r'<td data-label="업소명">([^<]*)</td>\s*'
        r'<td data-label="업종">([^<]*)</td>\s*'
        r'<td data-label="지역">([^<]*)</td>\s*'
        r'<td data-label="업소주소">([^<]*)</td>',
        html,
    )
    import html as html_mod
    pet_places = [
        {"name": html_mod.unescape(name).strip(), "address": html_mod.unescape(address).strip()}
        for _, name, _, _, address in raw_rows
    ]
    print(f"식약처 반려동물 동반 업소 {len(pet_places)}건 파싱", file=sys.stderr)

    by_region: dict[str, list[dict]] = {}
    unmatched_region = 0
    for p in pet_places:
        code = region_code_for_address(p["address"], region_names)
        if code is None:
            unmatched_region += 1
            continue
        by_region.setdefault(code, []).append(p)
    print(f"지역코드 매칭 {len(pet_places) - unmatched_region}건, 실패 {unmatched_region}건", file=sys.stderr)

    version_path = DATA_DIR / "version.json"
    version = json.loads(version_path.read_text())
    version.setdefault("enrichRegions", {})

    total_matched_places = 0
    regions_touched = 0
    for region_code, candidates in by_region.items():
        places_path = DATA_DIR / f"places_{region_code}.json"
        if not places_path.exists():
            continue
        places = json.loads(places_path.read_text())
        by_name: dict[str, list[dict]] = {}
        for pl in places:
            by_name.setdefault(pl["name"], []).append(pl)

        matched_ids: set[str] = set()
        for c in candidates:
            found = by_name.get(c["name"])
            if found and len(found) == 1:  # 이름이 유일하게 매칭될 때만(오매칭 방지)
                matched_ids.add(found[0]["id"])

        if not matched_ids:
            continue

        enrich_path = DATA_DIR / f"enrich_{region_code}.json"
        existing = json.loads(enrich_path.read_text()) if enrich_path.exists() else []
        by_id = {e["id"]: e["has"] for e in existing}
        for pid in matched_ids:
            by_id[pid] = by_id.get(pid, 0) | PET_INDOOR_BIT

        out = [{"id": k, "has": v} for k, v in by_id.items()]
        body = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        enrich_path.write_bytes(body)

        sha = hashlib.sha256(body).hexdigest()
        prev = version["enrichRegions"].get(region_code)
        new_v = (prev["v"] + 1) if (prev and prev.get("sha256") != sha) else (prev["v"] if prev else 1)
        version["enrichRegions"][region_code] = {"v": new_v, "sha256": sha, "bytes": len(body)}

        total_matched_places += len(matched_ids)
        regions_touched += 1
        print(f"[{region_code}] 반려동물 동반 {len(matched_ids)}곳 반영", file=sys.stderr)

    version_path.write_text(json.dumps(version, ensure_ascii=False, indent=2) + "\n")
    print(f"\n총 {regions_touched}개 지역, {total_matched_places}곳에 반려동물 동반 조건 반영", file=sys.stderr)


if __name__ == "__main__":
    main()

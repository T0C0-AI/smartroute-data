#!/usr/bin/env python3
"""
한국문화정보원_전국 세계 음식점 데이터(공공데이터포털)의 "유아의자 대여여부" 필드로
아기의자 조건을 채운다. tools/source_snapshots/에 있는 CSV를 읽는다(원본 URL:
https://www.data.go.kr/data/15111398/fileData.do).

확인된 사실 (2026-08-09):
- 이 파일은 로그인해야 다운로드되는 데이터라(자동변환된 오픈API도 별도 활용신청 필요,
  401 확인함) 스크립트가 매번 자동으로 새로 받아올 수 없다. 그래서 다운로드한 CSV를
  저장소에 스냅샷으로 같이 커밋해 재현 가능하게 해뒀다 — 원본이 갱신되면 사람이 다시
  받아서 이 파일을 교체해야 한다("업데이트 주기: 수시(1회성 데이터)"라 자주 바뀌진 않음).
- 인코딩은 EUC-KR이다(공공데이터 CSV에서 흔함, UTF-8로 읽으면 깨진다).
- 전체 9,502곳 중 "유아의자 대여여부"가 Y인 곳은 92곳뿐이다(약 1%). 작지만 실제로
  확인된 데이터라 반영한다.
- "무료주차 가능여부"는 9,502곳 전부 Y로 찍혀있다 — 실제 신호가 아니라 데이터 자체의
  기본값/오류로 보여서 안 쓴다(이미 TourAPI로 주차는 따로 확인해서 반영 중).
- 위도/경도가 있어서 TourAPI 때처럼 이름+좌표(100m 이내)로 매칭한다.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
DATA_DIR = TOOLS_DIR.parent / "data"
CSV_PATH = TOOLS_DIR / "source_snapshots" / "한국문화정보원_전국 세계 음식점 데이터_20221130.csv"
MATCH_RADIUS_M = 100
HIGHCHAIR_BIT = 1 << 4  # Constraint.HIGHCHAIR

PROVINCE_FULLNAME_TO_PREFIX = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천",
    "광주광역시": "전남광주", "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종",
    "경기도": "경기", "강원특별자치도": "강원", "강원도": "강원",
    "충청북도": "충북", "충청남도": "충남", "전북특별자치도": "전북", "전라북도": "전북",
    "전라남도": "전남광주", "경상북도": "경북", "경상남도": "경남",
    "제주특별자치도": "제주",
}


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def region_code_for(sido: str, sigungu: str, region_names: dict[str, str]) -> str | None:
    prefix = PROVINCE_FULLNAME_TO_PREFIX.get(sido)
    if not prefix:
        return None
    target = prefix + sigungu
    candidates = [code for code, name in region_names.items() if name.startswith(target)]
    return candidates[0] if len(candidates) == 1 else None


def main():
    region_names = json.loads((DATA_DIR / "region_names.json").read_text())

    with CSV_PATH.open(encoding="euc-kr", errors="replace") as f:
        rows = list(csv.DictReader(f))
    highchair_rows = [r for r in rows if r.get("유아의자 대여여부") == "Y"]
    print(f"전국 유아의자 대여 가능 {len(highchair_rows)}곳 (전체 {len(rows)}곳 중)", file=sys.stderr)

    by_region: dict[str, list[dict]] = {}
    for r in highchair_rows:
        code = region_code_for(r["시도 명칭"], r["시군구 명칭"], region_names)
        if code is None:
            continue
        by_region.setdefault(code, []).append(r)

    version_path = DATA_DIR / "version.json"
    version = json.loads(version_path.read_text())
    version.setdefault("enrichRegions", {})

    total_matched = 0
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
            try:
                clat, clng = float(c["위도"]), float(c["경도"])
            except ValueError:
                continue
            cand_places = by_name.get(c["시설명"], [])
            hit = next((p for p in cand_places if haversine_m(p["lat"], p["lng"], clat, clng) <= MATCH_RADIUS_M), None)
            if hit:
                matched_ids.add(hit["id"])

        if not matched_ids:
            continue

        enrich_path = DATA_DIR / f"enrich_{region_code}.json"
        existing = json.loads(enrich_path.read_text()) if enrich_path.exists() else []
        by_id = {e["id"]: e["has"] for e in existing}
        for pid in matched_ids:
            by_id[pid] = by_id.get(pid, 0) | HIGHCHAIR_BIT

        out = [{"id": k, "has": v} for k, v in by_id.items()]
        body = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        enrich_path.write_bytes(body)

        sha = hashlib.sha256(body).hexdigest()
        prev = version["enrichRegions"].get(region_code)
        new_v = (prev["v"] + 1) if (prev and prev.get("sha256") != sha) else (prev["v"] if prev else 1)
        version["enrichRegions"][region_code] = {"v": new_v, "sha256": sha, "bytes": len(body)}

        total_matched += len(matched_ids)
        regions_touched += 1
        print(f"[{region_code}] 아기의자 {len(matched_ids)}곳 반영", file=sys.stderr)

    version_path.write_text(json.dumps(version, ensure_ascii=False, indent=2) + "\n")
    print(f"\n총 {regions_touched}개 지역, {total_matched}곳에 아기의자 조건 반영", file=sys.stderr)


if __name__ == "__main__":
    main()

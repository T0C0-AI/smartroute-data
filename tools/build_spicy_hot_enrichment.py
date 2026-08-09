#!/usr/bin/env python3
"""
매운 음식 제외·뜨거운 음식 제외 조건을, 상호명 키워드로 추정해서 채운다.

확인된 사실 (2026-08-09):
- 정부 공공데이터·구글 Places API·카카오 로컬 API·네이버 지역검색 API·오픈스트리트맵을
  전부 확인했지만 "이 가게가 매운 음식/뜨거운 음식 위주인지"를 담은 실제 데이터는 어디에도
  없다(docs/ROADMAP.md 참고). 이 두 조건은 실측 데이터가 존재하지 않아서, 상호명 키워드로
  추정하는 것 외에 다른 방법이 없다.
- 우리 places 데이터에는 상호명(name)·주소(address)·대분류(category: meal/cafe)만 있고
  세부 음식 카테고리(한식/중식 등)가 없어서, 신호로 쓸 수 있는 건 상호명 텍스트뿐이다.
- 이건 실측이 아니라 추정이라서, 반영된 곳은 앱 화면에 "확인됨"이 아니라 "AI 추정" 배지로
  따로 표시한다(core/Constraint.kt의 aiEstimated 플래그, app의 BrutalIconChip 참고). 이
  스크립트는 그 추정값을 만드는 쪽이고, "확실친 않다"는 걸 UI에서 감추지 않는 게 원칙이다.
- "매운 음식 제외"는 놓치는 쪽(진짜 매운 곳을 못 잡아내는 것)이 더 위험하다 — 그렇다고
  "탕"·"국"처럼 짧고 흔한 글자를 그대로 매칭하면 탕후루·미역국 같은 무관한 가게까지 오탐되니,
  구체적인 단어 단위로만 매칭해서 오탐(과다 배제)과 누락(과소 배제) 사이에서 균형을 잡았다.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
DATA_DIR = TOOLS_DIR.parent / "data"

NO_SPICY_BIT = 1 << 0  # Constraint.NO_SPICY (core/Constraint.kt와 순서 반드시 일치)
NO_HOT_TEMP_BIT = 1 << 1  # Constraint.NO_HOT_TEMP

# 상호명에 포함되면 "매운맛이 주력"이라고 볼 만큼 신호가 강한 단어만 골랐다.
# "고추"·"짬뽕"·"낙지"처럼 안 매운 메뉴도 흔한 단어는 일부러 뺐다 — 놓치는 것보다
# 안 매운 가게를 잘못 걸러내는 게 사용자 경험상 더 나쁘다고 판단했다.
SPICY_KEYWORDS = [
    # 주의: 제주 지명 "마라도"와 겹치는 문제 때문에 "마라" 단독은 빼고 구체적인 복합어만 썼다
    # ("마라도순대"처럼 지명이 들어간 상호가 매운맛과 무관하게 오탐되는 걸 확인 후 조정).
    "마라탕", "마라샹궈", "마라룽샤", "마라향",
    "불닭", "불냉면", "불족발", "불곱창", "불막창", "불짬뽕",
    "매운", "매콤", "청양고추",
    "땡초", "화끈한", "화끈",
    "닭발", "무뼈닭발",
    "쭈꾸미볶음", "쭈꾸미",
]

# "탕"·"국" 같은 한 글자짜리 흔한 접미사는 탕후루·미역국처럼 무관한 상호까지 잡아서 뺐다.
# 뜨거운 국물이 확실한 구체적 메뉴명만 나열했다.
HOT_KEYWORDS = [
    "국밥", "순대국", "돼지국밥", "곰탕", "설렁탕", "육개장",
    "감자탕", "갈비탕", "삼계탕", "매운탕", "동태탕", "알탕", "꽃게탕", "해물탕", "대구탕",
    "전골", "샤브샤브", "훠궈",
    "우동", "라멘",
    "닭볶음탕", "닭한마리",
]


def classify(name: str) -> int:
    bits = 0
    if any(k in name for k in SPICY_KEYWORDS):
        bits |= NO_SPICY_BIT
    if any(k in name for k in HOT_KEYWORDS):
        bits |= NO_HOT_TEMP_BIT
    return bits


def build_region(region_code: str) -> tuple[int, int]:
    """반환값: (매운맛 추정 곳 수, 뜨거운 음식 추정 곳 수)"""
    places_path = DATA_DIR / f"places_{region_code}.json"
    places = json.loads(places_path.read_text())

    new_traits: dict[str, int] = {}
    for p in places:
        bits = classify(p["name"])
        if bits:
            new_traits[p["id"]] = bits

    if not new_traits:
        return 0, 0

    enrich_path = DATA_DIR / f"enrich_{region_code}.json"
    existing = json.loads(enrich_path.read_text()) if enrich_path.exists() else []
    has_by_id = {e["id"]: e.get("has", 0) for e in existing if e.get("has", 0)}
    traits_by_id = {e["id"]: e.get("traits", 0) for e in existing if e.get("traits", 0)}
    for pid, bits in new_traits.items():
        traits_by_id[pid] = traits_by_id.get(pid, 0) | bits

    all_ids = sorted(set(has_by_id) | set(traits_by_id))
    out = []
    for pid in all_ids:
        entry: dict = {"id": pid}
        if has_by_id.get(pid):
            entry["has"] = has_by_id[pid]
        if traits_by_id.get(pid):
            entry["traits"] = traits_by_id[pid]
        out.append(entry)

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

    spicy_n = sum(1 for b in new_traits.values() if b & NO_SPICY_BIT)
    hot_n = sum(1 for b in new_traits.values() if b & NO_HOT_TEMP_BIT)
    return spicy_n, hot_n


def main():
    places_files = sorted(DATA_DIR.glob("places_*.json"))
    total_spicy = 0
    total_hot = 0
    regions_touched = 0
    for pf in places_files:
        region_code = pf.stem.removeprefix("places_")
        spicy_n, hot_n = build_region(region_code)
        if spicy_n or hot_n:
            regions_touched += 1
            total_spicy += spicy_n
            total_hot += hot_n

    print(
        f"{len(places_files)}개 지역 처리, {regions_touched}개 지역에 반영. "
        f"매운맛 추정 {total_spicy}곳, 뜨거운 음식 추정 {total_hot}곳",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

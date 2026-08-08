#!/usr/bin/env python3
"""
공공데이터포털(행정안전부 지방행정 인허가 데이터)에서 지역별 일반음식점 CSV를 받아
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
"""
import csv
import hashlib
import io
import json
import sys
import time
from pathlib import Path

import requests
from pyproj import Transformer

LIST_PAGE_URL = "https://file.localdata.go.kr/file/general_restaurants/info"
DOWNLOAD_URL = "https://file.localdata.go.kr/file/download/general_restaurants/info"
ACTIVE_STATUS = "영업/정상"

# 앱의 DEFAULT_REGION("seoul_gangnam")과 반드시 일치해야 한다 (MainActivity.kt 참고).
REGIONS = {
    "seoul_gangnam": "3220000",
}

_transformer = Transformer.from_crs("EPSG:5174", "EPSG:4326", always_xy=True)


def _fetch_once(org_code: str) -> bytes:
    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SmartRouteDataBot/1.0)"}
    # 1단계: 목록 페이지 방문 → 세션 쿠키 확보 (이게 없으면 2단계가 403)
    session.get(LIST_PAGE_URL, headers=headers, timeout=30)
    # 2단계: 실제 다운로드
    resp = session.get(
        DOWNLOAD_URL,
        params={"orgCode": org_code},
        headers={**headers, "Referer": LIST_PAGE_URL},
        timeout=60,
    )
    resp.raise_for_status()
    if len(resp.content) < 1000:
        raise RuntimeError(f"응답이 너무 작음 ({len(resp.content)} bytes) — 차단됐을 가능성")
    return resp.content


def fetch_region_csv(org_code: str) -> str:
    # 실측: GitHub Actions에서 연속 두 번 실행했을 때 1회는 연결 타임아웃, 1회는 정상 성공했다
    # (2026-08-08). 이 사이트가 가끔 응답을 안 준다는 뜻이라 한 번은 자동으로 더 시도한다.
    # solo-ops.md 정책대로 재시도는 최대 1회만 — 무한 재시도로 사이트에 부담 주지 않는다.
    try:
        raw = _fetch_once(org_code)
    except requests.exceptions.RequestException as e:
        print(f"1차 시도 실패({e}), 10초 뒤 1회 재시도...", file=sys.stderr)
        time.sleep(10)
        raw = _fetch_once(org_code)
    return raw.decode("euc-kr", errors="ignore")


def parse_places(csv_text: str, region: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(csv_text))
    out = []
    seen_ids = set()
    for row in reader:
        if row.get("영업상태명") != ACTIVE_STATUS:
            continue
        x = (row.get("좌표정보(X)") or "").strip()
        y = (row.get("좌표정보(Y)") or "").strip()
        if not x or not y:
            continue
        place_id = row.get("관리번호", "").strip()
        name = row.get("사업장명", "").strip()
        if not place_id or not name or place_id in seen_ids:
            continue
        seen_ids.add(place_id)
        try:
            lng, lat = _transformer.transform(float(x), float(y))
        except ValueError:
            continue
        out.append({
            "id": place_id,
            "region": region,
            "name": name,
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
        print(f"[{region}] 다운로드 중 (orgCode={org_code})...", file=sys.stderr)
        csv_text = fetch_region_csv(org_code)
        places = parse_places(csv_text, region)
        if len(places) < 100:
            raise RuntimeError(f"[{region}] 결과가 {len(places)}건뿐 — 파싱이 깨졌을 가능성, 배포 중단")
        meta = write_region(region, places, out_dir)
        v = bump_version(existing, region, meta["sha256"])
        regions_meta[region] = {"v": v, "sha256": meta["sha256"], "bytes": meta["bytes"]}
        print(f"[{region}] {meta['count']}건, v{v}, {meta['bytes']:,} bytes", file=sys.stderr)

    version_path.write_text(json.dumps({"schema": 1, "regions": regions_meta}, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()

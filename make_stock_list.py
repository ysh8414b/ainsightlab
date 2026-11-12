"""내 매매 종목 검색용 전 종목 목록(stocks.json) 만들기

네이버 증권에서 코스피·코스닥 전 종목(일반주·우선주·ETF·ETN·리츠 등)을 받아
[[코드, 이름, 시장, 종류], ...] 형태로 저장. index.html 종목 검색이 이 파일을 기본으로 쓰고,
Supabase mt_stock_list(내 매매 분석 실행 때 갱신)가 있으면 그 시세·새 상장 종목을 덧붙임.

  python make_stock_list.py      → stocks.json 갱신 (새 상장 종목 반영하려면 가끔 다시 실행 후 커밋)
"""
import json
import os

from crawl import _fetch_naver_stocks

TYPE_MAP = {"stock": "", "etf": "ETF", "etn": "ETN"}


def main():
    out, seen = [], set()
    for market in ("KOSPI", "KOSDAQ"):
        items = _fetch_naver_stocks(market)
        print(f"{market}: {len(items)}개")
        if len(items) < 100:
            raise SystemExit(f"{market} 목록이 너무 적음 — 저장 안 함")
        for it in items:
            code = (it.get("itemCode") or "").strip()
            name = (it.get("stockName") or "").strip()
            if not code or not name or len(code) != 6 or code in seen:
                continue
            seen.add(code)
            kind = (it.get("stockEndType") or "").lower()
            out.append([code, name, market, TYPE_MAP.get(kind, kind.upper())])
    out.sort(key=lambda r: r[0])
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stocks.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"✅ {len(out)}개 → {path}")


if __name__ == "__main__":
    main()

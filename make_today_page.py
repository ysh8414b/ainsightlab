"""오늘의 증시 요약 정적 페이지(today.html) 만들기.

메인 대시보드는 자바스크립트가 데이터를 불러와야 보여서, AI·검색엔진이 원본 HTML만
읽으면 실제 숫자를 볼 수 없다. 이 스크립트는 Supabase 공개 테이블에서 그날 데이터를
읽어 글자 그대로 담긴 HTML을 만든다.

사용:
  python make_today_page.py          # 지금 바로 만들기
  python make_today_page.py --auto   # 크롤링 워크플로용: 평일 16:40 / 20:00 이후 하루 2번까지만

바뀐 내용이 없으면 파일을 쓰지 않는다 (워크플로가 git diff 로 커밋 여부 결정).
"""
import html
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
SB_URL = os.environ.get("SUPABASE_URL") or "https://mmmpqmvwpuxqyxlxytsh.supabase.co"
# 공개(anon) 키 — shared.js 와 같은 값. 공개 테이블 읽기 전용
SB_ANON = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1tbXBxbXZ3cHV4cXl4bHh5dHNoIiwi"
           "cm9sZSI6ImFub24iLCJpYXQiOjE3NzE3NTI5ODQsImV4cCI6MjA4NzMyODk4NH0.KsXLXL6g-WeodZ-wYOCJnZBkUWMZ-F06Tq4XBUQsKaA")
SB_KEY = os.environ.get("SUPABASE_KEY") or SB_ANON
SITE = "https://ainsightlab.kr"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "today.html")
SLOTS = [(1640, 1), (2000, 2)]   # (KST 시각, 회차)


def db(table, params):
    req = urllib.request.Request(f"{SB_URL}/rest/v1/{table}?{params}",
                                 headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  ⚠ {table} 조회 실패: {e}")
        return []


def e(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pct(v, sign=True):
    v = num(v)
    if v is None:
        return "-"
    return f"{v:+.2f}%" if sign else f"{v:.2f}%"


def won(v):
    v = num(v)
    return "-" if v is None else f"{v:,.0f}원"


def eok(v):
    """원 → 억/조 표시."""
    v = num(v)
    if v is None or v <= 0:
        return "-"
    e_ = v / 1e8
    return f"{e_ / 1e4:,.1f}조원" if e_ >= 1e4 else f"{e_:,.0f}억원"


def jparse(v, default):
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v) if v else default
    except Exception:
        return default


def latest(rows):
    if not rows:
        return "", []
    d = rows[0].get("date") or ""
    return d, [r for r in rows if r.get("date") == d]


def stock_link(code, name):
    return f'<a href="https://stock.naver.com/domestic/stock/{e(code)}/total" rel="nofollow">{e(name)}</a>'


# ── 섹션 ────────────────────────────────────────────────

def sec_index(rows):
    if not rows:
        return "", ""
    tr = []
    for r in rows:
        tr.append(f"<tr><td>{e(r.get('name'))}</td><td class=n>{e(r.get('value'))}</td>"
                  f"<td class='n {cls(r.get('change_pct'))}'>{e(r.get('change_amount'))} ({e(r.get('change_pct'))})</td></tr>")
    k = {r.get("name"): r for r in rows}
    brief = []
    for nm in ("코스피", "코스닥"):
        if nm in k:
            brief.append(f"{nm} {k[nm].get('value')}({k[nm].get('change_pct')})")
    body = ("<section><h2>시장 지수</h2>"
            "<table><thead><tr><th>지수</th><th>현재</th><th>전일 대비</th></tr></thead>"
            f"<tbody>{''.join(tr)}</tbody></table>"
            "<p class=note>해외 지수·환율은 이 페이지를 만든 시각 기준입니다.</p></section>")
    return body, ", ".join(brief)


def cls(v):
    s = str(v or "")
    if s.startswith("+") or (num(s.rstrip('%')) or 0) > 0:
        return "up"
    if s.startswith("-") or (num(s.rstrip('%')) or 0) < 0:
        return "dn"
    return ""


def sec_themes(rows):
    if not rows:
        return "", []
    d = rows[0].get("date") or ""
    items = []
    for r in rows[:10]:
        lead = []
        for part in str(r.get("leading_stocks") or "").split(",")[:3]:
            bits = [b.strip() for b in part.split(":")]
            if len(bits) >= 3 and bits[0]:
                lead.append(f"<span class=nw>{stock_link(bits[1], bits[0])} <span class='{cls(bits[2])}'>{e(bits[2])}</span></span>")
        items.append(
            f"<tr><td class=n>{e(r.get('rank'))}</td><td><b>{e(r.get('name'))}</b></td>"
            f"<td class='n {cls(r.get('change_pct'))}'>{e(r.get('change_pct'))}</td>"
            f"<td class=n>{e(r.get('up_count'))} / {e(r.get('down_count'))}</td>"
            f"<td>{', '.join(lead) or '-'}</td></tr>")
    body = (f"<section><h2>인기 테마 TOP 10 <small>{e(d)}</small></h2>"
            "<p>네이버 테마 기준. 뉴스 언급 빈도(50%)와 테마 소속 종목 평균 등락률(50%)을 합친 점수 순입니다.</p>"
            "<table><thead><tr><th>순위</th><th>테마</th><th>평균 등락</th><th>상승/하락 종목</th><th>대표 종목</th></tr></thead>"
            f"<tbody>{''.join(items)}</tbody></table></section>")
    return body, [r.get("name") for r in rows[:3]]


def sec_surge(rows):
    d, rows = latest(rows)
    if not d:
        return "", d, []
    head = (f"<section><h2>급등 종목 AI <small>{e(d)}</small></h2>"
            "<p>그날 +5% 이상 오르고 거래대금 1,000억 원 이상이면서 52주 신고가인 종목입니다. "
            "상승 원인을 시황·차트로 나누고, 과거 급등 사례로 학습한 딥러닝 모델(CNN+GRU)이 "
            "5거래일 뒤 오를 확률을 추정합니다.</p>")
    if not rows:
        return head + "<p>조건에 맞는 종목이 없었습니다.</p></section>", d, []
    cards = []
    for r in rows:
        pred = jparse(r.get("pred"), {}) or {}
        p5 = (pred.get("d5") or {}).get("p_up")
        q = ((jparse(r.get("market_ctx"), {}) or {}).get("quality") or {})
        good = f"{q.get('score')}/{q.get('max')}" if q.get("max") else "-"
        cards.append(
            f"<article><h3>{e(r.get('rank'))}. {stock_link(r.get('code'), r.get('name'))} "
            f"<span class=up>{pct(r.get('change_pct'))}</span></h3>"
            f"<p class=meta>거래대금 {eok(r.get('trading_value'))} · 유형: {e(r.get('surge_type') or '-')} · "
            f"주 원인: {e(r.get('main_cause') or '-')} · 좋은 조건 {e(good)}"
            + (f" · 5거래일 뒤 상승 확률 {p5 * 100:.0f}%" if num(p5) is not None else "") + "</p>"
            + (f"<p>{e(r.get('ai_comment'))}</p>" if r.get("ai_comment") else "")
            + "</article>")
    return head + "".join(cards) + "</section>", d, [r.get("name") for r in rows[:3]]


def sec_catalyst(rows):
    d, rows = latest(rows)
    if not d:
        return "", d
    tr = []
    for r in rows[:10]:
        amt = num(r.get("catalyst_amount"))
        src = r.get("catalyst_url")
        title = e(r.get("catalyst_title") or "")
        if src:
            title = f'<a href="{e(src)}" rel="nofollow">{title}</a>'
        tr.append(
            f"<tr><td class=n>{e(r.get('rank'))}</td><td>{stock_link(r.get('code'), r.get('name'))}</td>"
            f"<td>{e(r.get('catalyst_type') or '-')}</td><td>{title} <span class=meta>({e(r.get('catalyst_source') or '')} {e(r.get('catalyst_date') or '')})</span></td>"
            f"<td class=n>{f'{amt:,.0f}억원' if amt else '미공개'}</td>"
            f"<td class='n {cls(r.get('ret_5d'))}'>{pct(r.get('ret_5d'))}</td>"
            f"<td class=n>{e(r.get('total_score') if r.get('total_score') is not None else '-')}</td></tr>")
    body = (f"<section><h2>재료 포착 TOP 10 <small>{e(d)}</small></h2>"
            "<p>시가총액 3,000억 원 이상 종목 중 최근 5일 안에 DART 공시나 주요 언론으로 확인된 재료"
            "(공급계약·수주·설비투자·M&amp;A·품목허가 등)가 있고, 주가에 아직 덜 반영된 종목을 100점 만점으로 고른 목록입니다.</p>"
            "<table><thead><tr><th>순위</th><th>종목</th><th>재료</th><th>원문</th><th>규모</th><th>5일 수익률</th><th>점수</th></tr></thead>"
            f"<tbody>{''.join(tr)}</tbody></table></section>")
    return body, d


def sec_oversold(rows):
    d, rows = latest(rows)
    if not d:
        return "", d
    head = (f"<section><h2>과대 낙폭 종목 <small>{e(d)} {e((rows[0] if rows else {}).get('generated_time') or '')}</small></h2>"
            "<p>시가총액 3,000억 원·거래대금 1,000억 원 이상이고, 종가가 20일 이동평균선보다 20% 이상 낮으며 당일 오르지 않은 종목입니다.</p>")
    if not rows:
        return head + "<p>조건에 맞는 종목이 없었습니다.</p></section>", d
    tr = [f"<tr><td>{stock_link(r.get('code'), r.get('name'))}</td><td class=n>{won(r.get('price'))}</td>"
          f"<td class=n>{won(r.get('ma20'))}</td><td class='n dn'>{pct(r.get('deviation'))}</td>"
          f"<td class='n {cls(r.get('change_pct'))}'>{pct(r.get('change_pct'))}</td><td class=n>{eok(r.get('trading_value'))}</td></tr>"
          for r in rows[:15]]
    return (head + "<table><thead><tr><th>종목</th><th>종가</th><th>20일선</th><th>20일선 대비</th><th>당일</th><th>거래대금</th></tr></thead>"
            f"<tbody>{''.join(tr)}</tbody></table></section>"), d


# ── 페이지 ──────────────────────────────────────────────

CSS = """
:root{--bg:#f6f7f5;--card:#fff;--line:#dfe2e6;--fg:#1c2127;--muted:#5f6873;--accent:#0e7490;--up:#d6304a;--dn:#1d63d8}
@media (prefers-color-scheme:dark){:root{--bg:#0c0f13;--card:#12161b;--line:#222932;--fg:#e6e8eb;--muted:#8a93a0;--accent:#22b8cf;--up:#ff5c6c;--dn:#4d8dff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:'Pretendard',-apple-system,BlinkMacSystemFont,'Malgun Gothic',sans-serif;line-height:1.65}
main{max-width:920px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:24px;margin:0 0 6px}h2{font-size:18px;margin:0 0 8px}h2 small{font-size:13px;color:var(--muted);font-weight:400;margin-left:6px}
h3{font-size:15px;margin:0 0 4px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:16px 0;overflow-x:auto}
article{border-top:1px solid var(--line);padding:12px 0}article:first-of-type{border-top:0}
p{margin:6px 0}.meta,.note{color:var(--muted);font-size:13px}
table{border-collapse:collapse;width:100%;min-width:600px;font-size:14px}
.nw{white-space:nowrap}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:13px;white-space:nowrap}
.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.up{color:var(--up)}.dn{color:var(--dn)}
a{color:var(--accent)}
.lead{font-size:15px}
nav a{margin-right:12px}
footer{color:var(--muted);font-size:12px;margin-top:24px}
"""


def build():
    now = datetime.now(KST)
    idx = db("market_index", "order=id")
    thm = db("themes", "order=rank&limit=10")
    surge = db("surge_stocks", "select=date,rank,code,name,change_pct,trading_value,surge_type,main_cause,pred,ai_comment,market_ctx"
                               "&order=date.desc,rank.asc&limit=20")
    cat = db("catalyst_stocks", "select=date,rank,code,name,catalyst_type,catalyst_title,catalyst_source,catalyst_date,"
                                "catalyst_url,catalyst_amount,ret_5d,total_score&order=date.desc,rank.asc&limit=20")
    ovs = db("oversold_stocks", "order=date.desc,rank.asc&limit=40")
    if not (idx or thm or surge or cat or ovs):
        print("데이터를 하나도 못 읽음 — 페이지를 만들지 않음")
        return None

    s_idx, idx_brief = sec_index(idx)
    s_thm, top_thm = sec_themes(thm)
    s_surge, surge_d, top_surge = sec_surge(surge)
    s_cat, cat_d = sec_catalyst(cat)
    s_ovs, ovs_d = sec_oversold(ovs)
    day = max([d for d in (surge_d, cat_d, ovs_d, (thm[0].get("date") if thm else "")) if d] or [now.strftime("%Y-%m-%d")])

    summary = [f"{day} 한국 증시 요약"]
    if idx_brief:
        summary.append(idx_brief)
    if top_thm:
        summary.append("인기 테마: " + ", ".join(top_thm))
    if top_surge:
        summary.append("급등 종목: " + ", ".join(top_surge))
    desc = ". ".join(summary) + ". 재료 포착 종목, 과대 낙폭 종목까지 매일 장 마감 후 정리합니다."

    ld = {"@context": "https://schema.org", "@type": "Article",
          "headline": f"{day} 한국 증시 요약 — 지수·인기 테마·급등 종목·재료 포착",
          "datePublished": day, "dateModified": day,
          "inLanguage": "ko-KR", "url": f"{SITE}/today.html",
          "author": {"@type": "Organization", "name": "STOCKPULSE", "url": f"{SITE}/"},
          "publisher": {"@type": "Organization", "name": "STOCKPULSE", "url": f"{SITE}/"},
          "description": desc}

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<script async src="https://www.googletagmanager.com/gtag/js?id=G-3G7NQ8B69G"></script>
<script>window.dataLayer=window.dataLayer||[];function gtag(){{dataLayer.push(arguments);}}gtag('js',new Date());gtag('config','G-3G7NQ8B69G');</script>
<title>{e(day)} 한국 증시 요약 — 인기 테마·급등 종목·재료 포착 | STOCKPULSE</title>
<meta name="description" content="{e(desc)}">
<meta name="robots" content="index, follow">
<meta name="sp-slot" content="__SLOT__">
<link rel="canonical" href="{SITE}/today.html">
<meta property="og:type" content="article">
<meta property="og:title" content="{e(day)} 한국 증시 요약 | STOCKPULSE">
<meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{SITE}/today.html">
<meta property="og:locale" content="ko_KR">
<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>
<link href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<main>
<nav class=meta><a href="./">STOCKPULSE 대시보드</a><a href="guide.html">투자 가이드</a><a href="about.html">서비스 소개</a></nav>
<h1>{e(day)} 한국 증시 요약</h1>
<p class=lead>{e(desc)}</p>
<p class=meta>STOCKPULSE가 장 마감 후 자동으로 정리한 페이지입니다. 각 표 제목 옆 날짜가 그 데이터의 기준 거래일입니다.
실시간 화면·차트·종목별 상세 분석은 <a href="./">대시보드</a>에서 볼 수 있습니다.</p>
{s_idx}
{s_thm}
{s_surge}
{s_cat}
{s_ovs}
<section><h2>이 페이지에 대해</h2>
<p>STOCKPULSE(ainsightlab.kr)는 코스피·코스닥 데이터를 자동으로 모아 정리하는 무료 주식 정보 대시보드입니다.
시세는 네이버 금융, 공시는 금융감독원 DART, 해외 지수·환율은 Yahoo Finance에서 가져옵니다.
급등 종목 해설은 AI가 주어진 숫자만으로 쓴 글이고, 딥러닝 예측은 과거 패턴에 따른 확률일 뿐입니다.</p>
<p>함께 읽을거리: <a href="column/">STOCKPULSE 칼럼</a> · <a href="guide-themes.html">테마주 매매 5원칙</a> · <a href="guide-supply.html">외국인·기관·개인 수급 읽기</a> ·
<a href="guide-sectors.html">섹터 로테이션</a> · <a href="guide-indicators.html">PER·PBR·ROE 실전 활용</a></p>
</section>
<footer>이 페이지의 모든 정보는 일반 정보 제공 목적이며 특정 종목의 매수·매도 추천이 아닙니다.
투자 판단의 책임은 투자자 본인에게 있습니다. · <a href="terms.html">이용약관</a> · <a href="privacy.html">개인정보처리방침</a></footer>
</main>
</body>
</html>
"""


def strip_slot(s):
    return re.sub(r'<meta name="sp-slot" content="[^"]*">', "", s or "")


def main():
    auto = "--auto" in sys.argv
    now = datetime.now(KST)
    hm = now.hour * 100 + now.minute
    slot = None
    if auto:
        if now.weekday() >= 5:
            print("주말 — 만들지 않음")
            return
        for t, n in SLOTS:
            if hm >= t:
                slot = f"{now:%Y-%m-%d}-{n}"
        if not slot:
            print(f"아직 만들 시각 아님 ({hm:04d})")
            return
    old = ""
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            old = f.read()
    if auto and f'<meta name="sp-slot" content="{slot}">' in old:
        print(f"이번 회차({slot}) 이미 만듦")
        return
    page = build()
    if not page:
        return
    page = page.replace("__SLOT__", slot or f"{now:%Y-%m-%d}-manual")
    if strip_slot(page) == strip_slot(old):
        print("내용 변화 없음 — 그대로 둠")
        return
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)
    print(f"today.html 저장 ({len(page):,}자)")


if __name__ == "__main__":
    main()

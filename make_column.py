"""칼럼 반자동 글쓰기.

  python make_column.py draft [--auto] [--date YYYY-MM-DD] [--force]
      그날 데이터(지수·테마·급등 AI·재료 포착·과대 낙폭·성과 추적)로 AI 초안을 만들어
      column_posts 에 저장(status=draft). 공개되지 않음.
      --auto: 크롤링 워크플로용 — 평일 17:30 이후, 그날 급등 AI 결과가 있을 때(21:00 이후엔 없어도) 1번만.

  python make_column.py build
      관리자가 발행한 글(approved/published)로 column/*.html, column/index.html, sitemap.xml 을 다시 만든다.
      바뀐 파일만 쓰고, 발행 취소된 글의 파일은 지운다. 그 뒤 approved → published.

column_posts 는 service role 키(SUPABASE_KEY)로만 읽고 쓸 수 있다.
"""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.abspath(__file__))
COL_DIR = os.path.join(ROOT, "column")
SITEMAP = os.path.join(ROOT, "sitemap.xml")
SITE = "https://ainsightlab.kr"
SB_URL = os.environ.get("SUPABASE_URL") or "https://mmmpqmvwpuxqyxlxytsh.supabase.co"
SB_KEY = os.environ.get("SUPABASE_KEY") or ""
GROQ_API_KEY = os.environ.get("GROQ_API_KEY") or ""
GROQ_MODEL = os.environ.get("GROQ_MODEL") or "openai/gpt-oss-120b"
MAX_TRIES = 3


# ── Supabase ───────────────────────────────────────────

def sb(method, path, body=None, prefer=None):
    headers = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{SB_URL}/rest/v1/{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as ex:
        print(f"  ⚠ {method} {path.split('?')[0]} 실패: {ex.code} {ex.read().decode('utf-8', 'ignore')[:300]}")
        raise
    except Exception as ex:
        print(f"  ⚠ {method} {path.split('?')[0]} 실패: {ex}")
        raise


def get(path):
    try:
        return sb("GET", path) or []
    except Exception:
        return []


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def jparse(v, default):
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v) if v else default
    except Exception:
        return default


def r1(v, n=1):
    v = num(v)
    return None if v is None else round(v, n)


def eok(v):
    v = num(v)
    return None if not v else round(v / 1e8)


# ── 1. 초안 데이터 모으기 ───────────────────────────────

def track_stats(table):
    rows = get(f"{table}?select=result,close_ret,hold_days&status=eq.closed&limit=3000")
    if not rows:
        return None
    rets = [num(r.get("close_ret")) for r in rows if num(r.get("close_ret")) is not None]
    wins = sum(1 for r in rows if r.get("result") == "win")
    return {"closed": len(rows), "win_rate": round(wins / len(rows) * 100),
            "avg_ret": round(sum(rets) / len(rets), 2) if rets else None}


def collect(day):
    d = {"date": day}
    idx = get("market_index?select=name,value,change_pct&order=id")
    d["index"] = [{"name": r.get("name"), "value": r.get("value"), "change": r.get("change_pct")} for r in idx]

    thm = get(f"theme_history?select=rank,name,change_pct,leading_stocks&date=eq.{day}&order=rank&limit=10") \
        or get("themes?select=rank,name,change_pct,leading_stocks&order=rank&limit=10")
    d["themes"] = [{"rank": r.get("rank"), "name": r.get("name"), "change": r.get("change_pct"),
                    "leaders": ", ".join(p.split(":")[0] for p in str(r.get("leading_stocks") or "").split(",")[:3] if p)}
                   for r in thm]

    surge = get(f"surge_stocks?select=rank,code,name,change_pct,trading_value,surge_type,main_cause,market_ctx,pred,ai_comment"
                f"&date=eq.{day}&order=rank")
    d["surge"] = []
    for r in surge:
        q = (jparse(r.get("market_ctx"), {}) or {}).get("quality") or {}
        p5 = ((jparse(r.get("pred"), {}) or {}).get("d5") or {}).get("p_up")
        d["surge"].append({
            "name": r.get("name"), "change": r1(r.get("change_pct")), "trading_value_eok": eok(r.get("trading_value")),
            "type": r.get("surge_type"), "main_cause": r.get("main_cause"),
            "good_conditions": f"{q.get('score')}/{q.get('max')}" if q.get("max") else None,
            "core": bool(q.get("core")), "foreign_net_eok": q.get("foreign_net"), "individual_net_eok": q.get("individual_net"),
            "theme_together": q.get("theme_name") if q.get("theme_together") else None,
            "model_p_up_d5": round(p5 * 100) if num(p5) is not None else None,
            "ai_note": (r.get("ai_comment") or "")[:300]})

    # 지난 급등 종목 결과 (5거래일 뒤 성적이 채워진 가장 최근 날짜)
    past = get("surge_stocks?select=date,name,change_pct,ret_d5&ret_d5=not.is.null&order=date.desc,rank.asc&limit=15")
    if past:
        pd = past[0]["date"]
        rows = [r for r in past if r.get("date") == pd]
        d["surge_result"] = {"date": pd, "stocks": [{"name": r.get("name"), "surge_day": r1(r.get("change_pct")),
                                                     "after_5d": r1(r.get("ret_d5"), 2)} for r in rows]}

    cat = get(f"catalyst_stocks?select=rank,name,catalyst_type,catalyst_title,catalyst_amount,ret_5d,total_score"
              f"&date=eq.{day}&order=rank&limit=5")
    d["catalyst"] = [{"name": r.get("name"), "type": r.get("catalyst_type"), "title": r.get("catalyst_title"),
                      "amount_eok": num(r.get("catalyst_amount")) or None, "ret_5d": r1(r.get("ret_5d")),
                      "score": r.get("total_score")} for r in cat]

    ovs = get(f"oversold_stocks?select=name,deviation,change_pct,trading_value&date=eq.{day}&order=rank&limit=10")
    d["oversold"] = [{"name": r.get("name"), "vs_ma20": r1(r.get("deviation")), "change": r1(r.get("change_pct")),
                      "trading_value_eok": eok(r.get("trading_value"))} for r in ovs]

    d["tracks"] = {}
    for table, label in (("oversold_track", "과대 낙폭(+10% 익절/-10% 손절)"),
                         ("leader_track", "주도주(10일선 이탈 매도)"),
                         ("pullback_track", "급등주 급락(+10%/-10%)")):
        closed = get(f"{table}?select=name,result,close_ret,hold_days&close_date=eq.{day}&limit=30")
        stats = track_stats(table)
        if closed or stats:
            d["tracks"][label] = {
                "closed_today": [{"name": r.get("name"), "result": "익절" if r.get("result") == "win" else "손절",
                                  "ret": r1(r.get("close_ret"), 2), "days": r.get("hold_days")} for r in closed],
                "all_time": stats}
    return d


# ── 2. AI 초안 ─────────────────────────────────────────

PROMPT = """너는 한국 주식 정보 사이트 STOCKPULSE의 칼럼 초안 작성자다. 아래 [데이터]만 가지고 {day} 장 마감 칼럼 초안을 써라.
운영자가 이 초안을 읽고 고친 뒤 자기 의견을 덧붙여 발행한다.

[절대 규칙]
- [데이터]에 없는 숫자·뉴스·기업 사정·목표주가·실적 전망을 만들지 마라. 원인을 모르면 "데이터로는 원인이 확인되지 않았다"고 써라.
- 특정 종목을 사라/팔라고 권하지 마라. "~할 만하다", "주목해야 한다" 같은 추천성 표현도 쓰지 마라.
- 상투적인 말(변동성에 유의, 신중한 접근이 필요, 귀추가 주목된다, 투자자들의 관심이 쏠린다) 금지.
- 문장은 짧고 구체적으로. 숫자에는 단위(%, 억원)를 붙여라. 존댓말 '~습니다'체.
- 그날 데이터에서 가장 눈에 띄는 한두 가지를 골라 깊게 쓰고, 나머지는 짧게. 모든 항목을 나열하지 마라.
- 지난 급등 종목 결과·성과 추적 수치가 있으면 반드시 한 섹션으로 다뤄라 (이 사이트만의 데이터다).

[형식] JSON 하나만 출력:
{{"title": "구체적인 제목 (30자 안팎, 날짜는 넣지 말 것, 그날의 핵심이 드러나게)",
  "summary": "검색 결과에 보일 두 문장 요약 (120자 이내)",
  "body": "본문 마크다운. ## 소제목 3~5개, 필요하면 - 목록. 1,800~2,800자",
  "opinion_hints": ["운영자가 의견을 쓸 때 답해 볼 만한 질문 3개 (예: 오늘 급등 종목 중 직접 산다면 어떤 조건을 더 보겠는가?)"]}}

[데이터]
{data}
"""


def groq_draft(day, data):
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY 없음")
    body = {"model": GROQ_MODEL, "temperature": 0.5, "max_tokens": 8000,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": PROMPT.format(day=day, data=json.dumps(data, ensure_ascii=False))}]}
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
                                 data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json",
                                          "User-Agent": "stockpulse-column/1.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        res = json.loads(r.read().decode("utf-8"))
    out = json.loads(res["choices"][0]["message"]["content"])
    if len(out.get("body") or "") < 500:
        raise RuntimeError("본문이 너무 짧음")
    return out


def draft(argv):
    now = datetime.now(KST)
    auto = "--auto" in argv
    force = "--force" in argv
    day = now.strftime("%Y-%m-%d")
    if "--date" in argv:
        day = argv[argv.index("--date") + 1]
    if not SB_KEY:
        print("SUPABASE_KEY(service role) 없음 — 초안을 저장할 수 없음")
        return
    if auto:
        hm = now.hour * 100 + now.minute
        if now.weekday() >= 5 or hm < 1730:
            print(f"칼럼 초안 시간 아님 ({now:%a %H:%M})")
            return
    try:
        rows = sb("GET", f"column_posts?select=id,body,data&kind=eq.auto&date=eq.{day}") or []
    except Exception:
        print("column_posts 를 읽지 못함 (setup_column.sql 실행 여부 확인) — 건너뜀")
        return
    old = rows[0] if rows else None
    tries = int(((old or {}).get("data") or {}).get("tries") or 0)
    if old and not force and (old.get("body") or tries >= MAX_TRIES):
        print(f"{day} 칼럼 초안 이미 있음")
        return
    if auto:
        ran = get(f"surge_model_runs?select=id&date=eq.{day}&limit=1")
        if not ran and now.hour < 21:
            print("오늘 급등 AI 결과가 아직 없음 — 다음 실행 때 다시")
            return
        if not get(f"catalyst_stocks?select=id&date=eq.{day}&limit=1") and not ran:
            print(f"{day} 장 데이터 없음(휴장일?) — 초안 안 만듦")
            return

    data = collect(day)
    print(f"데이터: 급등 {len(data['surge'])} · 테마 {len(data['themes'])} · 재료 {len(data['catalyst'])} · 낙폭 {len(data['oversold'])}")
    row = {"date": day, "slug": day, "kind": "auto", "status": "draft", "updated_at": now.isoformat()}
    try:
        out = groq_draft(day, data)
        row.update(title=str(out.get("title") or "")[:120], summary=str(out.get("summary") or "")[:300],
                   body=str(out.get("body") or ""),
                   data={"facts": data, "opinion_hints": out.get("opinion_hints") or [], "tries": tries + 1,
                         "model": GROQ_MODEL})
        print(f"초안 생성: {row['title']} ({len(row['body']):,}자)")
    except Exception as ex:
        print(f"⚠ 초안 생성 실패 ({tries + 1}/{MAX_TRIES}): {ex}")
        row.update(title=row.get("title") or "(초안 생성 실패 — 직접 쓰기)", body="",
                   data={"facts": data, "error": str(ex)[:300], "tries": tries + 1})
    if old:
        sb("PATCH", f"column_posts?id=eq.{old['id']}", {k: v for k, v in row.items() if k not in ("slug", "status")},
           prefer="return=minimal")
    else:
        sb("POST", "column_posts", row, prefer="return=minimal")
    print("저장 완료 (관리자 페이지 → ✍ 칼럼에서 검토·발행)")


# ── 3. 발행 글 → 정적 페이지 ──────────────────────────

def e(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def inline(s):
    s = e(s)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)


def md(text):
    """## / ### / - / 1. / > / 빈 줄 문단 / **굵게** 만 지원 (HTML 은 모두 이스케이프)."""
    out, para, lst = [], [], None

    def flush():
        nonlocal para, lst
        if para:
            out.append("<p>" + "<br>".join(inline(x) for x in para) + "</p>")
            para = []
        if lst:
            tag, items = lst
            out.append(f"<{tag}>" + "".join(f"<li>{inline(i)}</li>" for i in items) + f"</{tag}>")
            lst = None

    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        s = line.strip()
        if not s:
            flush()
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            flush()
            lv = 2 if len(m.group(1)) <= 2 else 3
            out.append(f"<h{lv}>{inline(m.group(2))}</h{lv}>")
            continue
        m = re.match(r"^(?:[-*•]\s+)(.*)$", s)
        m2 = re.match(r"^\d+[.)]\s+(.*)$", s)
        if m or m2:
            if para:
                out.append("<p>" + "<br>".join(inline(x) for x in para) + "</p>")
                para = []
            tag = "ul" if m else "ol"
            if not lst or lst[0] != tag:
                flush()
                lst = (tag, [])
            lst[1].append((m or m2).group(1))
            continue
        if s.startswith(">"):
            flush()
            out.append(f"<blockquote>{inline(s.lstrip('> ').strip())}</blockquote>")
            continue
        if lst:
            flush()
        para.append(s)
    flush()
    return "\n".join(out)


def plain_len(text):
    return len(re.sub(r"[#*>\-\s]", "", text or ""))


GA = """<script async src="https://www.googletagmanager.com/gtag/js?id=G-3G7NQ8B69G"></script>
<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}gtag('js',new Date());gtag('config','G-3G7NQ8B69G');</script>"""
ADS = '<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-8471845294355265" crossorigin="anonymous"></script>'

CSS = """
.col-meta{color:var(--muted);font-size:13px;margin:0 0 18px}
.col-lead{font-size:15px;line-height:1.8;color:var(--text-sub);padding:14px 18px;border-radius:10px;background:var(--card-bg);border:1px solid var(--card-bd);margin-bottom:26px}
.col-body h2{font-size:18px;font-weight:700;margin:30px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--div2)}
.col-body h3{font-size:15px;font-weight:600;margin:20px 0 8px}
.col-body p,.col-body li{font-size:15px;line-height:1.85;color:var(--text-sub)}
.col-body p{margin:0 0 14px}
.col-body ul,.col-body ol{padding-left:22px;margin:0 0 14px}
.col-body li{margin-bottom:4px}
.col-body blockquote{margin:0 0 14px;padding:10px 16px;border-left:3px solid var(--div2);color:var(--muted)}
.col-opinion{margin:32px 0 20px;padding:18px 20px;border-radius:12px;border-left:3px solid #06b6d4;background:rgba(6,182,212,0.05)}
.col-opinion .lbl{font-size:13px;font-weight:700;color:#06b6d4;margin-bottom:8px}
.col-opinion p{font-size:15px;line-height:1.85;color:var(--text);margin:0 0 10px}
.col-note{font-size:12px;color:var(--dimmer);line-height:1.7;margin-top:18px}
.col-nav{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-top:28px}
.col-nav a,.col-chip{padding:8px 14px;border-radius:8px;background:var(--card-bg);border:1px solid var(--card-bd);font-size:13px;color:var(--text-sub)}
.col-list a{display:block;padding:16px 18px;border-bottom:1px solid var(--div);color:inherit}
.col-list a:hover{background:var(--hover)}
.col-list .t{font-weight:700;font-size:16px;color:var(--text);margin-bottom:4px}
.col-list .s{font-size:14px;color:var(--text-sub);line-height:1.6}
.col-list .d{font-size:12px;color:var(--dimmer);margin-top:6px}
"""

FOOTER = """  <footer style="border-top:1px solid var(--div2);max-width:900px;margin:0 auto;padding:18px 40px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;color:var(--dimmer);font-size:13px">
    <div><span style="font-weight:600;color:var(--dim)">STOCKPULSE</span> © 2026</div>
    <div class="footer-links">
      <a href="../index.html">대시보드</a>
      <a href="./">칼럼</a>
      <a href="../guide.html">투자 가이드</a>
      <a href="../about.html">서비스 소개</a>
      <a href="../privacy.html">개인정보처리방침</a>
      <a href="../terms.html">이용약관</a>
    </div>
  </footer>"""


def page(title, desc, url, body, robots="index, follow", ld=None, ads=True):
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  {GA}
  <title>{e(title)} — STOCKPULSE</title>
  <meta name="description" content="{e(desc)}" />
  <meta name="robots" content="{robots}" />
  <link rel="canonical" href="{url}" />
  <meta property="og:type" content="article" />
  <meta property="og:site_name" content="STOCKPULSE" />
  <meta property="og:title" content="{e(title)}" />
  <meta property="og:description" content="{e(desc)}" />
  <meta property="og:url" content="{url}" />
  <meta property="og:locale" content="ko_KR" />
  {f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>' if ld else ''}
  <link href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css" rel="stylesheet" />
  <link rel="stylesheet" href="../style.css" />
  <script src="../shared.js"></script>
  <style>{CSS}</style>
  {ADS if ads else ''}
</head>
<body>
<div id="root">
  <header class="page-header">
    <a href="./" class="page-back">← 칼럼 목록</a>
    <a href="../index.html" style="display:flex;align-items:center;gap:8px">
      <div style="width:8px;height:8px;border-radius:50%;background:#22c55e"></div>
      <span style="font-size:18px;font-weight:800;letter-spacing:-0.03em;color:var(--text)">STOCKPULSE</span>
    </a>
  </header>
  <main class="page-wrap">
{body}
  </main>
{FOOTER}
</div>
<script>applyTheme(getStoredTheme())</script>
</body>
</html>
"""


def post_html(p, prev_p, next_p):
    url = f"{SITE}/column/{p['slug']}.html"
    pub = (p.get("published_at") or p.get("updated_at") or p["date"])[:10]
    mod = (p.get("updated_at") or pub)[:10]
    opinion = "".join(f"<p>{inline(x.strip())}</p>" for x in re.split(r"\n\s*\n", p.get("opinion") or "") if x.strip())
    auto = p.get("kind") == "auto"
    note = ("이 글은 STOCKPULSE가 모은 시세·공시·자체 분석 데이터로 AI가 초안을 쓰고, 운영자가 내용을 검토·수정한 뒤 의견을 덧붙인 글입니다. "
            if auto else "")
    nav = []
    if prev_p:
        nav.append(f'<a href="{e(prev_p["slug"])}.html">← {e(prev_p["title"])}</a>')
    nav.append('<a href="./">칼럼 목록</a>')
    if next_p:
        nav.append(f'<a href="{e(next_p["slug"])}.html">{e(next_p["title"])} →</a>')
    ld = {"@context": "https://schema.org", "@type": "Article", "headline": p["title"], "description": p.get("summary") or "",
          "datePublished": pub, "dateModified": mod, "inLanguage": "ko-KR", "url": url,
          "author": {"@type": "Person", "name": "STOCKPULSE 운영자", "url": f"{SITE}/about.html"},
          "publisher": {"@type": "Organization", "name": "STOCKPULSE", "url": f"{SITE}/"}}
    body = f"""    <article>
      <h1 style="font-size:24px;font-weight:800;margin-bottom:8px;line-height:1.4">{e(p['title'])}</h1>
      <p class="col-meta">{e(p['date'])} 기준 · STOCKPULSE 운영자{' · 데이터 칼럼' if auto else ''}</p>
      {f'<div class="col-lead">{e(p.get("summary"))}</div>' if p.get('summary') else ''}
      <div class="col-body">
{md(p.get('body'))}
      </div>
      {f'<div class="col-opinion"><div class="lbl">✍ 운영자 의견</div>{opinion}</div>' if opinion else ''}
      <p class="col-note">{note}모든 내용은 일반 정보 제공 목적이며 특정 종목의 매수·매도 추천이 아닙니다. 투자 판단과 그 결과의 책임은 투자자 본인에게 있습니다.
      숫자는 기준일 장 마감 데이터이며, 실시간 화면은 <a href="../index.html">대시보드</a>에서 볼 수 있습니다.</p>
    </article>
    <div class="col-nav">{''.join(nav)}</div>
    <div style="margin-top:18px;display:flex;flex-wrap:wrap;gap:8px">
      <a class="col-chip" href="../guide-themes.html">→ 테마주 투자 5원칙</a>
      <a class="col-chip" href="../guide-supply.html">→ 외국인·기관 수급 읽기</a>
      <a class="col-chip" href="../guide-sectors.html">→ 섹터 로테이션</a>
    </div>"""
    return page(p["title"], p.get("summary") or p["title"], url, body, ld=ld)


def index_html(posts):
    url = f"{SITE}/column/"
    if posts:
        items = "".join(
            f'<a href="{e(p["slug"])}.html"><div class="t">{e(p["title"])}</div>'
            f'<div class="s">{e(p.get("summary") or "")}</div><div class="d">{e(p["date"])}</div></a>' for p in posts)
        lst = f'<div class="col-list" style="background:var(--card-bg);border:1px solid var(--card-bd);border-radius:12px;overflow:hidden">{items}</div>'
    else:
        lst = '<p style="color:var(--muted)">아직 발행된 칼럼이 없습니다.</p>'
    body = f"""    <h1 style="font-size:24px;font-weight:800;margin-bottom:8px">STOCKPULSE 칼럼</h1>
    <p style="color:var(--muted);font-size:15px;margin-bottom:24px;line-height:1.7">급등 종목 AI·재료 포착·과대 낙폭·성과 추적 등 STOCKPULSE가 매일 쌓는 데이터를 바탕으로,
    그날 시장에서 눈여겨볼 점과 지난 선정 종목의 실제 결과를 운영자가 정리하는 글입니다.</p>
    {lst}"""
    return page("STOCKPULSE 칼럼 — 데이터로 읽는 한국 증시", "급등 종목·재료·과대 낙폭·성과 추적 데이터를 바탕으로 운영자가 정리하는 한국 증시 칼럼.",
                url, body, robots="index, follow" if posts else "noindex, follow", ads=bool(posts))


def write_if_changed(path, text):
    old = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
    if old == text:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return True


def update_sitemap(posts):
    if not os.path.exists(SITEMAP):
        return False
    with open(SITEMAP, encoding="utf-8") as f:
        sm = f.read()
    base = re.sub(r"\s*<url>\s*<loc>[^<]*/column/[^<]*</loc>.*?</url>", "", sm, flags=re.S)
    add = []
    if posts:
        add.append(f"  <url>\n    <loc>{SITE}/column/</loc>\n    <lastmod>{posts[0]['date']}</lastmod>\n"
                   f"    <changefreq>daily</changefreq>\n    <priority>0.8</priority>\n  </url>")
        for p in posts:
            mod = (p.get("updated_at") or p["date"])[:10]
            add.append(f"  <url>\n    <loc>{SITE}/column/{p['slug']}.html</loc>\n    <lastmod>{mod}</lastmod>\n"
                       f"    <changefreq>monthly</changefreq>\n    <priority>0.7</priority>\n  </url>")
    new = base.replace("</urlset>", ("\n".join(add) + "\n" if add else "") + "</urlset>")
    new = re.sub(r"\n{3,}", "\n\n", new)
    return write_if_changed(SITEMAP, new)


def build(argv):
    if not SB_KEY:
        print("SUPABASE_KEY(service role) 없음 — 칼럼 페이지를 만들 수 없음")
        return
    try:
        posts = sb("GET", "column_posts?select=id,date,slug,kind,title,summary,body,opinion,status,published_at,updated_at"
                          "&status=in.(approved,published)&order=date.desc,id.desc&limit=1000") or []
    except Exception:
        print("column_posts 를 읽지 못함 (setup_column.sql 실행 여부 확인) — 건너뜀")
        return
    posts = [p for p in posts if p.get("slug") and re.fullmatch(r"[0-9A-Za-z-]+", p["slug"])]
    changed = []
    for i, p in enumerate(posts):
        if p.get("status") == "approved" and not p.get("published_at"):
            p["published_at"] = datetime.now(KST).isoformat()
        newer = posts[i - 1] if i > 0 else None
        older = posts[i + 1] if i + 1 < len(posts) else None
        if write_if_changed(os.path.join(COL_DIR, f"{p['slug']}.html"), post_html(p, older, newer)):
            changed.append(p["slug"])
    keep = {f"{p['slug']}.html" for p in posts} | {"index.html"}
    if os.path.isdir(COL_DIR):
        for fn in os.listdir(COL_DIR):
            if fn.endswith(".html") and fn not in keep:
                os.remove(os.path.join(COL_DIR, fn))
                changed.append(f"삭제 {fn}")
    if True:  # 글이 없어도 목록 페이지(noindex)는 둠 — 사이트 곳곳의 링크가 404 가 되지 않게
        if write_if_changed(os.path.join(COL_DIR, "index.html"), index_html(posts)):
            changed.append("index")
    if update_sitemap(posts):
        changed.append("sitemap")
    print(f"칼럼 {len(posts)}개 · 바뀐 파일: {', '.join(changed) or '없음'}")
    for p in posts:
        if p.get("status") == "approved":
            try:
                sb("PATCH", f"column_posts?id=eq.{p['id']}&status=eq.approved",
                   {"status": "published", "published_at": p["published_at"]}, prefer="return=minimal")
                print(f"  ✅ 발행: {p['title']}")
            except Exception:
                pass


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "draft":
        draft(sys.argv[2:])
    elif cmd == "build":
        build(sys.argv[2:])
    else:
        print(__doc__)

"""내 매매 분석 — 사용자가 입력한 보유 종목을 매도할 때까지 매일 분석 (GitHub Actions mytrades.yml)

1. 종목 검색 목록(mt_stock_list) 갱신 — 전 종목 이름·시세
2. 보유 중인 종목(모든 사용자, 같은 종목은 1번만)을 기준 거래일로 분석 → mt_daily
   - 차트: 신고가·돌파·거래량·갭·장대봉·이평선(5/10/20/60) 이탈·역배열·신저가·RSI
   - 이슈: 지수, 섹터 평균, 소속 테마(네이버) 동반 흐름, 공시(DART), 뉴스, 외국인·기관·개인 수급
   - 딥러닝(보유 모델): 시총 3,000억+ 종목의 '아무 날'(급등일만이 아님) 수만 건으로 학습한
     CNN+GRU → 앞으로 1·5·20거래일 상승 확률 + 수익률 범위, 차트가 비슷했던 과거 사례
     (모델은 surge_cache/hold_model.pt에 저장, 7일마다 다시 학습)
   - Groq: "오늘 무엇이 달라졌나" 3문장 (주어진 숫자만)
3. 매매마다 수익률·보유 중 최고/최저·보유일·상태 요약 → mt_trades
   - 처음 분석할 때 매수일 기준 차트·지표·AI 예측을 entry_snapshot에 저장 (2단계: 비슷한 매수 통계·전용 모델 학습용)
   - 매도한 매매는 한 번 최종 계산 후 final_done

로컬 시험:
    python mytrades_ai.py --dry-run --universe 120 --skip-groq
"""

import os
import json
import math
import time
import argparse
from datetime import datetime, timedelta

import surge_ai as S   # SUPABASE_KEY 없으면 anon 키로 바꿔 줌 + crawl 불러옴
import numpy as np
import requests

crawl = S.crawl
log = crawl.log
KST = S.KST

HOLD_MODEL = os.path.join(S.CACHE_DIR, "hold_model.pt")
MODEL_MAX_AGE_DAYS = 7
SAMPLE_DAYS = 520      # 최근 약 2년 안에서
SAMPLE_STRIDE = 7      # 종목마다 7거래일 간격으로
SAMPLE_MAX = 40000
OHLCV_KEEP = 300   # 화면 차트(120일선·MACD)용 — 최신 하루만 전체를 읽음


def _num(v, mul=1):
    try:
        return float(str(v).replace(",", "")) * mul
    except (TypeError, ValueError):
        return None


def stock_list_rows():
    """종목 검색 목록: 코스피·코스닥 전 종목(일반·우선주·ETF·ETN) 이름·시세"""
    kinds = {"stock": "", "etf": "ETF", "etn": "ETN"}
    now_iso = datetime.now(KST).isoformat()
    rows, seen = [], set()
    for market in ("KOSPI", "KOSDAQ"):
        items = crawl._fetch_naver_stocks(market)
        if len(items) < 100:
            log(f"  ⚠️ {market} 종목 목록 {len(items)}개 — 이번엔 검색 목록 갱신 안 함")
            return []
        for it in items:
            code = (it.get("itemCode") or "").strip()
            name = (it.get("stockName") or "").strip()
            if len(code) != 6 or not name or code in seen:
                continue
            seen.add(code)
            k = (it.get("stockEndType") or "").lower()
            rows.append({"code": code, "name": name, "market": market,
                         "price": _num(it.get("closePrice")), "change_pct": _num(it.get("fluctuationsRatio")),
                         "market_cap": _num(it.get("marketValue"), 100_000_000),
                         "kind": kinds.get(k, k.upper()), "updated_at": now_iso})
    return rows


# ═════════════════════════════════════════
# Supabase (upsert·페이지 조회는 crawl.supabase_request에 없어 직접)
# ═════════════════════════════════════════
def _hdr(prefer="return=minimal"):
    k = os.environ.get("SUPABASE_KEY", "")
    return {"apikey": k, "Authorization": f"Bearer {k}", "Content-Type": "application/json", "Prefer": prefer}


def sb_upsert(table, rows, on_conflict):
    url = f"{crawl.SUPABASE_URL}/rest/v1/{table}"
    ok = True
    for i in range(0, len(rows), 500):
        part = json.loads(json.dumps(rows[i:i + 500], default=float))
        r = requests.post(url, headers=_hdr("resolution=merge-duplicates,return=minimal"),
                          params={"on_conflict": on_conflict}, json=part, timeout=60)
        if r.status_code >= 400:
            log(f"  ⚠️ {table} 저장 실패 {r.status_code}: {r.text[:200]}")
            ok = False
    return ok


def sb_all(table, params):
    out, off = [], 0
    while True:
        p = dict(params); p["limit"] = "1000"; p["offset"] = str(off)
        rows = crawl.supabase_request("GET", table, params=p)
        if not rows:
            break
        out += rows
        if len(rows) < 1000:
            break
        off += 1000
    return out


def sb_patch(table, row_id, data):
    url = f"{crawl.SUPABASE_URL}/rest/v1/{table}"
    r = requests.patch(url, headers=_hdr(), params={"id": f"eq.{row_id}"},
                       json=json.loads(json.dumps(data, default=float)), timeout=30)
    if r.status_code >= 400:
        log(f"  ⚠️ {table} #{row_id} 갱신 실패 {r.status_code}: {r.text[:200]}")
        return False
    return True


# ═════════════════════════════════════════
# 1. 보유 모델 (아무 날 → 이후 1·5·20일)
# ═════════════════════════════════════════
def surge_flags(arr):
    c = arr[:, 4]; v = arr[:, 5]
    ret = np.zeros(len(c)); ret[1:] = c[1:] / c[:-1] - 1
    return (ret >= S.EV_MIN_RET) & (ret <= S.EV_MAX_RET) & (c * v >= S.EV_MIN_TV)


def compute_heat(cache):
    heat = {}
    for code, arr in cache.items():
        if code in ("KOSPI", "KOSDAQ") or arr is None or len(arr) < 2:
            continue
        for d in arr[surge_flags(arr), 0]:
            heat[int(d)] = heat.get(int(d), 0) + 1
    return heat


def build_samples(cache, markets, idx_map, heat):
    Ss, Ts, Ys, meta = [], [], [], []
    for code, arr in cache.items():
        if code in ("KOSPI", "KOSDAQ") or arr is None or len(arr) < S.MIN_HIST + 25:
            continue
        n = len(arr); c = arr[:, 4]; h = arr[:, 2]; v = arr[:, 5]
        fl = surge_flags(arr)
        kosdaq = markets.get(code) == "KOSDAQ"
        idx = idx_map["KOSDAQ" if kosdaq else "KOSPI"]
        start = max(S.MIN_HIST, n - SAMPLE_DAYS) + (sum(map(ord, code)) % SAMPLE_STRIDE)
        for t in range(start, n, SAMPLE_STRIDE):
            if v[t] <= 0 or v[t - 1] <= 0:
                continue
            r = c[t] / c[t - 1] - 1
            if abs(r) > S.EV_MAX_RET:
                continue
            try:
                seq, tab = S.make_features(arr, t, idx, heat.get(int(arr[t, 0]), 0), int(fl[t - 20:t].sum()), kosdaq)
            except Exception:
                continue
            y = [c[t + hh] / c[t] - 1 if t + hh < n else np.nan for hh in S.HORIZONS]
            if any(abs(x) > 1.5 for x in y if not np.isnan(x)):   # 액면분할 등
                continue
            Ss.append(seq); Ts.append(tab); Ys.append(y)
            meta.append({"code": code, "date": int(arr[t, 0]), "chg": round(r * 100, 2),
                         "nh": bool(c[t] >= h[max(0, t - 250):t].max())})
    if len(Ss) < 1000:
        return None
    Sa = np.array(Ss, dtype=np.float32); Ta = np.array(Ts, dtype=np.float32); Ya = np.array(Ys, dtype=np.float32)
    if len(Sa) > SAMPLE_MAX:
        keep = np.sort(np.random.default_rng(0).choice(len(Sa), SAMPLE_MAX, replace=False))
        Sa, Ta, Ya = Sa[keep], Ta[keep], Ya[keep]
        meta = [meta[i] for i in keep]
    return Sa, Ta, Ya, meta, heat


def save_model(model, metrics):
    torch, _ = S._torch()
    nets, sc, bank = model
    emb, yb, mb = bank
    os.makedirs(S.CACHE_DIR, exist_ok=True)
    torch.save({"trained": S.TODAY, "n_tab": len(S.TAB_NAMES), "states": [n.state_dict() for n in nets],
                "scaler": [sc.sm, sc.ss, sc.tm, sc.ts], "emb": emb, "y": yb, "meta": mb, "metrics": metrics},
               HOLD_MODEL + ".tmp")
    os.replace(HOLD_MODEL + ".tmp", HOLD_MODEL)


def load_model():
    if not os.path.exists(HOLD_MODEL):
        return None
    try:
        torch, _ = S._torch()
        d = torch.load(HOLD_MODEL, weights_only=False)
        if d.get("n_tab") != len(S.TAB_NAMES):
            return None
        age = (datetime.now(KST).date() - datetime.strptime(d["trained"], "%Y-%m-%d").date()).days
        if age > MODEL_MAX_AGE_DAYS:
            log(f"  🧠 보유 모델 {age}일 지남 — 다시 학습")
            return None
        nets = []
        for st in d["states"]:
            net = S.build_net(d["n_tab"]); net.load_state_dict(st); net.eval(); nets.append(net)
        sc = S.Scaler(); sc.sm, sc.ss, sc.tm, sc.ts = d["scaler"]
        log(f"  🧠 보유 모델 불러옴 (학습일 {d['trained']})")
        return (nets, sc, (d["emb"], d["y"], d["meta"])), d.get("metrics") or {}
    except Exception as e:
        log(f"  ⚠️ 보유 모델 불러오기 실패: {e}")
        return None


def get_model(cache, markets, idx_map, heat, seeds, retrain):
    if not retrain:
        got = load_model()
        if got:
            return got
    ev = build_samples(cache, markets, idx_map, heat)
    if ev is None:
        log("  ⚠️ 학습 표본 부족 — 예측 없이 분석")
        return None, {}
    log(f"  🧠 보유 모델 학습: 표본 {len(ev[3]):,}건")
    t0 = time.time()
    nets, sc, metrics, bank = S.evaluate_and_train(ev, seeds=seeds)
    metrics["train_seconds"] = round(time.time() - t0, 1)
    metrics["trained"] = S.TODAY
    metrics["sample_rule"] = {"days": SAMPLE_DAYS, "stride": SAMPLE_STRIDE, "max": SAMPLE_MAX}
    model = (nets, sc, bank)
    try:
        save_model(model, metrics)
    except Exception as e:
        log(f"  ⚠️ 보유 모델 저장 실패: {e}")
    log(f"  🧠 학습 완료 ({metrics['train_seconds']}초)")
    return model, metrics


def predict_at(model, arr, t, idx, heat, kosdaq, names, exclude_code=None):
    """arr[t] 기준 예측 + 비슷한 과거 사례. 반환 (pred, similar, tab, seq)"""
    if model is None or t < S.MIN_HIST:
        return {}, {}, None, None
    nets, sc, (emb_bank, y_bank, meta_bank) = model
    fl = surge_flags(arr)
    seq, tab = S.make_features(arr, t, idx, heat.get(int(arr[t, 0]), 0), int(fl[t - 20:t].sum()), kosdaq)
    s_, t_ = sc(seq[None], tab[None])
    pp, qq, ee = S.predict(nets, s_, t_)
    pred = {}
    for k, hh in enumerate(S.HORIZONS):
        pred[f"d{hh}"] = {"p_up": round(float(pp[0, k]), 4), "q10": round(float(qq[0, k, 0]) * 100, 2),
                          "q50": round(float(qq[0, k, 1]) * 100, 2), "q90": round(float(qq[0, k, 2]) * 100, 2)}
    mu = emb_bank.mean(0); sd = emb_bank.std(0) + 1e-6
    e = (ee[0] - mu) / sd; e /= (np.linalg.norm(e) + 1e-9)
    B = (emb_bank - mu) / sd; B /= (np.linalg.norm(B, axis=1, keepdims=True) + 1e-9)
    sims = B @ e
    d0 = int(arr[t, 0])
    cut = int((datetime.strptime(str(d0), "%Y%m%d") - timedelta(days=45)).strftime("%Y%m%d"))
    cases = []
    for j in np.argsort(-sims):
        m = meta_bank[j]
        if m["date"] >= d0 or (m["code"] == exclude_code and m["date"] >= cut):
            continue   # 미래 사례·같은 종목 최근 사례 제외
        cases.append((int(j), float(sims[j])))
        if len(cases) >= 30:
            break

    def agg(k):
        vals = [y_bank[j, k] for j, _ in cases if not np.isnan(y_bank[j, k])]
        if not vals:
            return 0.0, 0.0
        return round(float(np.mean(vals)) * 100, 2), round(float(np.mean(np.array(vals) > 0)), 3)
    (a1, u1), (a5, u5), (a20, u20) = agg(0), agg(1), agg(2)
    fmt = lambda d: f"{str(d)[:4]}-{str(d)[4:6]}-{str(d)[6:]}"
    nz = lambda x: None if np.isnan(x) else round(float(x) * 100, 2)
    similar = {"summary": {"n": len(cases), "d1_avg": a1, "d1_up": u1, "d5_avg": a5, "d5_up": u5,
                           "d20_avg": a20, "d20_up": u20},
               "cases": [{"code": meta_bank[j]["code"], "name": names.get(meta_bank[j]["code"], meta_bank[j]["code"]),
                          "date": fmt(meta_bank[j]["date"]), "chg": meta_bank[j]["chg"], "sim": round(s, 3),
                          "d1": nz(y_bank[j, 0]), "d5": nz(y_bank[j, 1]), "d20": nz(y_bank[j, 2])}
                         for j, s in cases[:6]]}
    return pred, similar, tab, seq


# ═════════════════════════════════════════
# 2. 차트 신호 (오르는 날·내리는 날 모두)
# ═════════════════════════════════════════
TONE = {"high": 1, "box": 1, "gap": 1, "candle": 1, "rebound": 1, "ma": 1, "align": 1, "limit": 1,
        "wick": -1, "volume": 0, "streak": 0, "ipo": 0}


def ma_at(c, t, n):
    return float(c[t - n + 1:t + 1].mean()) if t >= n - 1 else None


def rsi(c, t, n=14):
    if t < n + 1:
        return None
    d = np.diff(c[t - n:t + 1])
    up = d[d > 0].sum(); dn = -d[d < 0].sum()
    if dn == 0:
        return 100.0
    return float(100 - 100 / (1 + up / dn))


def chart_state(arr, t):
    """arr[t] 기준 차트 신호 [{kind,label,detail,tone}] + 이평선 정보"""
    a = arr[:t + 1]
    o, h, l, c0, v0 = a[t, 1], a[t, 2], a[t, 3], a[t, 4], a[t, 5]
    C = a[:, 4]; H = a[:, 2]; L = a[:, 3]; V = a[:, 5]
    pc = C[t - 1] if t >= 1 else c0
    ret = c0 / pc - 1 if pc else 0
    sig = []
    if t >= 70:
        for kind, label, detail, _w in S.chart_signals(a):
            sig.append({"kind": kind, "label": label, "detail": detail, "tone": TONE.get(kind, 0)})
    mas = {n: ma_at(C, t, n) for n in (5, 10, 20, 60, 120)}
    prev = {n: ma_at(C, t - 1, n) for n in (10, 20, 60)} if t >= 1 else {}
    for n in (10, 20, 60):
        m, pm = mas.get(n), prev.get(n)
        if m is None or pm is None:
            continue
        if c0 < m and C[t - 1] >= pm:
            sig.append({"kind": f"ma{n}_break", "label": f"{n}일선 아래로 마감",
                        "detail": f"{n}일선 {m:,.0f} / 종가 {c0:,.0f}", "tone": -1})
        elif c0 < m:
            k = 0
            for i in range(t, max(t - 30, n), -1):
                mi = ma_at(C, i, n)
                if mi is not None and C[i] < mi:
                    k += 1
                else:
                    break
            if n in (10, 20) and k >= 2:
                sig.append({"kind": f"ma{n}_below", "label": f"{n}일선 아래 {k}일째",
                            "detail": f"{n}일선 {m:,.0f}", "tone": -1})
        elif c0 >= m and C[t - 1] < pm:
            sig.append({"kind": f"ma{n}_up", "label": f"{n}일선 회복", "detail": f"{n}일선 {m:,.0f}", "tone": 1})
    rng = h - l
    if rng > 0 and (o - c0) / rng >= 0.7 and ret <= -0.05:
        sig.append({"kind": "bear_candle", "label": "장대음봉", "detail": f"{ret*100:+.1f}%", "tone": -1})
    if pc and o <= pc * 0.97:
        sig.append({"kind": "gap_down", "label": "갭 하락 출발", "detail": f"시가 {(o/pc-1)*100:+.1f}%", "tone": -1})
    if t >= 21:
        v20 = V[t - 20:t].mean()
        if v20 > 0 and ret <= -0.03 and v0 / v20 >= 2:
            sig.append({"kind": "vol_drop", "label": "거래량 실린 하락", "detail": f"거래량 20일 평균의 {v0/v20:.1f}배", "tone": -1})
        if c0 <= L[t - 20:t].min():
            sig.append({"kind": "low20", "label": "20일 신저가", "detail": f"직전 20일 저가 {L[t-20:t].min():,.0f} 아래", "tone": -1})
    if mas[5] and mas[20] and mas[60] and mas[5] < mas[20] < mas[60]:
        sig.append({"kind": "inverse", "label": "이평선 역배열", "detail": "5일 < 20일 < 60일", "tone": -1})
    r14 = rsi(C, t)
    if r14 is not None and r14 >= 75:
        sig.append({"kind": "rsi_hot", "label": "단기 과열 (RSI)", "detail": f"RSI {r14:.0f}", "tone": 0})
    elif r14 is not None and r14 <= 30:
        sig.append({"kind": "rsi_cold", "label": "단기 과매도 (RSI)", "detail": f"RSI {r14:.0f}", "tone": 0})
    info = {"ma": {str(n): (round(m, 2) if m else None) for n, m in mas.items()},
            "rsi": round(r14, 1) if r14 is not None else None,
            "ret5": round((c0 / C[t - 5] - 1) * 100, 2) if t >= 5 else None,
            "ret20": round((c0 / C[t - 20] - 1) * 100, 2) if t >= 20 else None,
            "hi52": round(float(H[max(0, t - 250):t + 1].max()), 2)}
    return sig, info


def state_label(signals):
    s = sum(x.get("tone", 0) for x in signals)
    strong_bad = any(x["kind"] in ("ma10_break", "ma20_break", "bear_candle", "vol_drop", "dd10", "loss10") for x in signals)
    if s <= -3 or (strong_bad and s <= -1):
        return {"score": s, "label": "경고", "color": "red"}
    if s < 0 or strong_bad:
        return {"score": s, "label": "주의", "color": "orange"}
    if s >= 2:
        return {"score": s, "label": "양호", "color": "green"}
    return {"score": s, "label": "중립", "color": "gray"}


# ═════════════════════════════════════════
# 3. 이슈 (시황·테마·공시·뉴스·수급)
# ═════════════════════════════════════════
def code_themes(code, krx, themes_raw):
    out = []
    for name, codes in (themes_raw or {}).items():
        if code not in codes or len(codes) > 60:
            continue
        peers = [krx[c] for c in codes if c != code and c in krx]
        if len(peers) < 2:
            continue
        chs = [p.get("change_pct", 0) for p in peers]
        out.append({"name": name, "avg_pct": round(sum(chs) / len(chs), 2), "count": len(peers),
                    "up3": sum(1 for x in chs if x >= 3), "down3": sum(1 for x in chs if x <= -3)})
    out.sort(key=lambda x: abs(x["avg_pct"]), reverse=True)
    return out[:4]


def issues_for(code, me, krx, idx_today, themes, disc, news, flow):
    iss = []
    mk = me.get("market", "KOSPI")
    ir = idx_today.get(mk, 0.0)
    nm = "코스닥" if mk == "KOSDAQ" else "코스피"
    if abs(ir) >= 1.0:
        iss.append({"kind": "market", "label": f"{nm} {'강세' if ir > 0 else '약세'}", "detail": f"지수 {ir:+.2f}%",
                    "tone": 1 if ir > 0 else -1})
    sec = me.get("display_sector") or ""
    sec_info = None
    if sec:
        peers = [d for d in krx.values() if d.get("display_sector") == sec and d["code"] != code
                 and d.get("market_cap", 0) >= S.UNIVERSE_MIN_CAP]
        if len(peers) >= 3:
            avg = sum(p["change_pct"] for p in peers) / len(peers)
            sec_info = {"name": sec, "avg_pct": round(avg, 2), "count": len(peers)}
            if abs(avg) >= 1.5:
                iss.append({"kind": "sector", "label": f"{sec} 섹터 {'동반 상승' if avg > 0 else '동반 하락'}",
                            "detail": f"섹터 평균 {avg:+.2f}%", "tone": 1 if avg > 0 else -1})
    if themes and abs(themes[0]["avg_pct"]) >= 2:
        b = themes[0]
        iss.append({"kind": "theme", "label": f"{b['name']} 테마 {'강세' if b['avg_pct'] > 0 else '약세'}",
                    "detail": f"테마 평균 {b['avg_pct']:+.2f}% · {b['count']}종목", "tone": 1 if b["avg_pct"] > 0 else -1})
    for d in (disc or [])[:2]:
        if d.get("days_ago", 9) <= 2:
            iss.append({"kind": "disclosure", "label": f"공시: {d.get('type', '')}", "detail": d.get("title", ""), "tone": 0})
    titles = [n["title"] for n in news]
    nl, nt = S.classify_news(titles)
    if nl:
        iss.append({"kind": "news", "label": f"뉴스: {nl}", "detail": nt, "tone": 0})
    if flow:
        fn, inn, pn = flow.get("foreign_net", 0), flow.get("institution_net", 0), flow.get("individual_net", 0)
        if abs(fn) >= 30 or abs(inn) >= 30:
            if fn > 0 and inn > 0:
                lab, tn = "외국인·기관 동반 순매수", 1
            elif fn < 0 and inn < 0:
                lab, tn = "외국인·기관 동반 순매도", -1
            elif fn > 0:
                lab, tn = "외국인 순매수", 1
            elif inn > 0:
                lab, tn = "기관 순매수", 1
            else:
                lab, tn = "외국인·기관 순매도", -1
            iss.append({"kind": "flow", "label": lab,
                        "detail": f"외국인 {fn:+,}억 · 기관 {inn:+,}억 · 개인 {pn:+,}억", "tone": tn})
    return iss, sec_info


def groq_comments(items):
    if not crawl.GROQ_API_KEY or not items:
        return {}
    out = {}
    for i in range(0, len(items), 12):
        part = items[i:i + 12]
        blocks = []
        for it in part:
            p = it["pred"]
            ptxt = " / ".join(f"{k[1:]}거래일 뒤 상승확률 {v['p_up']*100:.0f}%" for k, v in p.items()) or "예측 없음"
            sim = (it["similar"] or {}).get("summary") or {}
            blocks.append(
                f"[{it['name']} ({it['code']})] 종가 {it['close']:,.0f} ({it['chg']:+.2f}%), 상태 {it['state']}\n"
                f"- 차트: " + ("; ".join(f"{s['label']}({s['detail']})" for s in it["signals"][:6]) or "특이 신호 없음") + "\n"
                f"- 이슈: " + ("; ".join(f"{s['label']}({s['detail']})" for s in it["issues"][:5]) or "특이 이슈 없음") + "\n"
                f"- 모델: {ptxt}\n"
                f"- 비슷한 과거 차트 {sim.get('n', 0)}건: 20거래일 뒤 평균 {sim.get('d20_avg', 0):+.1f}%, 오른 비율 {sim.get('d20_up', 0)*100:.0f}%")
        prompt = f"""당신은 한국 주식 차트·수급 분석가입니다. 아래는 사용자가 보유 중인 종목들의 {S.TODAY} 장 마감 기준 데이터입니다.

{chr(10).join(blocks)}

[규칙]
1. 위에 적힌 숫자·사실만 쓰라. 없는 실적·계약·목표가·일정을 만들지 마라.
2. 보유자 관점에서 '오늘 무엇이 달라졌는지'(좋아진 점·나빠진 점)를 3문장 이내로.
3. 모델 숫자는 확률이라고 쓰고, "매수하라/매도하라/홀딩하라" 같은 권유 표현은 쓰지 마라.

JSON으로만 답하라: {{"stocks":[{{"code":"6자리","comment":"3문장 이내"}}]}}"""
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                              headers={"Authorization": f"Bearer {crawl.GROQ_API_KEY}", "Content-Type": "application/json"},
                              json={"model": crawl.GROQ_MODEL, "messages": [{"role": "user", "content": prompt}],
                                    "temperature": 0.3, "max_tokens": 5000, "response_format": {"type": "json_object"}},
                              timeout=90)
            if r.status_code != 200:
                log(f"  ⚠️ Groq 오류 {r.status_code}: {r.text[:200]}")
                continue
            data = json.loads(r.json()["choices"][0]["message"]["content"])
            for s in data.get("stocks", []):
                out[str(s.get("code", "")).zfill(6)] = (s.get("comment") or "").strip()
        except Exception as e:
            log(f"  ⚠️ Groq 코멘트 실패: {e}")
    return out


# ═════════════════════════════════════════
# 4. 매매별 계산
# ═════════════════════════════════════════
def ymd_int(s):
    return int(str(s).replace("-", ""))


def fmt_date(d):
    d = str(int(d)); return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def trade_fills(tr, fills):
    """매수·매도 기록 (날짜 → 매수 먼저 → id 순). 기록이 없으면(SQL 실행 전) 매매 칸으로 만듦"""
    fl = [f for f in (fills or []) if f.get("date") and f.get("price")]
    if not fl:
        fl = [{"id": 0, "side": "buy", "date": tr["buy_date"], "price": tr["buy_price"], "qty": tr.get("qty")}]
        if tr["status"] == "closed" and tr.get("sell_date") and tr.get("sell_price"):
            fl.append({"id": 1, "side": "sell", "date": tr["sell_date"], "price": tr["sell_price"], "qty": None})
    return sorted(fl, key=lambda f: (ymd_int(f["date"]), f["side"] == "sell", f.get("id") or 0))


def cost_steps(fl):
    """기록을 차례로 적용한 (날짜, 평균단가, 보유수량(모르면 None), 매도면 (매도가, 그때 평균단가)) 목록"""
    out, avg, hold, unknown = [], None, 0.0, False
    for f in fl:
        p = float(f["price"]); q = f.get("qty")
        sell = None
        if f["side"] == "buy":
            if q is None:
                unknown, avg = True, p
            else:
                q = float(q)
                avg = p if avg is None or hold <= 0 else (avg * hold + p * q) / (hold + q)
                hold += q
        else:
            sell = (p, avg)
            if unknown or q is None:
                hold = 0.0
            else:
                hold = max(0.0, hold - float(q))
        out.append((ymd_int(f["date"]), avg, None if unknown and f["side"] == "buy" else hold, sell))
    return out


def trade_update(tr, arr, code_row, trade_int, fills=None):
    """보유 기간 수익률·최고/최저·보유일 + 보유 관련 신호 (추가 매수·일부 매도면 그날까지의 평균단가 기준)"""
    steps = cost_steps(trade_fills(tr, fills))
    bd = ymd_int(tr["buy_date"])
    bp = float(steps[-1][1] or tr["buy_price"])   # 지금(마지막 기록 후) 평균단가
    closed = tr["status"] == "closed"
    end_day = min(ymd_int(tr["sell_date"]), trade_int) if closed and tr.get("sell_date") else trade_int
    d = arr[:, 0].astype(int)
    bi = int(np.searchsorted(d, bd))            # 매수일(또는 그 다음 거래일)
    ei = int(np.searchsorted(d, end_day, side="right")) - 1
    patch = {}
    hold_sig = []
    if bi >= len(arr) or ei < bi:   # 매수일이 아직 기준 거래일 뒤 (오늘 산 종목, 장 마감 전)
        patch.update({"last_date": fmt_date(trade_int), "days_held": 0,
                      "latest": {"date": fmt_date(trade_int), "waiting": True,
                                 "note": "매수일 장 마감 후 첫 분석이 나옵니다"}})
        return patch
    C = arr[bi:ei + 1, 4]
    last = float(arr[ei, 4])
    # 날짜마다 그날까지의 평균단가로 수익률 (다 판 뒤의 날은 뺌) + 매도한 가격
    rets, k, avg, hold = [], 0, None, None
    for j in range(bi, ei + 1):
        dj = int(arr[j, 0])
        while k < len(steps) and steps[k][0] <= dj:
            avg, hold = steps[k][1], steps[k][2]; k += 1
        if avg and (hold is None or hold > 0):
            rets.append((float(arr[j, 4]) / avg - 1) * 100)
    rets += [(sp / sa - 1) * 100 for _, _, _, sl in steps if sl for sp, sa in [sl] if sa]
    rets = np.array(rets or [(last / bp - 1) * 100])
    patch["max_ret"] = round(float(rets.max()), 2)
    patch["min_ret"] = round(float(rets.min()), 2)
    patch["days_held"] = int(ei - bi)
    if not closed:
        patch["last_price"] = last
        patch["last_date"] = fmt_date(int(arr[ei, 0]))
        patch["ret_cum"] = round((last / bp - 1) * 100, 2)
        peak = max(float(arr[bi:ei + 1, 4].max()), bp)
        dd = (last / peak - 1) * 100
        rc = patch["ret_cum"]
        if rc <= -10:
            hold_sig.append({"kind": "loss10", "label": f"평균단가 대비 {rc:+.1f}%", "detail": f"평균단가 {bp:,.0f}", "tone": -1})
        if dd <= -10 and peak > bp:
            hold_sig.append({"kind": "dd10", "label": f"보유 중 고점 대비 {dd:+.1f}%",
                             "detail": f"고점 종가 {peak:,.0f}", "tone": -1})
        if rc >= 20:
            hold_sig.append({"kind": "gain20", "label": f"평균단가 대비 {rc:+.1f}%", "detail": "", "tone": 1})
        if code_row:
            dat = code_row["data"]
            sigs = hold_sig + dat.get("signals", []) + dat.get("issues", [])
            st = state_label(sigs)
            patch["latest"] = {
                "date": code_row["date"], "close": last, "chg": code_row.get("change_pct"),
                "state": st, "dd": round(dd, 2),
                "signals": [{"label": s["label"], "tone": s["tone"]} for s in sorted(sigs, key=lambda x: x.get("tone", 0))[:6]],
                "hold_signals": hold_sig,
                "pred": {k: {"p_up": v["p_up"], "q50": v["q50"]} for k, v in (dat.get("pred") or {}).items()},
                "comment": dat.get("comment", ""),
            }
    else:
        patch["final_done"] = True
        patch["last_price"] = float(tr["sell_price"])
        patch["last_date"] = tr["sell_date"]
        patch["latest"] = {"date": tr["sell_date"], "closed": True}
    return patch, bi


def entry_snapshot(arr, bi, model, idx, heat, kosdaq, names, code):
    sig, info = chart_state(arr, bi)
    pred, similar, tab, seq = predict_at(model, arr, bi, idx, heat, kosdaq, names, exclude_code=code)
    snap = {"date": fmt_date(int(arr[bi, 0])), "close": float(arr[bi, 4]),
            "chg": round((arr[bi, 4] / arr[bi - 1, 4] - 1) * 100, 2) if bi >= 1 else None,
            "signals": sig, "info": info, "state": state_label(sig), "pred": pred,
            "similar": (similar or {}).get("summary")}
    if tab is not None:
        snap["features"] = {"tab_names": S.TAB_NAMES, "tab": [round(float(x), 4) for x in tab],
                            "seq": [[round(float(x), 4) for x in row] for row in seq]}
    return snap


# ═════════════════════════════════════════
# main
# ═════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--universe", type=int, default=0, help="학습 종목 수 제한 (로컬 시험용)")
    ap.add_argument("--skip-groq", action="store_true")
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--retrain", action="store_true", help="저장된 보유 모델 무시하고 다시 학습")
    ap.add_argument("--force", action="store_true", help="이미 분석한 종목도 다시 분석")
    ap.add_argument("--codes", default="", help="(시험용) 매매 대신 이 종목들 분석, 쉼표 구분")
    args = ap.parse_args()
    dry = args.dry_run or not S.HAS_SERVICE_KEY
    if dry:
        log("🧪 드라이런 — Supabase에 쓰지 않음")
    log("=" * 50)
    log(f"📒 내 매매 분석 ({S.TODAY})")

    sector_map = crawl.fetch_naver_sector_map()
    krx = crawl.fetch_naver_market_data(sector_map)
    if not krx:
        log("❌ 시세 조회 실패"); return
    names = {c: d["name"] for c, d in krx.items()}

    # 1) 종목 검색 목록 (ETF·ETN 포함 전 종목 — 새 상장 자동 반영)
    rows = stock_list_rows()
    if not dry and rows:
        ok = sb_upsert("mt_stock_list", rows, "code")
        if not ok:   # setup_mytrades.sql 재실행 전(kind 칸 없음)
            ok = sb_upsert("mt_stock_list", [{k: v for k, v in r.items() if k != "kind"} for r in rows], "code")
        if ok and len(rows) > 3000:   # 상장폐지 등으로 2주 넘게 안 나온 종목 정리
            old = (datetime.now(KST) - timedelta(days=14)).isoformat()
            requests.delete(f"{crawl.SUPABASE_URL}/rest/v1/mt_stock_list", headers=_hdr(),
                            params={"updated_at": f"lt.{old}"}, timeout=30)
        log(f"  🔎 종목 검색 목록 {len(rows)}개 {'✅' if ok else '❌'}")
    elif dry:
        log(f"  🔎 종목 검색 목록 {len(rows)}개 (드라이런 — 저장 안 함)")

    # 2) 매매 불러오기
    if args.codes:
        trades = [{"id": -i, "code": c.strip(), "name": names.get(c.strip(), c.strip()), "status": "open",
                   "buy_date": (datetime.now(KST) - timedelta(days=30)).strftime("%Y-%m-%d"),
                   "buy_price": krx.get(c.strip(), {}).get("price", 1) or 1, "entry_snapshot": None,
                   "analyze_tries": 0}
                  for i, c in enumerate(args.codes.split(","), 1) if c.strip()]
    else:
        sel = "id,code,name,status,buy_date,buy_price,qty,sell_date,sell_price,entry_snapshot,final_done,analyze_tries,last_date"
        trades = sb_all("mt_trades", {"select": sel, "status": "eq.open", "order": "id.asc"})
        trades += sb_all("mt_trades", {"select": sel, "status": "eq.closed", "final_done": "eq.false", "order": "id.asc"})
    # 아직 분석 전인 매매는 시도 횟수부터 올림 → 작업이 중간에 실패해도 크롤링이 무한히 다시 부르지 않음 (3번까지)
    if not dry and not args.codes:
        for t in trades:
            if t["status"] == "open" and not t.get("last_date"):
                t["analyze_tries"] = int(t.get("analyze_tries") or 0) + 1
                sb_patch("mt_trades", t["id"], {"analyze_tries": t["analyze_tries"]})
    # 매수·매도 기록 (setup_mytrades_fills.sql 실행 전이면 비어 있음 → 매매 칸으로 계산)
    fills_by = {}
    ids = [t["id"] for t in trades if t["id"] > 0]
    for i in range(0, len(ids), 100):
        part = ids[i:i + 100]
        rows = sb_all("mt_fills", {"select": "id,trade_id,side,date,price,qty,snap_date:snapshot->>date",
                                   "trade_id": f"in.({','.join(map(str, part))})", "order": "date.asc,id.asc"}) or []
        for f in rows:
            fills_by.setdefault(f["trade_id"], []).append(f)
    if fills_by:
        log(f"  🧾 매수·매도 기록 {sum(len(v) for v in fills_by.values())}줄")
    open_codes = sorted({t["code"] for t in trades if t["status"] == "open"})
    all_codes = sorted({t["code"] for t in trades})
    log(f"  📒 매매 {len(trades)}건 · 보유 종목 {len(open_codes)}개")

    # 3) 일봉 + 기준 거래일
    universe = [d for d in krx.values() if d.get("market_cap", 0) >= S.UNIVERSE_MIN_CAP and not crawl.is_etf_etn(d["name"])]
    universe.sort(key=lambda d: d["market_cap"], reverse=True)
    if args.universe:
        universe = universe[:args.universe]
    ucodes = sorted({d["code"] for d in universe})
    markets = {c: d.get("market", "") for c, d in krx.items()}
    cache = S.load_cache()
    got = None if args.retrain else (load_model() if trades else None)
    need_train = bool(trades) and got is None
    want = all_codes + ["KOSPI", "KOSDAQ"] + (ucodes if need_train else [])
    cache = S.update_prices(cache, sorted(set(want)))
    if not dry:
        S.save_cache(cache)
    kospi = cache.get("KOSPI")
    if kospi is None:
        log("❌ 지수 차트 조회 실패"); return
    now = datetime.now(KST)
    real_today = int(now.strftime("%Y%m%d"))
    bars = kospi[:, 0].astype(int)
    trade_int = int(bars[-1])
    if trade_int == real_today and now.hour * 60 + now.minute < 15 * 60 + 40 and len(bars) > 1:
        trade_int = int(bars[-2])
    S.TODAY = fmt_date(trade_int); S.TODAY_INT = trade_int
    log(f"  📅 기준 거래일 {S.TODAY}")
    cache = {k: (a[a[:, 0] <= trade_int] if a is not None and len(a) and a[-1, 0] > trade_int else a)
             for k, a in cache.items()}
    if trade_int != real_today:
        krx_day = S.krx_from_chart(krx, cache, trade_int)
    else:
        krx_day = krx
    idx_map = {"KOSPI": S.IndexSeries(cache.get("KOSPI")), "KOSDAQ": S.IndexSeries(cache.get("KOSDAQ"))}
    idx_today = {k: round(v.r(trade_int) * 100, 2) for k, v in idx_map.items()}

    run_today = crawl.supabase_request("GET", "mt_runs", params={
        "select": "id", "created_at": f"gte.{now.strftime('%Y-%m-%d')}T00:00:00+09:00", "limit": "1"}) if not dry else []

    if not trades:
        if not dry and not run_today:
            crawl.supabase_request("POST", "mt_runs", data=[{"date": S.TODAY, "n_trades": 0, "n_codes": 0, "metrics": {}}])
        log("  ℹ️ 분석할 매매 없음 — 종료"); return

    # 4) 보유 모델
    heat = compute_heat({c: cache[c] for c in ucodes if c in cache})
    if got:
        model, metrics = got
    else:
        model, metrics = get_model({c: cache[c] for c in ucodes if c in cache}, markets, idx_map, heat,
                                   args.seeds, retrain=True)

    # 5) 종목별 분석 (이미 오늘 분석한 종목은 건너뜀)
    done = set()
    if not dry and not args.force and open_codes:
        rows = crawl.supabase_request("GET", "mt_daily", params={
            "select": "code", "date": f"eq.{S.TODAY}", "code": f"in.({','.join(open_codes)})"}) or []
        done = {r["code"] for r in rows}
    todo = [c for c in open_codes if c not in done]
    log(f"  🔬 오늘 분석할 종목 {len(todo)}개 (이미 분석 {len(done)}개)")
    themes_raw = None
    dart = {}
    if todo:
        try:
            themes_raw = crawl.fetch_naver_themes()
        except Exception as e:
            log(f"  ⚠️ 네이버 테마 실패: {e}")
        try:
            dart = crawl.fetch_dart_disclosures(days_back=3)
        except Exception as e:
            log(f"  ⚠️ DART 실패: {e}")
    daily_rows, gitems = [], []
    for code in todo:
        arr = cache.get(code)
        me = krx_day.get(code) or {"code": code, "name": names.get(code, code), "market": markets.get(code, "")}
        if arr is None or len(arr) < 2:
            log(f"  ⚠️ {code} 일봉 없음 — 건너뜀"); continue
        t = len(arr) - 1
        close = float(arr[t, 4]); chg = round((arr[t, 4] / arr[t - 1, 4] - 1) * 100, 2)
        sig, info = chart_state(arr, t)
        kosdaq = me.get("market") == "KOSDAQ"
        idx = idx_map["KOSDAQ" if kosdaq else "KOSPI"]
        try:
            pred, similar, _, _ = predict_at(model, arr, t, idx, heat, kosdaq, names, exclude_code=code)
        except Exception as e:
            log(f"  ⚠️ {code} 예측 실패: {e}"); pred, similar = {}, {}
        news = crawl.fetch_stock_news(me.get("name", code), max_count=5)
        flow = S.fetch_flow_on(code, trade_int, close)
        th = code_themes(code, krx_day, themes_raw)
        iss, sec_info = issues_for(code, me, krx_day, idx_today, th, dart.get(code, []), news, flow)
        st = state_label(sig + iss)
        ohlcv = [[fmt_date(int(r[0])), r[1], r[2], r[3], r[4], int(r[5])] for r in arr[-OHLCV_KEEP:]]
        data = {"signals": sig, "issues": iss, "info": info, "state": st, "pred": pred, "similar": similar,
                "themes": th, "sector": sec_info, "index": idx_today, "flow": flow or {}, "news": news[:5],
                "disclosures": [{"type": d.get("type", ""), "title": d.get("title", ""), "date": d.get("date", ""),
                                 "url": d.get("url", "")} for d in dart.get(code, [])[:3]],
                "ohlcv": ohlcv, "comment": "", "model": metrics.get("trained", "")}
        daily_rows.append({"code": code, "date": S.TODAY, "name": me.get("name", code), "close": close,
                           "change_pct": chg, "data": data})
        gitems.append({"code": code, "name": me.get("name", code), "close": close, "chg": chg, "state": st["label"],
                       "signals": sig, "issues": iss, "pred": pred, "similar": similar})
        log(f"  · {me.get('name', code)} {chg:+.2f}% · {st['label']} · "
            + ", ".join(s["label"] for s in sig[:3])
            + (f" · 20일 뒤 상승확률 {pred['d20']['p_up']*100:.0f}%" if pred else ""))
    if gitems and not args.skip_groq:
        com = groq_comments(gitems)
        for r in daily_rows:
            r["data"]["comment"] = com.get(r["code"], "")
        log(f"  🤖 AI 코멘트 {sum(1 for r in daily_rows if r['data']['comment'])}/{len(daily_rows)}")
    if daily_rows and not dry:
        ok = sb_upsert("mt_daily", daily_rows, "code,date")
        log(f"  💾 종목 분석 {len(daily_rows)}개 저장 {'✅' if ok else '❌'}")

    # 6) 매매별 갱신 (오늘 분석이 없던 종목은 저장된 오늘 분석을 읽어 씀)
    code_rows = {r["code"]: r for r in daily_rows}
    miss = [c for c in open_codes if c not in code_rows]
    if miss and not dry:
        for r in crawl.supabase_request("GET", "mt_daily", params={
                "select": "code,date,change_pct,data", "date": f"eq.{S.TODAY}", "code": f"in.({','.join(miss)})"}) or []:
            code_rows[r["code"]] = r
    n_up = 0
    for tr in trades:
        arr = cache.get(tr["code"])
        if arr is None or len(arr) < 2:
            log(f"  ⚠️ 매매 #{tr['id']} {tr['code']} 일봉 없음")
            continue
        res = trade_update(tr, arr, code_rows.get(tr["code"]), trade_int, fills_by.get(tr["id"]))
        if isinstance(res, tuple):
            patch, bi = res
            if not tr.get("entry_snapshot") and bi >= 1:
                try:
                    kosdaq = markets.get(tr["code"]) == "KOSDAQ"
                    patch["entry_snapshot"] = entry_snapshot(arr, bi, model, idx_map["KOSDAQ" if kosdaq else "KOSPI"],
                                                             heat, kosdaq, names, tr["code"])
                except Exception as e:
                    log(f"  ⚠️ 매매 #{tr['id']} 매수 시점 분석 실패: {e}")
        else:
            patch = res
        # 추가 매수·매도한 날의 차트·지표·AI 예측 (첫 매수는 entry_snapshot)
        fl = trade_fills(tr, fills_by.get(tr["id"]))
        dser = arr[:, 0].astype(int)
        for f in fl[1:]:
            if f.get("snap_date") or not f.get("id") or ymd_int(f["date"]) > trade_int:
                continue
            fi = int(np.searchsorted(dser, ymd_int(f["date"])))
            if fi < 1 or fi >= len(arr) or int(arr[fi, 0]) > trade_int:
                continue
            try:
                kosdaq = markets.get(tr["code"]) == "KOSDAQ"
                snap = entry_snapshot(arr, fi, model, idx_map["KOSDAQ" if kosdaq else "KOSPI"], heat, kosdaq, names, tr["code"])
                snap["side"] = f["side"]
                if dry:
                    log(f"     [드라이런] 기록 #{f['id']} {f['side']} {f['date']} → {snap['state']['label']}")
                elif sb_patch("mt_fills", f["id"], {"snapshot": snap}):
                    log(f"  📸 매매 #{tr['id']} {'추가 매수' if f['side'] == 'buy' else '매도'} {f['date']} 시점 기록")
            except Exception as e:
                log(f"  ⚠️ 기록 #{f.get('id')} 시점 분석 실패: {e}")
        if dry:
            log(f"  [드라이런] 매매 #{tr['id']} {tr['name']}: " +
                json.dumps({k: v for k, v in patch.items() if k not in ("entry_snapshot",)}, ensure_ascii=False, default=float)[:300])
            if patch.get("entry_snapshot"):
                es = patch["entry_snapshot"]
                log(f"     매수 시점 {es['date']} · {es['state']['label']} · "
                    + ", ".join(s["label"] for s in es["signals"][:4])
                    + (f" · 20일 뒤 상승확률 {es['pred']['d20']['p_up']*100:.0f}%" if es.get("pred") else ""))
            continue
        if sb_patch("mt_trades", tr["id"], patch):
            n_up += 1
    log(f"  📒 매매 {n_up}건 갱신")

    if not dry and (daily_rows or not run_today):
        crawl.supabase_request("POST", "mt_runs", data=[{
            "date": S.TODAY, "n_trades": len(trades), "n_codes": len(daily_rows),
            "metrics": json.loads(json.dumps(metrics or {}, default=float))}])
    # 보관: 종목 분석은 2년, 실행 기록 200개
    if not dry:
        cut = (now - timedelta(days=730)).strftime("%Y-%m-%d")
        crawl.supabase_request("DELETE", "mt_daily", params={"date": f"lt.{cut}"})
        old = crawl.supabase_request("GET", "mt_runs", params={"select": "id", "order": "id.desc", "offset": "200", "limit": "500"}) or []
        for o in old:
            crawl.supabase_request("DELETE", "mt_runs", params={"id": f"eq.{o['id']}"})
    log("✅ 완료")


if __name__ == "__main__":
    main()

-- ═══════════════════════════════════════════════════════════
-- 내 매매 분석 (index.html #mytrade) — 1회 실행, 다시 실행해도 안전
--
-- 로그인: Supabase Auth (구글). 행마다 user_id를 두고 RLS로 본인 행만 읽기·쓰기.
-- 공개: 매매마다 is_public을 켜면 다른 사람도 "공개 매매"에서 볼 수 있음
--       (닉네임·종목·매수일·매수가·수익률·매수 이유만, 수량·메모는 절대 안 보임)
-- 분석: GitHub Actions(mytrades.yml, mytrades_ai.py)가 service key로 매일 채움
-- ═══════════════════════════════════════════════════════════

-- ── 1. 프로필 (닉네임, 새 매수 기본 공개) ──
CREATE TABLE IF NOT EXISTS mt_profiles (
  user_id        UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  nickname       TEXT NOT NULL DEFAULT '',
  default_public BOOLEAN NOT NULL DEFAULT FALSE,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT mt_profiles_nick_len CHECK (char_length(nickname) <= 12)
);

-- ── 2. 매매 ──
CREATE TABLE IF NOT EXISTS mt_trades (
  id            BIGSERIAL PRIMARY KEY,
  user_id       UUID NOT NULL DEFAULT auth.uid() REFERENCES auth.users(id) ON DELETE CASCADE,
  code          TEXT NOT NULL,
  name          TEXT NOT NULL,
  market        TEXT NOT NULL DEFAULT '',
  buy_date      DATE NOT NULL,
  buy_price     NUMERIC NOT NULL,
  qty           NUMERIC,
  buy_reason    TEXT NOT NULL DEFAULT '',
  memo          TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL DEFAULT 'open',
  sell_date     DATE,
  sell_price    NUMERIC,
  sell_reason   TEXT NOT NULL DEFAULT '',
  is_public     BOOLEAN NOT NULL DEFAULT FALSE,
  -- ↓ 서버(분석 작업)만 채우는 칸 (사용자는 쓰기 권한 없음)
  last_price    NUMERIC,
  last_date     DATE,
  ret_cum       NUMERIC,      -- 매수가 대비 수익률 % (보유 중: 마지막 종가 / 매도: 매도가)
  max_ret       NUMERIC,      -- 보유 기간 최고 종가 수익률 %
  min_ret       NUMERIC,      -- 보유 기간 최저 종가 수익률 %
  days_held     INT,          -- 보유 거래일 수
  latest        JSONB,        -- 최근 분석 요약 (상태·신호·예측·AI 한 줄)
  entry_snapshot JSONB,       -- 매수일 기준 차트·지표·AI 예측 (2단계 학습용)
  final_done    BOOLEAN NOT NULL DEFAULT FALSE,   -- 매도 후 최종 계산 끝
  analyze_tries INT NOT NULL DEFAULT 0,           -- 첫 분석 실패 횟수 (자동 재실행 제한)
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT mt_trades_code    CHECK (code ~ '^[0-9A-Z]{6}$'),
  CONSTRAINT mt_trades_price   CHECK (buy_price > 0),
  CONSTRAINT mt_trades_qty     CHECK (qty IS NULL OR qty > 0),
  CONSTRAINT mt_trades_status  CHECK (status IN ('open', 'closed')),
  CONSTRAINT mt_trades_sell    CHECK (status = 'open' OR (sell_date IS NOT NULL AND sell_price > 0)),
  CONSTRAINT mt_trades_selldt  CHECK (sell_date IS NULL OR sell_date >= buy_date),
  CONSTRAINT mt_trades_len     CHECK (char_length(name) <= 40 AND char_length(buy_reason) <= 500
                                      AND char_length(memo) <= 1000 AND char_length(sell_reason) <= 500)
);
CREATE INDEX IF NOT EXISTS idx_mt_trades_user   ON mt_trades(user_id, status, buy_date DESC);
CREATE INDEX IF NOT EXISTS idx_mt_trades_code   ON mt_trades(code, status);
CREATE INDEX IF NOT EXISTS idx_mt_trades_public ON mt_trades(is_public, status) WHERE is_public;

-- ── 3. 종목별 일일 분석 (같은 종목은 사는 사람이 여럿이어도 하루 1번만 계산) ──
CREATE TABLE IF NOT EXISTS mt_daily (
  code        TEXT NOT NULL,
  date        DATE NOT NULL,
  name        TEXT NOT NULL DEFAULT '',
  close       NUMERIC,
  change_pct  NUMERIC,
  data        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- 신호·이슈·수급·예측·유사 사례·AI 코멘트
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_mt_daily_date ON mt_daily(date);

-- ── 4. 종목 검색용 목록 (분석 작업이 매일 갱신) ──
CREATE TABLE IF NOT EXISTS mt_stock_list (
  code        TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  market      TEXT NOT NULL DEFAULT '',
  price       NUMERIC,
  change_pct  NUMERIC,
  market_cap  NUMERIC,
  kind        TEXT NOT NULL DEFAULT '',   -- '' 일반·우선주 / ETF / ETN
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE mt_stock_list ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT '';

-- ── 5. 분석 실행 기록 (모델 성적) ──
CREATE TABLE IF NOT EXISTS mt_runs (
  id         BIGSERIAL PRIMARY KEY,
  date       DATE NOT NULL,
  n_trades   INT NOT NULL DEFAULT 0,
  n_codes    INT NOT NULL DEFAULT 0,
  metrics    JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mt_runs_date ON mt_runs(date DESC);

-- ═══ 트리거 ═══
-- 닉네임: 공백 정리, 금지·예약 닉네임 거부
CREATE OR REPLACE FUNCTION mt_profiles_guard() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.nickname := btrim(regexp_replace(COALESCE(NEW.nickname, ''), '\s+', ' ', 'g'));
  IF NEW.nickname <> '' AND (
       LOWER(NEW.nickname) IN ('운영자', '관리자', 'admin', '운영진', '매니저')
       OR EXISTS (SELECT 1 FROM banned_nicknames b WHERE LOWER(b.nickname) = LOWER(NEW.nickname))) THEN
    RAISE EXCEPTION 'banned_nickname';
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_mt_profiles_guard ON mt_profiles;
CREATE TRIGGER trg_mt_profiles_guard BEFORE INSERT OR UPDATE ON mt_profiles
  FOR EACH ROW EXECUTE FUNCTION mt_profiles_guard();

-- 매매: 보유 중 종목 50개 제한, 날짜 검사, 매수 정보가 바뀌면 서버 계산 다시
CREATE OR REPLACE FUNCTION mt_trades_guard() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.buy_date > (now() AT TIME ZONE 'Asia/Seoul')::date THEN
    RAISE EXCEPTION 'future_date';
  END IF;
  IF NEW.sell_date IS NOT NULL AND NEW.sell_date > (now() AT TIME ZONE 'Asia/Seoul')::date THEN
    RAISE EXCEPTION 'future_date';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF (SELECT COUNT(*) FROM mt_trades WHERE user_id = NEW.user_id AND status = 'open') >= 50 THEN
      RAISE EXCEPTION 'too_many_open';
    END IF;
  ELSE
    IF NEW.status = 'open' AND OLD.status = 'closed'
       AND (SELECT COUNT(*) FROM mt_trades WHERE user_id = NEW.user_id AND status = 'open') >= 50 THEN
      RAISE EXCEPTION 'too_many_open';
    END IF;
    IF NEW.buy_date IS DISTINCT FROM OLD.buy_date OR NEW.buy_price IS DISTINCT FROM OLD.buy_price THEN
      NEW.entry_snapshot := NULL;
      NEW.final_done := FALSE;
      NEW.max_ret := NULL; NEW.min_ret := NULL;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status OR NEW.sell_date IS DISTINCT FROM OLD.sell_date
       OR NEW.sell_price IS DISTINCT FROM OLD.sell_price THEN
      NEW.final_done := FALSE;
    END IF;
  END IF;
  IF NEW.status = 'open' THEN
    NEW.sell_date := NULL; NEW.sell_price := NULL;
  ELSE
    NEW.ret_cum := round((NEW.sell_price / NEW.buy_price - 1) * 100, 2);
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_mt_trades_guard ON mt_trades;
CREATE TRIGGER trg_mt_trades_guard BEFORE INSERT OR UPDATE ON mt_trades
  FOR EACH ROW EXECUTE FUNCTION mt_trades_guard();

-- ═══ 권한 + RLS ═══
ALTER TABLE mt_profiles   ENABLE ROW LEVEL SECURITY;
ALTER TABLE mt_trades     ENABLE ROW LEVEL SECURITY;
ALTER TABLE mt_daily      ENABLE ROW LEVEL SECURITY;
ALTER TABLE mt_stock_list ENABLE ROW LEVEL SECURITY;
ALTER TABLE mt_runs       ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON mt_profiles, mt_trades, mt_daily, mt_stock_list, mt_runs FROM anon, authenticated;

-- 프로필: 본인만
GRANT SELECT, DELETE ON mt_profiles TO authenticated;
GRANT INSERT (user_id, nickname, default_public), UPDATE (nickname, default_public) ON mt_profiles TO authenticated;
DROP POLICY IF EXISTS mt_profiles_own ON mt_profiles;
CREATE POLICY mt_profiles_own ON mt_profiles FOR ALL TO authenticated
  USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

-- 매매: 본인만 (사용자가 쓸 수 있는 칸은 아래 목록뿐 — 수익률 등 서버 칸은 못 바꿈)
GRANT SELECT, DELETE ON mt_trades TO authenticated;
GRANT INSERT (code, name, market, buy_date, buy_price, qty, buy_reason, memo, is_public) ON mt_trades TO authenticated;
GRANT UPDATE (name, buy_date, buy_price, qty, buy_reason, memo, status, sell_date, sell_price, sell_reason, is_public)
  ON mt_trades TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE mt_trades_id_seq TO authenticated;
DROP POLICY IF EXISTS mt_trades_own ON mt_trades;
CREATE POLICY mt_trades_own ON mt_trades FOR ALL TO authenticated
  USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

-- 일일 분석: 내가 가진 종목이거나 누군가 공개한 종목만
-- (정책 안에서 mt_trades를 직접 보면 그쪽 RLS에 막히므로 소유자 권한 함수로 확인)
CREATE OR REPLACE FUNCTION mt_code_visible(p_code TEXT) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (SELECT 1 FROM mt_trades t
                 WHERE t.code = p_code AND (t.is_public OR t.user_id = auth.uid()));
$$;
REVOKE ALL ON FUNCTION mt_code_visible(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mt_code_visible(TEXT) TO anon, authenticated;
GRANT SELECT ON mt_daily TO anon, authenticated;
DROP POLICY IF EXISTS mt_daily_read ON mt_daily;
CREATE POLICY mt_daily_read ON mt_daily FOR SELECT TO anon, authenticated
  USING (mt_code_visible(code));

-- 종목 목록·실행 기록: 누구나 읽기
GRANT SELECT ON mt_stock_list, mt_runs TO anon, authenticated;
DROP POLICY IF EXISTS mt_stock_list_read ON mt_stock_list;
CREATE POLICY mt_stock_list_read ON mt_stock_list FOR SELECT TO anon, authenticated USING (true);
DROP POLICY IF EXISTS mt_runs_read ON mt_runs;
CREATE POLICY mt_runs_read ON mt_runs FOR SELECT TO anon, authenticated USING (true);

-- ═══ 공개 매매 (다른 사람도 보는 것) ═══
-- 뷰 소유자 권한으로 실행 → RLS를 건너뛰되 공개한 행 + 정해진 칸만 내보냄
-- (user_id·수량·메모는 넣지 않음)
CREATE OR REPLACE VIEW mt_public_trades AS
SELECT t.id,
       COALESCE(NULLIF(p.nickname, ''), '익명') AS nickname,
       t.code, t.name, t.market, t.buy_date, t.buy_price, t.buy_reason,
       t.status, t.sell_date, t.sell_price, t.sell_reason,
       t.last_price, t.last_date, t.ret_cum, t.max_ret, t.min_ret, t.days_held, t.latest,
       t.created_at
  FROM mt_trades t
  LEFT JOIN mt_profiles p ON p.user_id = t.user_id
 WHERE t.is_public;
REVOKE ALL ON mt_public_trades FROM anon, authenticated;
GRANT SELECT ON mt_public_trades TO anon, authenticated;

-- ═══ 탈퇴 (내 데이터와 로그인 계정 모두 삭제) ═══
CREATE OR REPLACE FUNCTION mt_delete_my_account() RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, auth AS $$
DECLARE uid UUID := auth.uid();
BEGIN
  IF uid IS NULL THEN RAISE EXCEPTION 'not_logged_in'; END IF;
  DELETE FROM mt_trades WHERE user_id = uid;
  DELETE FROM mt_profiles WHERE user_id = uid;
  DELETE FROM auth.users WHERE id = uid;
END $$;
REVOKE ALL ON FUNCTION mt_delete_my_account() FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION mt_delete_my_account() TO authenticated;

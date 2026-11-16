-- 주도주 단타 (선정 다음날·다다음날 장중 매매 시뮬레이션) — Supabase SQL Editor에서 1회 실행 (재실행 안전)
-- 크롤러(service role)가 쓰고, 사이트는 읽기만 함
CREATE TABLE IF NOT EXISTS leader_scalp (
  id            BIGSERIAL PRIMARY KEY,
  pick_date     TEXT NOT NULL,          -- 주도주 선정일 (leader_track.pick_date)
  code          TEXT NOT NULL,
  name          TEXT,
  theme_name    TEXT,
  rank          INT,
  day_no        INT NOT NULL,           -- 1 = 다음 거래일, 2 = 그다음 거래일
  trade_date    TEXT NOT NULL,          -- 실제 매매일
  ref_close     NUMERIC,                -- 기준가 = 선정일 종가 (1·2일차 모두)
  day_open      NUMERIC,
  day_high      NUMERIC,
  day_low       NUMERIC,
  day_close     NUMERIC,
  mode          TEXT,                   -- up(시가 > 기준가) | down(시가 <= 기준가)
  status        TEXT,                   -- done(매수함) | no_entry(매수 조건 안 옴)
  base_low      NUMERIC,                -- 구간 아래 (up = 기준가, down = 그때까지 저점)
  base_high     NUMERIC,                -- 1차 매수 때까지의 고가
  buy1_price    NUMERIC,
  buy1_time     TEXT,                   -- HH:MM
  buy2_price    NUMERIC,
  buy2_time     TEXT,
  avg_price     NUMERIC,                -- 같은 금액 매수 기준 평균단가
  take_price    NUMERIC,                -- 평균단가 +2%
  stop_price    NUMERIC,                -- up = 기준가, down = 저점 (이 아래로 내려가면 손절)
  exit_price    NUMERIC,
  exit_time     TEXT,
  exit_reason   TEXT,                   -- tp(익절) | sl(손절) | close(장 마감 전 매도)
  ret           NUMERIC,                -- 수익률 %
  result        TEXT,                   -- win | loss (no_entry 는 NULL)
  created_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE (pick_date, code, day_no)
);

CREATE INDEX IF NOT EXISTS idx_leader_scalp_pick ON leader_scalp (pick_date DESC, rank);

ALTER TABLE leader_scalp ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "leader_scalp read" ON leader_scalp;
CREATE POLICY "leader_scalp read" ON leader_scalp FOR SELECT USING (true);
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON leader_scalp FROM anon, authenticated;
GRANT SELECT ON leader_scalp TO anon, authenticated;

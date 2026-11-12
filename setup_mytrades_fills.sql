-- ═══════════════════════════════════════════════════════════
-- 내 매매 — 추가 매수(물타기)·일부 매도 기록 (setup_mytrades.sql 다음에 1회 실행, 다시 실행해도 안전)
--
-- 매매(mt_trades) 1건 아래에 매수·매도 기록(mt_fills)을 여러 줄 쌓음.
-- 기록이 바뀔 때마다 DB가 매매 칸을 다시 계산:
--   buy_date = 첫 매수일, first_buy_price = 첫 매수가, buy_price = 지금 평균단가(이동평균),
--   qty = 보유 수량(다 팔았으면 판 수량), 보유 0이 되면 status = closed,
--   sell_date = 마지막 매도일, sell_price = 매도 평균가, realized_pnl = 실현 손익
-- 사용자는 매매의 매수·매도 칸을 직접 못 고침 → 기록(mt_fills)으로만
-- 기존 매매는 첫 매수 1줄(+ 매도했으면 매도 1줄)로 자동 옮김
-- ═══════════════════════════════════════════════════════════

-- ── 1. 매매에 요약 칸 추가 (서버만 채움) ──
ALTER TABLE mt_trades ADD COLUMN IF NOT EXISTS first_buy_price NUMERIC;
ALTER TABLE mt_trades ADD COLUMN IF NOT EXISTS n_buys       INT;
ALTER TABLE mt_trades ADD COLUMN IF NOT EXISTS n_sells      INT;
ALTER TABLE mt_trades ADD COLUMN IF NOT EXISTS buy_qty      NUMERIC;   -- 총 매수 수량
ALTER TABLE mt_trades ADD COLUMN IF NOT EXISTS buy_amount   NUMERIC;   -- 총 매수 금액
ALTER TABLE mt_trades ADD COLUMN IF NOT EXISTS sold_qty     NUMERIC;   -- 총 매도 수량
ALTER TABLE mt_trades ADD COLUMN IF NOT EXISTS sold_amount  NUMERIC;   -- 총 매도 금액
ALTER TABLE mt_trades ADD COLUMN IF NOT EXISTS sold_cost    NUMERIC;   -- 판 수량의 원가(그때 평균단가 × 수량)
ALTER TABLE mt_trades ADD COLUMN IF NOT EXISTS realized_pnl NUMERIC;   -- 실현 손익 (원)

-- ── 2. 매수·매도 기록 ──
CREATE TABLE IF NOT EXISTS mt_fills (
  id         BIGSERIAL PRIMARY KEY,
  trade_id   BIGINT NOT NULL REFERENCES mt_trades(id) ON DELETE CASCADE,
  user_id    UUID NOT NULL DEFAULT auth.uid() REFERENCES auth.users(id) ON DELETE CASCADE,
  side       TEXT NOT NULL,
  date       DATE NOT NULL,
  price      NUMERIC NOT NULL,
  qty        NUMERIC,            -- 매수: 예전 매매(수량 없음)만 NULL 가능 / 매도: NULL = 남은 수량 전부
  memo       TEXT NOT NULL DEFAULT '',
  snapshot   JSONB,              -- 그날 차트·지표·AI 예측 (분석 작업이 채움, 첫 매수는 mt_trades.entry_snapshot)
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT mt_fills_side  CHECK (side IN ('buy', 'sell')),
  CONSTRAINT mt_fills_price CHECK (price > 0),
  CONSTRAINT mt_fills_qty   CHECK (qty IS NULL OR qty > 0),
  CONSTRAINT mt_fills_memo  CHECK (char_length(memo) <= 300)
);
CREATE INDEX IF NOT EXISTS idx_mt_fills_trade ON mt_fills(trade_id, date, id);

-- ── 3. 매매 다시 계산 ──
CREATE OR REPLACE FUNCTION mt_recalc_trade(p_id BIGINT) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  t mt_trades%ROWTYPE; f RECORD;
  hold NUMERIC := 0; avgc NUMERIC; unknown BOOLEAN := FALSE; closed BOOLEAN := FALSE;
  nb INT := 0; ns INT := 0; first_d DATE; first_p NUMERIC;
  bq NUMERIC := 0; ba NUMERIC := 0; sq NUMERIC := 0; sa NUMERIC := 0; sc NUMERIC := 0;
  last_sell DATE; last_sell_p NUMERIC; q NUMERIC;
BEGIN
  SELECT * INTO t FROM mt_trades WHERE id = p_id FOR UPDATE;
  IF NOT FOUND THEN RETURN; END IF;           -- 매매 삭제 중 (기록이 같이 지워지는 중)
  FOR f IN SELECT * FROM mt_fills WHERE trade_id = p_id ORDER BY date, (side = 'sell'), id LOOP
    IF closed THEN RAISE EXCEPTION 'fill_after_close'; END IF;
    IF f.side = 'buy' THEN
      nb := nb + 1;
      IF nb = 1 THEN first_d := f.date; first_p := f.price; END IF;
      IF f.qty IS NULL THEN
        IF nb > 1 THEN RAISE EXCEPTION 'qty_required'; END IF;
        unknown := TRUE; avgc := f.price;
      ELSE
        IF unknown THEN RAISE EXCEPTION 'qty_required'; END IF;
        avgc := (COALESCE(avgc, 0) * hold + f.price * f.qty) / (hold + f.qty);
        hold := hold + f.qty; bq := bq + f.qty; ba := ba + f.price * f.qty;
      END IF;
    ELSE
      IF nb = 0 THEN RAISE EXCEPTION 'sell_before_buy'; END IF;
      ns := ns + 1; last_sell := f.date; last_sell_p := f.price;
      IF unknown THEN
        IF f.qty IS NOT NULL THEN RAISE EXCEPTION 'qty_required'; END IF;
        closed := TRUE;                       -- 수량 모르는 예전 매매: 전량 매도로 봄
      ELSE
        q := COALESCE(f.qty, hold);
        IF q > hold + 0.000001 THEN RAISE EXCEPTION 'oversell'; END IF;
        sq := sq + q; sa := sa + f.price * q; sc := sc + avgc * q;
        hold := hold - q;
        IF hold <= 0.000001 THEN hold := 0; closed := TRUE; END IF;
      END IF;
    END IF;
  END LOOP;
  IF nb = 0 THEN RAISE EXCEPTION 'no_buy'; END IF;
  IF nb + ns > 60 THEN RAISE EXCEPTION 'too_many_fills'; END IF;

  UPDATE mt_trades SET
    buy_date        = first_d,
    first_buy_price = first_p,
    buy_price       = round(avgc, 4),
    qty             = CASE WHEN unknown THEN NULL WHEN closed THEN NULLIF(sq, 0) ELSE hold END,
    status          = CASE WHEN closed THEN 'closed' ELSE 'open' END,
    sell_date       = CASE WHEN closed THEN last_sell END,
    sell_price      = CASE WHEN NOT closed THEN NULL WHEN unknown THEN last_sell_p ELSE round(sa / sq, 4) END,
    n_buys = nb, n_sells = ns,
    buy_qty      = CASE WHEN unknown THEN NULL ELSE bq END,
    buy_amount   = CASE WHEN unknown THEN NULL ELSE round(ba) END,
    sold_qty     = CASE WHEN unknown THEN NULL ELSE sq END,
    sold_amount  = CASE WHEN unknown THEN NULL ELSE round(sa) END,
    sold_cost    = CASE WHEN unknown THEN NULL ELSE round(sc, 4) END,
    realized_pnl = CASE WHEN unknown OR sq = 0 THEN NULL ELSE round(sa - sc) END,
    ret_cum      = CASE WHEN NOT closed AND t.last_price IS NOT NULL
                        THEN round((t.last_price / avgc - 1) * 100, 2) ELSE t.ret_cum END,
    final_done   = FALSE
  WHERE id = p_id;
END $$;
REVOKE ALL ON FUNCTION mt_recalc_trade(BIGINT) FROM PUBLIC, anon, authenticated;

-- ── 4. 매매 트리거 다시 정의: 평균단가가 바뀌어도 매수 시점 기록은 유지 (첫 매수가 바뀔 때만 초기화) ──
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
    IF NEW.buy_date IS DISTINCT FROM OLD.buy_date
       OR COALESCE(NEW.first_buy_price, NEW.buy_price) IS DISTINCT FROM COALESCE(OLD.first_buy_price, OLD.buy_price) THEN
      NEW.entry_snapshot := NULL;
    END IF;
    IF NEW.buy_date IS DISTINCT FROM OLD.buy_date OR NEW.buy_price IS DISTINCT FROM OLD.buy_price THEN
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
  ELSIF NEW.sold_cost IS NOT NULL AND NEW.sold_cost > 0 THEN
    NEW.ret_cum := round((NEW.sold_amount / NEW.sold_cost - 1) * 100, 2);   -- 판 금액 ÷ 판 수량의 원가
  ELSE
    NEW.ret_cum := round((NEW.sell_price / NEW.buy_price - 1) * 100, 2);
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$;

-- ── 5. 기록 트리거 ──
CREATE OR REPLACE FUNCTION mt_fills_guard() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_owner UUID;
BEGIN
  IF NEW.date > (now() AT TIME ZONE 'Asia/Seoul')::date THEN RAISE EXCEPTION 'future_date'; END IF;
  SELECT user_id INTO v_owner FROM mt_trades WHERE id = NEW.trade_id;
  IF v_owner IS NULL OR NEW.user_id IS DISTINCT FROM v_owner THEN RAISE EXCEPTION 'not_owner'; END IF;
  NEW.memo := btrim(COALESCE(NEW.memo, ''));
  IF TG_OP = 'UPDATE' THEN
    IF NEW.trade_id <> OLD.trade_id THEN RAISE EXCEPTION 'not_owner'; END IF;
    IF NEW.date IS DISTINCT FROM OLD.date OR NEW.side IS DISTINCT FROM OLD.side THEN
      NEW.snapshot := NULL;
    END IF;
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION mt_fills_after() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  PERFORM mt_recalc_trade(COALESCE(NEW.trade_id, OLD.trade_id));
  RETURN NULL;
END $$;

-- 새 매매를 넣으면 첫 매수 기록을 자동으로 만듦
CREATE OR REPLACE FUNCTION mt_trades_first_fill() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM mt_fills WHERE trade_id = NEW.id) THEN
    INSERT INTO mt_fills (trade_id, user_id, side, date, price, qty)
    VALUES (NEW.id, NEW.user_id, 'buy', NEW.buy_date, NEW.buy_price, NEW.qty);
  END IF;
  RETURN NULL;
END $$;

-- ── 6. 기존 매매 옮기기 (트리거를 잠시 빼고 넣은 뒤 한 번에 계산) ──
DROP TRIGGER IF EXISTS trg_mt_fills_guard ON mt_fills;
DROP TRIGGER IF EXISTS trg_mt_fills_after ON mt_fills;
DROP TRIGGER IF EXISTS trg_mt_trades_first_fill ON mt_trades;

DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT t.* FROM mt_trades t WHERE NOT EXISTS (SELECT 1 FROM mt_fills f WHERE f.trade_id = t.id) LOOP
    INSERT INTO mt_fills (trade_id, user_id, side, date, price, qty, created_at)
    VALUES (r.id, r.user_id, 'buy', r.buy_date, r.buy_price, r.qty, r.created_at);
    IF r.status = 'closed' AND r.sell_date IS NOT NULL AND r.sell_price IS NOT NULL THEN
      INSERT INTO mt_fills (trade_id, user_id, side, date, price, qty, memo, created_at)
      VALUES (r.id, r.user_id, 'sell', r.sell_date, r.sell_price, NULL, '', r.updated_at);
    END IF;
    PERFORM mt_recalc_trade(r.id);
  END LOOP;
END $$;

CREATE TRIGGER trg_mt_fills_guard BEFORE INSERT OR UPDATE ON mt_fills
  FOR EACH ROW EXECUTE FUNCTION mt_fills_guard();
CREATE TRIGGER trg_mt_fills_after AFTER INSERT OR UPDATE OR DELETE ON mt_fills
  FOR EACH ROW EXECUTE FUNCTION mt_fills_after();
CREATE TRIGGER trg_mt_trades_first_fill AFTER INSERT ON mt_trades
  FOR EACH ROW EXECUTE FUNCTION mt_trades_first_fill();

-- ── 7. 권한 ──
ALTER TABLE mt_fills ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON mt_fills FROM anon, authenticated;
GRANT SELECT, DELETE ON mt_fills TO authenticated;
GRANT INSERT (trade_id, side, date, price, qty, memo) ON mt_fills TO authenticated;
GRANT UPDATE (date, price, qty, memo) ON mt_fills TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE mt_fills_id_seq TO authenticated;
DROP POLICY IF EXISTS mt_fills_own ON mt_fills;
CREATE POLICY mt_fills_own ON mt_fills FOR ALL TO authenticated
  USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

-- 매매의 매수·매도 칸은 이제 기록으로만 바뀜 (이름·이유·메모·공개만 직접 수정)
REVOKE UPDATE ON mt_trades FROM authenticated;
GRANT UPDATE (name, buy_reason, memo, sell_reason, is_public) ON mt_trades TO authenticated;

-- ── 8. 공개 매매: 기록(날짜·가격만, 수량 없음) + 요약 칸 ──
CREATE OR REPLACE VIEW mt_public_trades AS
SELECT t.id,
       COALESCE(NULLIF(p.nickname, ''), '익명') AS nickname,
       t.code, t.name, t.market, t.buy_date, t.buy_price, t.buy_reason,
       t.status, t.sell_date, t.sell_price, t.sell_reason,
       t.last_price, t.last_date, t.ret_cum, t.max_ret, t.min_ret, t.days_held, t.latest,
       t.created_at,
       t.first_buy_price, t.n_buys, t.n_sells
  FROM mt_trades t
  LEFT JOIN mt_profiles p ON p.user_id = t.user_id
 WHERE t.is_public;
REVOKE ALL ON mt_public_trades FROM anon, authenticated;
GRANT SELECT ON mt_public_trades TO anon, authenticated;

CREATE OR REPLACE VIEW mt_public_fills AS
SELECT f.id, f.trade_id, f.side, f.date, f.price
  FROM mt_fills f
  JOIN mt_trades t ON t.id = f.trade_id
 WHERE t.is_public;
REVOKE ALL ON mt_public_fills FROM anon, authenticated;
GRANT SELECT ON mt_public_fills TO anon, authenticated;

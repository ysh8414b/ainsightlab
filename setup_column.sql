-- ═══════════════════════════════════════════════════════════
-- 칼럼 (반자동 글쓰기) — Supabase SQL Editor 에서 1회 실행 (재실행 안전)
--
-- 흐름
--   1) 크롤링(평일 장 마감 후)이 그날 데이터로 AI 초안을 만들어 column_posts 에 저장 (status=draft)
--   2) admin.html → "✍ 칼럼" 에서 초안을 고치고 '운영자 의견'을 써서 [발행] (status=approved)
--   3) 다음 크롤링이 approved/published 글로 column/*.html 을 만들고 커밋 (status=published)
--
-- 이 테이블은 공개 키(anon)로 읽을 수 없음 — 관리자 RPC(비밀번호 해시 확인)와
-- 크롤러(service role)만 접근. 공개되는 건 커밋된 column/*.html 뿐
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS column_posts (
  id BIGSERIAL PRIMARY KEY,
  date TEXT NOT NULL,                          -- 기준 거래일 (YYYY-MM-DD)
  slug TEXT UNIQUE,                            -- column/{slug}.html
  kind TEXT NOT NULL DEFAULT 'auto' CHECK (kind IN ('auto','manual')),  -- auto = AI 초안, manual = 직접 쓴 글
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',            -- 목록·검색 설명용 한두 문장
  body TEXT NOT NULL DEFAULT '',               -- 본문 (간단한 마크다운: ## 제목, - 목록, **굵게**)
  opinion TEXT NOT NULL DEFAULT '',            -- 운영자 의견 (AI 초안은 발행 때 필수)
  data JSONB,                                  -- 초안을 만들 때 쓴 데이터 (참고용)
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','published','discarded')),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  published_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_column_posts_status ON column_posts(status, date DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_column_posts_auto_date ON column_posts(date) WHERE kind = 'auto';

ALTER TABLE column_posts ENABLE ROW LEVEL SECURITY;   -- 정책 없음 → anon/authenticated 조회·쓰기 불가
REVOKE ALL ON column_posts FROM anon, authenticated;
REVOKE ALL ON SEQUENCE column_posts_id_seq FROM anon, authenticated;

-- 최고관리자 확인
CREATE OR REPLACE FUNCTION column_admin_ok(p_admin_hash TEXT) RETURNS BOOLEAN AS $$
  SELECT EXISTS (SELECT 1 FROM board_admins WHERE password_hash = p_admin_hash AND role = 'superadmin');
$$ LANGUAGE sql SECURITY DEFINER STABLE SET search_path = public;
REVOKE ALL ON FUNCTION column_admin_ok(TEXT) FROM PUBLIC, anon, authenticated;

-- 목록 (본문 포함 — 글 수가 많지 않음)
CREATE OR REPLACE FUNCTION column_list(p_admin_hash TEXT)
RETURNS JSON AS $$
BEGIN
  IF NOT column_admin_ok(p_admin_hash) THEN RAISE EXCEPTION 'unauthorized'; END IF;
  RETURN (
    SELECT COALESCE(json_agg(json_build_object(
      'id', id, 'date', date, 'slug', slug, 'kind', kind, 'title', title, 'summary', summary,
      'body', body, 'opinion', opinion, 'data', data, 'status', status,
      'created_at', created_at, 'updated_at', updated_at, 'published_at', published_at
    ) ORDER BY date DESC, id DESC), '[]'::json)
    FROM (SELECT * FROM column_posts WHERE status <> 'discarded' OR updated_at > now() - interval '30 days'
          ORDER BY date DESC, id DESC LIMIT 300) t
  );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;

-- 저장 / 발행 / 발행 취소 / 버리기
--   p_id NULL → 새 글(직접 쓰기, kind=manual)
--   p_status: draft(임시 저장·발행 취소) | approved(발행 요청) | discarded(버리기)
CREATE OR REPLACE FUNCTION column_save(
  p_admin_hash TEXT, p_id BIGINT, p_title TEXT, p_summary TEXT, p_body TEXT, p_opinion TEXT, p_status TEXT
) RETURNS JSON AS $$
DECLARE
  r column_posts;
  st TEXT := COALESCE(p_status, 'draft');
BEGIN
  IF NOT column_admin_ok(p_admin_hash) THEN RAISE EXCEPTION 'unauthorized'; END IF;
  IF st NOT IN ('draft','approved','discarded') THEN RAISE EXCEPTION 'bad_status'; END IF;

  IF p_id IS NULL THEN
    INSERT INTO column_posts (date, kind, title, summary, body, opinion, status)
    VALUES (to_char(now() AT TIME ZONE 'Asia/Seoul', 'YYYY-MM-DD'), 'manual',
            COALESCE(p_title,''), COALESCE(p_summary,''), COALESCE(p_body,''), COALESCE(p_opinion,''), 'draft')
    RETURNING * INTO r;
    UPDATE column_posts SET slug = r.date || '-' || r.id WHERE id = r.id RETURNING * INTO r;
  ELSE
    SELECT * INTO r FROM column_posts WHERE id = p_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF;
  END IF;

  IF st = 'approved' THEN
    IF length(trim(COALESCE(p_title,''))) < 5 THEN RAISE EXCEPTION 'title_required'; END IF;
    IF length(trim(COALESCE(p_body,''))) < 300 THEN RAISE EXCEPTION 'body_too_short'; END IF;
    IF r.kind = 'auto' AND length(trim(COALESCE(p_opinion,''))) < 100 THEN RAISE EXCEPTION 'opinion_required'; END IF;
  END IF;

  UPDATE column_posts SET
    title = COALESCE(p_title, title), summary = COALESCE(p_summary, summary),
    body = COALESCE(p_body, body), opinion = COALESCE(p_opinion, opinion),
    -- 이미 공개된 글을 고쳐 다시 발행하면 approved → 다음 크롤링이 파일을 다시 만듦
    status = st,
    slug = COALESCE(slug, date || CASE WHEN kind = 'manual' THEN '-' || id ELSE '' END),
    updated_at = now()
  WHERE id = r.id
  RETURNING * INTO r;
  RETURN json_build_object('id', r.id, 'slug', r.slug, 'status', r.status);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;

GRANT EXECUTE ON FUNCTION column_list(TEXT) TO anon, authenticated;
GRANT EXECUTE ON FUNCTION column_save(TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT, TEXT) TO anon, authenticated;

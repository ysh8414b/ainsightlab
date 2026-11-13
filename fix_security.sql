-- ═══════════════════════════════════════════════════════════════
-- 공개 키(anon)로 데이터를 고치거나 지울 수 없게 막기 (2026-11-13)
-- Supabase SQL Editor에 통째로 붙여 실행. 여러 번 실행해도 안전.
--
-- 문제: 여러 setup_*.sql 에 있던
--   CREATE POLICY "Allow service role full access" ... FOR ALL USING (true) WITH CHECK (true);
-- 는 TO 절이 없어 anon(사이트 공개 키)에게도 쓰기·수정·삭제를 허용했음.
-- 크롤러(service_role)는 원래 RLS를 통과하므로 이 정책이 없어도 정상 동작.
--
-- 영향 없음:
--   - 크롤러·급등 AI·내 매매 분석 (service_role 키)
--   - 사이트 읽기 (SELECT 정책은 그대로)
--   - 토론방·관리자 (SECURITY DEFINER 함수로 씀)
--   - 내 매매 mt_* 테이블 (로그인 사용자 본인 행 — 건드리지 않음)
-- ═══════════════════════════════════════════════════════════════


-- ── 1. anon / 모든 사용자(public)에게 쓰기를 허용하는 정책 삭제 ──
--    mt_* 정책은 TO authenticated 라 대상이 아님.
DO $$
DECLARE p RECORD;
BEGIN
  FOR p IN
    SELECT tablename, policyname, cmd, roles
    FROM pg_policies
    WHERE schemaname = 'public'
      AND cmd IN ('ALL', 'INSERT', 'UPDATE', 'DELETE')
      AND (roles && ARRAY['public', 'anon']::name[])
  LOOP
    EXECUTE format('DROP POLICY %I ON public.%I', p.policyname, p.tablename);
    RAISE NOTICE '정책 삭제: %.% (%)', p.tablename, p.policyname, p.cmd;
  END LOOP;
END $$;


-- ── 2. RLS가 꺼진 테이블은 켜고 읽기만 허용 ──
--    (대시보드에서 만든 테이블 중 RLS가 꺼져 있으면 정책과 상관없이 누구나 쓸 수 있음)
--    읽기 정책을 함께 넣어 사이트 화면은 그대로 보이게 함.
--    app_secrets 는 원래 RLS가 켜져 있고 읽기 정책도 없어야 하므로 제외.
DO $$
DECLARE t RECORD;
BEGIN
  FOR t IN
    SELECT c.relname
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r'
      AND NOT c.relrowsecurity
      AND c.relname <> 'app_secrets'
  LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.relname);
    IF NOT EXISTS (SELECT 1 FROM pg_policies
                   WHERE schemaname = 'public' AND tablename = t.relname AND cmd = 'SELECT') THEN
      EXECUTE format('CREATE POLICY "anon read" ON public.%I FOR SELECT USING (true)', t.relname);
    END IF;
    RAISE NOTICE 'RLS 켬 + 읽기 허용: %', t.relname;
  END LOOP;
END $$;


-- ── 3. 테이블 권한에서도 쓰기 회수 (이중 잠금) ──
--    mt_* 는 로그인 사용자가 써야 하므로 제외 (setup_mytrades*.sql 이 칸 단위로 이미 정리함).
DO $$
DECLARE t RECORD;
BEGIN
  FOR t IN
    SELECT c.relname
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'p')
      AND c.relname NOT LIKE 'mt\_%'
  LOOP
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON public.%I FROM anon, authenticated', t.relname);
  END LOOP;
END $$;

-- 앞으로 새로 만드는 테이블도 기본으로 anon·authenticated 쓰기 권한 없음
-- (필요한 테이블은 그 setup SQL에서 GRANT로 따로 열어 줌 — mt_* 처럼)
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLES FROM anon, authenticated;


-- ── 4. 확인 ──
-- (1) 남은 쓰기 정책 — mt_* (authenticated) 만 나와야 정상
SELECT tablename, policyname, cmd, roles
FROM pg_policies
WHERE schemaname = 'public' AND cmd <> 'SELECT'
ORDER BY tablename;

-- (2) 관리자 기본 비밀번호('admin')가 아직 남아 있는지 — 1 이상이면 admin.html에서 즉시 변경!
SELECT count(*) AS "기본 비밀번호 admin 남은 계정 수"
FROM board_admins
WHERE password_hash = '8c6976e5b5410415bde908bd4dee15dfb167a9c873fc4bb8a81f6f2ab448a918';

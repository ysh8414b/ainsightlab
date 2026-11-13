-- 자유게시판 완전 삭제 (2026-11-13)
-- Supabase SQL Editor에서 1회 실행. 게시글·댓글이 영구 삭제되며 되돌릴 수 없음.
-- 관리자 계정(board_admins, admin_login 등)·닉네임 차단(banned_nicknames)·채팅 함수는
-- admin.html·chat.html·내 매매가 계속 쓰므로 남겨 둠.

DROP VIEW IF EXISTS board_comments_public;
DROP VIEW IF EXISTS board_posts_public;

DROP FUNCTION IF EXISTS insert_board_post(TEXT, TEXT, TEXT, TEXT);
DROP FUNCTION IF EXISTS insert_board_comment(BIGINT, TEXT, TEXT, TEXT);
DROP FUNCTION IF EXISTS delete_board_post(BIGINT, TEXT);
DROP FUNCTION IF EXISTS delete_board_comment(BIGINT, TEXT);
DROP FUNCTION IF EXISTS update_board_post(BIGINT, TEXT, TEXT, TEXT);
DROP FUNCTION IF EXISTS update_board_comment(BIGINT, TEXT, TEXT);
DROP FUNCTION IF EXISTS toggle_pin_post(BIGINT, TEXT);
DROP FUNCTION IF EXISTS increment_view_count(BIGINT);

DROP TABLE IF EXISTS board_comments;   -- 트리거 trg_comment_count 도 함께 삭제됨
DROP TABLE IF EXISTS board_posts;
DROP FUNCTION IF EXISTS update_comment_count();

-- 口语跟读：数据库结构（PostgreSQL）
-- 按当前线上结构整理。适用于 WorkBuddy 云服务（auth.uid() 返回 text）。
-- 迁到 Supabase 时 auth.uid() 返回 uuid：把各表 owner_id 的默认值改成 auth.uid()::text，
-- 并把策略里的 auth.uid() 改成 auth.uid()::text。
-- 所有用户数据都按 owner_id 隔离：每人只能读写自己的数据；子表写入时还会校验父级归属。

-- ============ 表 ============
CREATE TABLE IF NOT EXISTS topics (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_id    TEXT NOT NULL DEFAULT auth.uid(),
  owner_name  TEXT,
  title       TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 100),
  description TEXT CHECK (description IS NULL OR char_length(description) <= 500),
  position    INT NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 导入的 B 站视频 / 本地音视频
CREATE TABLE IF NOT EXISTS media_sources (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_id       TEXT NOT NULL DEFAULT auth.uid(),
  topic_id       BIGINT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  kind           TEXT NOT NULL CHECK (kind IN ('bili', 'file')),
  title          TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 300),
  ref            JSONB CHECK (ref IS NULL OR pg_column_size(ref) <= 2000),
  range_start_ms INT NOT NULL DEFAULT 0,
  duration_ms    INT,
  status         TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'transcribing', 'organizing', 'done', 'failed', 'stopped')),
  chunks_total   INT,
  chunks_done    INT NOT NULL DEFAULT 0,
  speakers       JSONB,
  options        JSONB CHECK (options IS NULL OR pg_column_size(options) <= 2000),
  error          TEXT CHECK (error IS NULL OR char_length(error) <= 500),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dialogues (
  id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_id   TEXT NOT NULL DEFAULT auth.uid(),
  topic_id   BIGINT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  title      TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 100),
  scene      TEXT CHECK (scene IS NULL OR char_length(scene) <= 1000),
  role_a     TEXT CHECK (role_a IS NULL OR char_length(role_a) <= 40),
  role_b     TEXT CHECK (role_b IS NULL OR char_length(role_b) <= 40),
  notes      TEXT CHECK (notes IS NULL OR char_length(notes) <= 4000),          -- 重点表达
  source_id  BIGINT REFERENCES media_sources(id) ON DELETE SET NULL,            -- 来自哪次导入
  position   INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lines (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_id     TEXT NOT NULL DEFAULT auth.uid(),
  dialogue_id  BIGINT NOT NULL REFERENCES dialogues(id) ON DELETE CASCADE,
  speaker      TEXT NOT NULL DEFAULT 'A' CHECK (speaker IN ('A', 'B')),
  speaker_name TEXT CHECK (speaker_name IS NULL OR char_length(speaker_name) <= 60),  -- 导入内容里的真实说话人
  text         TEXT NOT NULL CHECK (char_length(text) BETWEEN 1 AND 1000),
  translation  TEXT CHECK (translation IS NULL OR char_length(translation) <= 1000),
  note         TEXT CHECK (note IS NULL OR char_length(note) <= 2000),               -- AI 点评
  vocab        JSONB CHECK (vocab IS NULL OR pg_column_size(vocab) <= 16000),        -- 难词与用法
  src          JSONB CHECK (src IS NULL OR pg_column_size(src) <= 1000),             -- 原声片段 {path, s, e}
  position     INT NOT NULL DEFAULT 0,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS recordings (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_id    TEXT NOT NULL DEFAULT auth.uid(),
  dialogue_id BIGINT NOT NULL REFERENCES dialogues(id) ON DELETE CASCADE,
  line_id     BIGINT NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
  path        TEXT NOT NULL CHECK (char_length(path) <= 300),                  -- 云存储 users/<uid>/rec/...
  mime        TEXT CHECK (mime IS NULL OR char_length(mime) <= 60),
  size_bytes  INT,
  duration_ms INT,
  source      TEXT CHECK (source IS NULL OR source IN ('manual', 'shadow')),
  line_text   TEXT CHECK (line_text IS NULL OR char_length(line_text) <= 1000),
  analysis    JSONB CHECK (analysis IS NULL OR pg_column_size(analysis) <= 20000), -- 发音分析
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vocab_words (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_id    TEXT NOT NULL DEFAULT auth.uid(),
  dialogue_id BIGINT NOT NULL REFERENCES dialogues(id) ON DELETE CASCADE,
  line_id     BIGINT REFERENCES lines(id) ON DELETE SET NULL,
  word        TEXT NOT NULL CHECK (char_length(word) BETWEEN 1 AND 80),
  phonetic    TEXT CHECK (phonetic IS NULL OR char_length(phonetic) <= 80),
  pos         TEXT CHECK (pos IS NULL OR char_length(pos) <= 30),
  meaning     TEXT CHECK (meaning IS NULL OR char_length(meaning) <= 300),
  note        TEXT CHECK (note IS NULL OR char_length(note) <= 300),
  example     TEXT CHECK (example IS NULL OR char_length(example) <= 1000),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS media_chunks (
  id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_id   TEXT NOT NULL DEFAULT auth.uid(),
  source_id  BIGINT NOT NULL REFERENCES media_sources(id) ON DELETE CASCADE,
  idx        INT NOT NULL,
  start_ms   INT NOT NULL,
  end_ms     INT NOT NULL,
  path       TEXT CHECK (path IS NULL OR char_length(path) <= 300),
  transcript JSONB CHECK (transcript IS NULL OR pg_column_size(transcript) <= 60000),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_id, idx)
);

CREATE TABLE IF NOT EXISTS user_prefs (
  owner_id   TEXT PRIMARY KEY DEFAULT auth.uid(),
  prefs      JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (pg_column_size(prefs) <= 4000),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- AI 调用日志：客户端只能写入自己的记录、不能读；管理员通过 ai_usage_stats() 看汇总
CREATE TABLE IF NOT EXISTS ai_calls (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_id          TEXT NOT NULL DEFAULT auth.uid(),
  owner_name        TEXT CHECK (owner_name IS NULL OR char_length(owner_name) <= 60),
  kind              TEXT NOT NULL CHECK (char_length(kind) <= 40),
  model             TEXT CHECK (model IS NULL OR char_length(model) <= 100),
  ok                BOOLEAN NOT NULL DEFAULT true,
  error_code        TEXT CHECK (error_code IS NULL OR char_length(error_code) <= 100),
  prompt_tokens     INT,
  completion_tokens INT,
  total_tokens      INT,
  credit            NUMERIC,
  duration_ms       INT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 管理员与一次性认领码：客户端不可直接访问（启用 RLS 且不建策略）
CREATE TABLE IF NOT EXISTS app_admins (
  user_id    TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS admin_claim_codes (
  code    TEXT PRIMARY KEY,
  used_by TEXT,
  used_at TIMESTAMPTZ
);

-- 应用配置（如全员默认模型）：所有人可读，仅管理员可写
CREATE TABLE IF NOT EXISTS app_config (
  key        TEXT PRIMARY KEY CHECK (char_length(key) <= 60),
  value      TEXT CHECK (value IS NULL OR char_length(value) <= 500),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ 索引 ============
CREATE INDEX IF NOT EXISTS dialogues_topic_idx     ON dialogues(topic_id);
CREATE INDEX IF NOT EXISTS lines_dialogue_idx      ON lines(dialogue_id);
CREATE INDEX IF NOT EXISTS recordings_line_idx     ON recordings(line_id);
CREATE INDEX IF NOT EXISTS recordings_dialogue_idx ON recordings(dialogue_id);
CREATE INDEX IF NOT EXISTS media_sources_topic_idx ON media_sources(topic_id);
CREATE INDEX IF NOT EXISTS ai_calls_created_idx    ON ai_calls(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS vocab_words_uniq ON vocab_words(dialogue_id, lower(word));

-- ============ 函数（SECURITY DEFINER：内部先校验身份） ============
CREATE OR REPLACE FUNCTION is_app_admin() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
AS $$ SELECT auth.uid() IS NOT NULL AND EXISTS (SELECT 1 FROM app_admins WHERE user_id = auth.uid()) $$;

CREATE OR REPLACE FUNCTION claim_admin(p_code text) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public
AS $$
DECLARE uid text := auth.uid();
BEGIN
  IF uid IS NULL THEN RAISE EXCEPTION 'not_signed_in' USING ERRCODE = '42501'; END IF;
  IF p_code IS NULL OR char_length(p_code) > 100 THEN RETURN false; END IF;
  UPDATE admin_claim_codes SET used_by = uid, used_at = now() WHERE code = p_code AND used_by IS NULL;
  IF NOT FOUND THEN RETURN false; END IF;
  INSERT INTO app_admins(user_id) VALUES (uid) ON CONFLICT DO NOTHING;
  RETURN true;
END $$;

CREATE OR REPLACE FUNCTION ai_usage_stats() RETURNS json
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public
AS $$
DECLARE d0 timestamptz := date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai';
BEGIN
  IF NOT is_app_admin() THEN RAISE EXCEPTION 'not_admin' USING ERRCODE = '42501'; END IF;
  RETURN json_build_object(
    'total',   (SELECT count(*) FROM ai_calls),
    'failed',  (SELECT count(*) FROM ai_calls WHERE NOT ok),
    'today',   (SELECT count(*) FROM ai_calls WHERE created_at >= d0),
    'last7d',  (SELECT count(*) FROM ai_calls WHERE created_at >= d0 - interval '6 days'),
    'users',   (SELECT count(DISTINCT owner_id) FROM ai_calls),
    'tokens',  (SELECT coalesce(sum(total_tokens), 0) FROM ai_calls),
    'credit',  (SELECT coalesce(sum(credit), 0) FROM ai_calls),
    'by_kind', (SELECT coalesce(json_agg(x ORDER BY x.n DESC), '[]'::json) FROM
                 (SELECT kind, count(*) AS n, coalesce(sum(total_tokens), 0) AS tokens FROM ai_calls GROUP BY kind) x),
    'by_model',(SELECT coalesce(json_agg(x ORDER BY x.n DESC), '[]'::json) FROM
                 (SELECT coalesce(model, '?') AS model, count(*) AS n, count(*) FILTER (WHERE NOT ok) AS failed,
                         coalesce(sum(total_tokens), 0) AS tokens FROM ai_calls GROUP BY model) x),
    'by_user', (SELECT coalesce(json_agg(x ORDER BY x.n DESC), '[]'::json) FROM
                 (SELECT max(owner_name) AS name, right(owner_id, 4) AS uid, count(*) AS n,
                         coalesce(sum(total_tokens), 0) AS tokens, max(created_at) AS last_at
                  FROM ai_calls GROUP BY owner_id ORDER BY count(*) DESC LIMIT 20) x),
    'daily',   (SELECT coalesce(json_agg(x ORDER BY x.day), '[]'::json) FROM
                 (SELECT to_char(created_at AT TIME ZONE 'Asia/Shanghai', 'MM-DD') AS day, count(*) AS n
                  FROM ai_calls WHERE created_at >= d0 - interval '13 days' GROUP BY 1) x)
  );
END $$;

-- ============ 行级安全 ============
ALTER TABLE topics            ENABLE ROW LEVEL SECURITY;
ALTER TABLE media_sources     ENABLE ROW LEVEL SECURITY;
ALTER TABLE dialogues         ENABLE ROW LEVEL SECURITY;
ALTER TABLE lines             ENABLE ROW LEVEL SECURITY;
ALTER TABLE recordings        ENABLE ROW LEVEL SECURITY;
ALTER TABLE vocab_words       ENABLE ROW LEVEL SECURITY;
ALTER TABLE media_chunks      ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_prefs        ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_calls          ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_admins        ENABLE ROW LEVEL SECURITY;
ALTER TABLE admin_claim_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_config        ENABLE ROW LEVEL SECURITY;

GRANT SELECT, INSERT, UPDATE, DELETE ON topics, media_sources, dialogues, lines, recordings, vocab_words, media_chunks
  TO authenticated, anon;
GRANT SELECT, INSERT, UPDATE ON user_prefs, app_config TO authenticated, anon;
GRANT INSERT ON ai_calls TO authenticated, anon;

-- 顶层表：只看自己的
CREATE POLICY topics_select_own ON topics FOR SELECT TO authenticated, anon USING (owner_id = auth.uid());
CREATE POLICY topics_insert_own ON topics FOR INSERT TO authenticated, anon WITH CHECK (owner_id = auth.uid());
CREATE POLICY topics_update_own ON topics FOR UPDATE TO authenticated, anon USING (owner_id = auth.uid()) WITH CHECK (owner_id = auth.uid());
CREATE POLICY topics_delete_own ON topics FOR DELETE TO authenticated, anon USING (owner_id = auth.uid());

CREATE POLICY prefs_select_own ON user_prefs FOR SELECT TO authenticated, anon USING (owner_id = auth.uid());
CREATE POLICY prefs_insert_own ON user_prefs FOR INSERT TO authenticated, anon WITH CHECK (owner_id = auth.uid());
CREATE POLICY prefs_update_own ON user_prefs FOR UPDATE TO authenticated, anon USING (owner_id = auth.uid()) WITH CHECK (owner_id = auth.uid());

CREATE POLICY ai_calls_insert_own ON ai_calls FOR INSERT TO authenticated, anon WITH CHECK (owner_id = auth.uid());

CREATE POLICY app_config_read_all     ON app_config FOR SELECT TO authenticated, anon USING (true);
CREATE POLICY app_config_insert_admin ON app_config FOR INSERT TO authenticated, anon WITH CHECK (is_app_admin());
CREATE POLICY app_config_update_admin ON app_config FOR UPDATE TO authenticated, anon USING (is_app_admin()) WITH CHECK (is_app_admin());

-- 子表：只看自己的；写入时父级也必须是自己的
CREATE POLICY ms_select_own ON media_sources FOR SELECT TO authenticated, anon USING (owner_id = auth.uid());
CREATE POLICY ms_insert_own ON media_sources FOR INSERT TO authenticated, anon
  WITH CHECK (owner_id = auth.uid() AND EXISTS (SELECT 1 FROM topics t WHERE t.id = topic_id AND t.owner_id = auth.uid()));
CREATE POLICY ms_update_own ON media_sources FOR UPDATE TO authenticated, anon USING (owner_id = auth.uid())
  WITH CHECK (owner_id = auth.uid() AND EXISTS (SELECT 1 FROM topics t WHERE t.id = topic_id AND t.owner_id = auth.uid()));
CREATE POLICY ms_delete_own ON media_sources FOR DELETE TO authenticated, anon USING (owner_id = auth.uid());

CREATE POLICY dialogues_select_own ON dialogues FOR SELECT TO authenticated, anon USING (owner_id = auth.uid());
CREATE POLICY dialogues_insert_own ON dialogues FOR INSERT TO authenticated, anon
  WITH CHECK (owner_id = auth.uid() AND EXISTS (SELECT 1 FROM topics t WHERE t.id = topic_id AND t.owner_id = auth.uid()));
CREATE POLICY dialogues_update_own ON dialogues FOR UPDATE TO authenticated, anon USING (owner_id = auth.uid())
  WITH CHECK (owner_id = auth.uid() AND EXISTS (SELECT 1 FROM topics t WHERE t.id = topic_id AND t.owner_id = auth.uid()));
CREATE POLICY dialogues_delete_own ON dialogues FOR DELETE TO authenticated, anon USING (owner_id = auth.uid());

CREATE POLICY lines_select_own ON lines FOR SELECT TO authenticated, anon USING (owner_id = auth.uid());
CREATE POLICY lines_insert_own ON lines FOR INSERT TO authenticated, anon
  WITH CHECK (owner_id = auth.uid() AND EXISTS (SELECT 1 FROM dialogues d WHERE d.id = dialogue_id AND d.owner_id = auth.uid()));
CREATE POLICY lines_update_own ON lines FOR UPDATE TO authenticated, anon USING (owner_id = auth.uid())
  WITH CHECK (owner_id = auth.uid() AND EXISTS (SELECT 1 FROM dialogues d WHERE d.id = dialogue_id AND d.owner_id = auth.uid()));
CREATE POLICY lines_delete_own ON lines FOR DELETE TO authenticated, anon USING (owner_id = auth.uid());

CREATE POLICY rec_select_own ON recordings FOR SELECT TO authenticated, anon USING (owner_id = auth.uid());
CREATE POLICY rec_insert_own ON recordings FOR INSERT TO authenticated, anon
  WITH CHECK (owner_id = auth.uid() AND EXISTS (SELECT 1 FROM lines l WHERE l.id = line_id AND l.dialogue_id = recordings.dialogue_id AND l.owner_id = auth.uid()));
CREATE POLICY rec_update_own ON recordings FOR UPDATE TO authenticated, anon USING (owner_id = auth.uid())
  WITH CHECK (owner_id = auth.uid() AND EXISTS (SELECT 1 FROM lines l WHERE l.id = line_id AND l.dialogue_id = recordings.dialogue_id AND l.owner_id = auth.uid()));
CREATE POLICY rec_delete_own ON recordings FOR DELETE TO authenticated, anon USING (owner_id = auth.uid());

CREATE POLICY vocab_select_own ON vocab_words FOR SELECT TO authenticated, anon USING (owner_id = auth.uid());
CREATE POLICY vocab_insert_own ON vocab_words FOR INSERT TO authenticated, anon
  WITH CHECK (owner_id = auth.uid() AND EXISTS (SELECT 1 FROM dialogues d WHERE d.id = dialogue_id AND d.owner_id = auth.uid()));
CREATE POLICY vocab_update_own ON vocab_words FOR UPDATE TO authenticated, anon USING (owner_id = auth.uid())
  WITH CHECK (owner_id = auth.uid() AND EXISTS (SELECT 1 FROM dialogues d WHERE d.id = dialogue_id AND d.owner_id = auth.uid()));
CREATE POLICY vocab_delete_own ON vocab_words FOR DELETE TO authenticated, anon USING (owner_id = auth.uid());

CREATE POLICY mc_select_own ON media_chunks FOR SELECT TO authenticated, anon USING (owner_id = auth.uid());
CREATE POLICY mc_insert_own ON media_chunks FOR INSERT TO authenticated, anon
  WITH CHECK (owner_id = auth.uid() AND EXISTS (SELECT 1 FROM media_sources m WHERE m.id = source_id AND m.owner_id = auth.uid()));
CREATE POLICY mc_update_own ON media_chunks FOR UPDATE TO authenticated, anon USING (owner_id = auth.uid())
  WITH CHECK (owner_id = auth.uid() AND EXISTS (SELECT 1 FROM media_sources m WHERE m.id = source_id AND m.owner_id = auth.uid()));
CREATE POLICY mc_delete_own ON media_chunks FOR DELETE TO authenticated, anon USING (owner_id = auth.uid());

-- ============ 部署后：生成自己的管理员认领码（只能用一次） ============
-- INSERT INTO admin_claim_codes(code) VALUES ('换成你自己的随机字符串');

CREATE TABLE IF NOT EXISTS sources(id SERIAL PRIMARY KEY,name TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,url TEXT NOT NULL,active BOOLEAN NOT NULL DEFAULT FALSE);
CREATE TABLE IF NOT EXISTS participants(telegram_id BIGINT PRIMARY KEY,role TEXT NOT NULL CHECK(role IN ('owner','member')),active BOOLEAN NOT NULL DEFAULT TRUE,miniapp_enabled BOOLEAN NOT NULL DEFAULT FALSE,created_at TIMESTAMPTZ NOT NULL DEFAULT now());
ALTER TABLE sources ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}';
ALTER TABLE sources ADD COLUMN IF NOT EXISTS interval_hours INTEGER NOT NULL DEFAULT 24;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS deferred_at TIMESTAMPTZ;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS deferred_code TEXT;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS deferred_reason TEXT;
CREATE TABLE IF NOT EXISTS runs(id SERIAL PRIMARY KEY,service TEXT NOT NULL,status TEXT NOT NULL,started_at TIMESTAMPTZ NOT NULL DEFAULT now(),finished_at TIMESTAMPTZ,items INTEGER NOT NULL DEFAULT 0,note TEXT);
CREATE TABLE IF NOT EXISTS materials(id SERIAL PRIMARY KEY,source_id INTEGER REFERENCES sources(id),run_id INTEGER REFERENCES runs(id),url TEXT UNIQUE NOT NULL,title TEXT NOT NULL,text TEXT,published_at TIMESTAMPTZ,collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),status TEXT NOT NULL DEFAULT 'new');
ALTER TABLE materials ADD COLUMN IF NOT EXISTS doi TEXT;
ALTER TABLE materials ADD COLUMN IF NOT EXISTS pmid TEXT;
ALTER TABLE materials ADD COLUMN IF NOT EXISTS authors TEXT;
ALTER TABLE materials ADD COLUMN IF NOT EXISTS text_scope TEXT NOT NULL DEFAULT 'excerpt';
ALTER TABLE materials ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE materials ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE materials ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE materials ADD COLUMN IF NOT EXISTS last_error TEXT;
CREATE TABLE IF NOT EXISTS saved_materials(owner_telegram_id BIGINT NOT NULL REFERENCES participants(telegram_id),material_id INTEGER NOT NULL REFERENCES materials(id),saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),PRIMARY KEY(owner_telegram_id,material_id));
CREATE UNIQUE INDEX IF NOT EXISTS materials_doi ON materials(doi) WHERE doi IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS materials_pmid ON materials(pmid) WHERE pmid IS NOT NULL;
CREATE TABLE IF NOT EXISTS material_sources(material_id INTEGER REFERENCES materials(id),source_id INTEGER REFERENCES sources(id),url TEXT NOT NULL,PRIMARY KEY(material_id,source_id,url));
CREATE TABLE IF NOT EXISTS findings(id SERIAL PRIMARY KEY,material_id INTEGER UNIQUE NOT NULL REFERENCES materials(id),entity TEXT,summary TEXT NOT NULL,model TEXT NOT NULL,prompt_version TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now());
ALTER TABLE findings ADD COLUMN IF NOT EXISTS topics JSONB NOT NULL DEFAULT '[]';
ALTER TABLE findings ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE findings ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}';
ALTER TABLE findings ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS facts(id SERIAL PRIMARY KEY,material_id INTEGER NOT NULL REFERENCES materials(id),statement TEXT NOT NULL,quote TEXT NOT NULL,payload JSONB NOT NULL DEFAULT '{}',created_at TIMESTAMPTZ NOT NULL DEFAULT now(),UNIQUE(material_id,statement,quote));
ALTER TABLE facts ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
CREATE TABLE IF NOT EXISTS conclusions(id SERIAL PRIMARY KEY,topic TEXT NOT NULL,version INTEGER NOT NULL,content JSONB NOT NULL,reason TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),UNIQUE(topic,version));
CREATE TABLE IF NOT EXISTS answers(id SERIAL PRIMARY KEY,question TEXT NOT NULL,content JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now());
ALTER TABLE answers ADD COLUMN IF NOT EXISTS owner_telegram_id BIGINT;
CREATE INDEX IF NOT EXISTS answers_owner_created ON answers(owner_telegram_id,created_at DESC);
CREATE TABLE IF NOT EXISTS corrections(id SERIAL PRIMARY KEY,material_id INTEGER REFERENCES materials(id),before_value JSONB NOT NULL,after_value JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS digests(id SERIAL PRIMARY KEY,day DATE UNIQUE NOT NULL,status TEXT NOT NULL DEFAULT 'ready',content JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS digest_items(digest_id INTEGER REFERENCES digests(id),material_id INTEGER REFERENCES materials(id),PRIMARY KEY(digest_id,material_id));
CREATE TABLE IF NOT EXISTS digest_deliveries(digest_id INTEGER REFERENCES digests(id),chat_id BIGINT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',sent_at TIMESTAMPTZ,error TEXT,PRIMARY KEY(digest_id,chat_id));
ALTER TABLE digest_deliveries ADD COLUMN IF NOT EXISTS next_part INTEGER NOT NULL DEFAULT 0;
ALTER TABLE digest_deliveries ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1;
CREATE TABLE IF NOT EXISTS telegram_updates(update_id BIGINT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'pending',response JSONB NOT NULL DEFAULT '[]',chat_id BIGINT,next_part INTEGER NOT NULL DEFAULT 0);
ALTER TABLE telegram_updates ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE telegram_updates ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS telegram_state(key TEXT PRIMARY KEY,value BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS model_calls(id SERIAL PRIMARY KEY,stage TEXT NOT NULL,model TEXT NOT NULL,reserved_usd NUMERIC NOT NULL,actual_usd NUMERIC,created_at TIMESTAMPTZ NOT NULL DEFAULT now());
ALTER TABLE materials ADD COLUMN IF NOT EXISTS source_role TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE material_sources ADD COLUMN IF NOT EXISTS source_role TEXT NOT NULL DEFAULT 'unknown';
-- Admission is separate from processing status, quotations and private questions.
CREATE OR REPLACE FUNCTION reader_text_version(TEXT,TEXT,TEXT,TEXT) RETURNS TEXT
LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
 SELECT md5(length(coalesce($1,''))::text || ':' || coalesce($1,'') ||
            length(coalesce($2,''))::text || ':' || coalesce($2,'') ||
            length(coalesce($3,'excerpt'))::text || ':' || coalesce($3,'excerpt') ||
            length(coalesce($4,'unknown'))::text || ':' || coalesce($4,'unknown'))
$$;
CREATE TABLE IF NOT EXISTS material_assessments(
 id BIGSERIAL PRIMARY KEY,material_id INTEGER NOT NULL REFERENCES materials(id),
 profile_version TEXT NOT NULL,text_version TEXT NOT NULL,scope_key TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('eligible','excluded','needs_review')),code TEXT NOT NULL,
 details JSONB NOT NULL,reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(material_id,profile_version,text_version,scope_key));
CREATE INDEX IF NOT EXISTS material_assessments_reader ON material_assessments(profile_version,scope_key,status,material_id);
-- Russian search must work even on clusters initialized with the C locale.
CREATE OR REPLACE FUNCTION reader_casefold(TEXT) RETURNS TEXT
LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
 SELECT translate(lower(coalesce($1,'')),
   'АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ',
   'абвгдеёжзийклмнопрстуфхцчшщъыьэюя')
$$;
CREATE OR REPLACE FUNCTION reader_fact_version(INTEGER) RETURNS TEXT
LANGUAGE SQL STABLE AS $$
 SELECT md5(jsonb_build_object('facts',
   coalesce((SELECT jsonb_agg(jsonb_build_array(x.id,x.statement,x.quote,x.payload) ORDER BY x.id)
             FROM facts x WHERE x.material_id=m.id AND x.active),'[]'::jsonb),
   'kind',f.kind,'verified',f.verified,'url',m.url,'published',extract(epoch FROM m.published_at))::text)
 FROM materials m JOIN findings f ON f.material_id=m.id WHERE m.id=$1
$$;
CREATE TABLE IF NOT EXISTS reader_findings(
 id BIGSERIAL PRIMARY KEY,material_id INTEGER NOT NULL REFERENCES materials(id),
 profile_version TEXT NOT NULL,text_version TEXT NOT NULL,input_signature TEXT NOT NULL,
 editorial_version INTEGER NOT NULL,status TEXT NOT NULL
   CHECK(status IN ('eligible','excluded','needs_review')),
 content JSONB NOT NULL DEFAULT '{}',fact_ids INTEGER[] NOT NULL DEFAULT '{}',
 error JSONB,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(material_id,profile_version,text_version,input_signature,editorial_version));
CREATE INDEX IF NOT EXISTS reader_findings_current ON reader_findings(profile_version,editorial_version,status,material_id);
CREATE TABLE IF NOT EXISTS topic_refresh(
 topic TEXT PRIMARY KEY,revision BIGINT NOT NULL DEFAULT 1,done_revision BIGINT NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','ready','failed')),
 success_signature TEXT,attempt_signature TEXT,attempt_revision BIGINT NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0,next_retry_at TIMESTAMPTZ,last_error JSONB,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS topic_snapshots(
 topic TEXT NOT NULL REFERENCES topic_refresh(topic),input_signature TEXT NOT NULL,
 profile_version TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
 next_retry_at TIMESTAMPTZ,last_error JSONB,successful BOOLEAN NOT NULL DEFAULT FALSE,
 PRIMARY KEY(topic,input_signature));
ALTER TABLE conclusions ADD COLUMN IF NOT EXISTS input_signature TEXT;
ALTER TABLE conclusions ADD COLUMN IF NOT EXISTS profile_version TEXT;
ALTER TABLE conclusions ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT FALSE;
-- Exa reservation commits on a separate connection: no FK to uncommitted runs.
CREATE TABLE IF NOT EXISTS search_calls(
 id BIGSERIAL PRIMARY KEY,scope_id TEXT NOT NULL,purpose TEXT NOT NULL CHECK(purpose IN ('question','daily')),
 operation TEXT NOT NULL CHECK(operation IN ('search','contents')),run_id INTEGER,
 reserved_usd NUMERIC NOT NULL CHECK(reserved_usd>0),actual_usd NUMERIC CHECK(actual_usd>=0),
 status TEXT NOT NULL DEFAULT 'reserved' CHECK(status IN ('reserved','completed','failed','unknown')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),finished_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS search_calls_month ON search_calls(created_at);
CREATE INDEX IF NOT EXISTS search_calls_scope ON search_calls(scope_id);
CREATE TABLE IF NOT EXISTS source_checks(
 id BIGSERIAL PRIMARY KEY,run_id INTEGER REFERENCES runs(id),source_id INTEGER REFERENCES sources(id),
 purpose TEXT NOT NULL,scope_id TEXT,status TEXT NOT NULL,stage TEXT NOT NULL,reason TEXT,
 details JSONB NOT NULL DEFAULT '{}',started_at TIMESTAMPTZ NOT NULL DEFAULT now(),finished_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS source_checks_created ON source_checks(finished_at);
ALTER TABLE source_checks ADD COLUMN IF NOT EXISTS planned BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE source_checks ADD COLUMN IF NOT EXISTS complete BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE source_checks ADD COLUMN IF NOT EXISTS code TEXT;
-- Separate committed claim precedes any daily HTTP. No FK to outer collector runs.
CREATE TABLE IF NOT EXISTS daily_search_runs(
 day DATE PRIMARY KEY,timezone TEXT NOT NULL,scope_id TEXT UNIQUE NOT NULL,
 topic_index INTEGER NOT NULL CHECK(topic_index BETWEEN 0 AND 2),
 status TEXT NOT NULL CHECK(status IN ('claimed','completed','partial','failed')),
 code TEXT,reason TEXT,run_id INTEGER,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),finished_at TIMESTAMPTZ);
-- Preserve original AI data, but never collect it for the new product.
UPDATE sources SET active=FALSE WHERE kind IN ('hn','arxiv') OR name IN ('Хабр: ИИ','Sostav','New Retail');
-- Explicit approved replacement, including databases with old active defaults.
-- Preserve every previous document and all administrator configuration fields.
UPDATE sources SET active=FALSE WHERE kind IN ('europe_pmc','pubmed');

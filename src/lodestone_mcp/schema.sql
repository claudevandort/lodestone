CREATE TABLE IF NOT EXISTS memories (
  id            INTEGER PRIMARY KEY,
  uuid          TEXT NOT NULL UNIQUE,
  project_id    TEXT NOT NULL,
  project_label TEXT,
  author_id     TEXT NOT NULL DEFAULT 'claude',
  kind          TEXT NOT NULL CHECK (kind IN (
                  'attempt','decision','gotcha','preference','fact','question'
                )),
  title         TEXT NOT NULL,
  content       TEXT NOT NULL,
  outcome       TEXT CHECK (outcome IN ('worked','failed','partial','unknown')),
  confidence    REAL NOT NULL DEFAULT 0.7 CHECK (confidence BETWEEN 0 AND 1),
  context       TEXT,
  created_at    INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL,
  verified_at   INTEGER,
  superseded_by INTEGER REFERENCES memories(id),
  deleted_at    INTEGER,
  access_count  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_mem_project_kind
  ON memories(project_id, kind) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_mem_recent
  ON memories(project_id, created_at DESC) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS tags (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS memory_tags (
  memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  tag_id    INTEGER NOT NULL REFERENCES tags(id),
  PRIMARY KEY (memory_id, tag_id)
);

CREATE TABLE IF NOT EXISTS memory_links (
  from_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  to_id   INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  kind    TEXT NOT NULL CHECK (kind IN (
            'supersedes','related','contradicts','caused_by'
          )),
  PRIMARY KEY (from_id, to_id, kind)
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
  memory_id INTEGER PRIMARY KEY,
  embedding FLOAT[1024]
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  title, content, tags,
  tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memory_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, '');
END;

CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE ON memories BEGIN
  UPDATE memory_fts SET title = new.title, content = new.content
    WHERE rowid = new.id;
END;

CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
  DELETE FROM memory_fts WHERE rowid = old.id;
END;

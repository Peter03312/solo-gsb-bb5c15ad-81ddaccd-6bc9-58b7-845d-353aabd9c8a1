-- 线下越野赛计时核算：初始化脚本（幂等，可重复执行）
-- 由应用启动时自动执行；参数 :race_id 为默认赛事 ID（默认 1）。

CREATE TABLE IF NOT EXISTS races (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO races (id, name)
VALUES (%(race_id)s, '默认线下越野赛')
ON CONFLICT (id) DO NOTHING;

-- 检查点：position 即过点顺序（1 = 起点），赛事内唯一
CREATE TABLE IF NOT EXISTS checkpoints (
    race_id     INTEGER NOT NULL REFERENCES races(id),
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    position    INTEGER NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (race_id, code),
    UNIQUE (race_id, position)
);

CREATE TABLE IF NOT EXISTS participants (
    race_id     INTEGER NOT NULL REFERENCES races(id),
    id          TEXT NOT NULL,
    bib         TEXT NOT NULL,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (race_id, id),
    UNIQUE (race_id, bib)
);

-- 扫描报文：message_id 全局唯一（离线采集端生成的 UUID），重传幂等
CREATE TABLE IF NOT EXISTS scans (
    message_id        UUID PRIMARY KEY,
    race_id           INTEGER NOT NULL REFERENCES races(id),
    participant_id    TEXT NOT NULL,
    checkpoint_code   TEXT NOT NULL,
    event_time        TIMESTAMPTZ NOT NULL,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked           BOOLEAN NOT NULL DEFAULT FALSE,
    revoked_at        TIMESTAMPTZ,
    -- 即使报文被撤销也保留行（审计 + 重传识别），故 FK 不级联
    FOREIGN KEY (race_id, participant_id)
        REFERENCES participants(race_id, id),
    FOREIGN KEY (race_id, checkpoint_code)
        REFERENCES checkpoints(race_id, code)
);

CREATE INDEX IF NOT EXISTS idx_scans_lookup
    ON scans (race_id, participant_id, checkpoint_code, event_time);

-- 已发布版本：version 在赛事内连续递增（靠发布事务串行化保证无空洞）
CREATE TABLE IF NOT EXISTS result_versions (
    id                BIGINT GENERATED ALWAYS AS IDENTITY,
    race_id           INTEGER NOT NULL REFERENCES races(id),
    version           INTEGER NOT NULL,
    published_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    checkpoints_json  JSONB NOT NULL,
    result_json       JSONB NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (race_id, version)
);

CREATE TABLE IF NOT EXISTS result_rows (
    version_id        BIGINT NOT NULL REFERENCES result_versions(id) ON DELETE CASCADE,
    race_id           INTEGER NOT NULL REFERENCES races(id),
    participant_id    TEXT NOT NULL,
    rank              INTEGER,                       -- NULL 表示未完赛
    elapsed_seconds   NUMERIC(12, 3),
    start_time        TIMESTAMPTZ,
    finish_time       TIMESTAMPTZ,
    passes_json       JSONB NOT NULL,
    reason            TEXT,
    PRIMARY KEY (version_id, participant_id)
);

CREATE INDEX IF NOT EXISTS idx_result_rows_rank
    ON result_rows (version_id, rank);

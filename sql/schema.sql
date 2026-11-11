-- WorkBuddy 半自动量化信号系统 · SQLite schema v1
-- 所有时间戳为 ISO8601 且带 +08:00 偏移；禁止裸 UTC。
-- 开启 WAL / busy_timeout 由 core/db.py 的 connect() 负责。

-- 1. 原始行情：每一轮采集全量落库，是「禁止前视偏差」可 SQL 审计的基础。
CREATE TABLE IF NOT EXISTS quote_table (
    quote_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_ts     TEXT NOT NULL,                 -- 行情源「更新时间」
    ingest_ts    TEXT NOT NULL,                 -- 本地入库时间
    source       TEXT NOT NULL DEFAULT 'primary',
    code_a       TEXT NOT NULL,
    price_a      REAL,
    code_b       TEXT NOT NULL,
    price_b      REAL,
    ratio        REAL,
    iopv_a       REAL,
    iopv_b       REAL,
    is_valid     INTEGER NOT NULL DEFAULT 1,
    drop_reason  TEXT,
    UNIQUE(quote_ts, source)
);
CREATE INDEX IF NOT EXISTS idx_quote_ts ON quote_table(quote_ts);
CREATE INDEX IF NOT EXISTS idx_quote_valid ON quote_table(is_valid, quote_ts);

-- 2. 信号：quote_id 绑定生成时所用行情，供 SQL 审计。
CREATE TABLE IF NOT EXISTS signal_table (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_ts         TEXT NOT NULL,
    signal_type       TEXT NOT NULL,           -- spread_long | spread_short
    price_a           REAL,
    price_b           REAL,
    ratio             REAL,
    base_ratio        REAL,
    tp                REAL,
    sl                REAL,
    status            TEXT NOT NULL DEFAULT 'open',   -- open | closed
    settle_ts         TEXT,
    result            TEXT,                    -- win | loss | hold
    result_ratio      REAL,
    quote_id          INTEGER,                -- 必须指向 quote_ts <= signal_ts 的行
    config_hash       TEXT NOT NULL,
    strategy_version  TEXT NOT NULL,
    ambiguity         INTEGER NOT NULL DEFAULT 0,
    note              TEXT,
    UNIQUE(signal_ts, signal_type, config_hash)
);
CREATE INDEX IF NOT EXISTS idx_signal_status ON signal_table(status);
CREATE INDEX IF NOT EXISTS idx_signal_cfg_ts ON signal_table(config_hash, signal_ts);
CREATE INDEX IF NOT EXISTS idx_signal_result ON signal_table(result, config_hash);

-- 3. 冻结快照：每日 15:10 唯一冻结一次，盘中不写入。
CREATE TABLE IF NOT EXISTS snapshot_table (
    snap_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    snap_ts            TEXT NOT NULL,
    freeze_date        TEXT NOT NULL,          -- YYYY-MM-DD，同日幂等
    scope              TEXT NOT NULL,          -- full | roll100 | roll200
    config_hash        TEXT NOT NULL,
    strategy_version   TEXT NOT NULL,
    n                  INTEGER NOT NULL,
    win_cnt            INTEGER NOT NULL,
    loss_cnt           INTEGER NOT NULL,
    hold_cnt           INTEGER NOT NULL,
    ambiguous_cnt      INTEGER NOT NULL,
    effective_sessions INTEGER NOT NULL,
    win_rate           REAL NOT NULL,
    break_even         REAL NOT NULL,
    ci_low             REAL NOT NULL,
    ci_high            REAL NOT NULL,
    qualified          INTEGER NOT NULL,
    qualified_reason   TEXT NOT NULL,
    cost_per_unit      REAL NOT NULL,
    created_by         TEXT NOT NULL,          -- eod | manual
    UNIQUE(freeze_date, scope, config_hash, created_by)
);
CREATE INDEX IF NOT EXISTS idx_snap_cfg_date ON snapshot_table(config_hash, freeze_date);

-- 4. 漂移记录：互斥窗口两比例检验，含效应量与校正后临界值。
CREATE TABLE IF NOT EXISTS drift_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    config_hash      TEXT NOT NULL,
    scope            TEXT NOT NULL,
    status           TEXT NOT NULL,           -- ok | insufficient | alert | config_change
    window_size      INTEGER NOT NULL,
    n1 INTEGER, k1 INTEGER, p1 REAL,
    n2 INTEGER, k2 INTEGER, p2 REAL,
    z_stat           REAL,
    z_critical       REAL,
    effect_size      REAL,
    min_effect_size  REAL,
    drift_alert      INTEGER NOT NULL DEFAULT 0,
    action           TEXT,
    note             TEXT
);
CREATE INDEX IF NOT EXISTS idx_drift_cfg_ts ON drift_log(config_hash, ts);

-- 5. 全链路日志：每一条指标计算附带输入参数，可复现校验。
CREATE TABLE IF NOT EXISTS run_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    module       TEXT NOT NULL,
    action       TEXT NOT NULL,
    input_json   TEXT,
    output_json  TEXT,
    level        TEXT NOT NULL DEFAULT 'INFO',
    message      TEXT
);
CREATE INDEX IF NOT EXISTS idx_runlog_ts ON run_log(ts);
CREATE INDEX IF NOT EXISTS idx_runlog_action ON run_log(module, action);

-- 6. 全局状态：跨会话持久，pause_trade_alert 不放在内存变量里。
CREATE TABLE IF NOT EXISTS system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_ts  TEXT NOT NULL,
    operator    TEXT,
    reason      TEXT
);

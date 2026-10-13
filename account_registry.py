"""Persistent monitoring targets and an atomic migration of legacy usage rows."""
import json


def initialize_accounts(db, local_account):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS monitor_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS monitored_accounts (
            account_key TEXT PRIMARY KEY, label TEXT NOT NULL, account_json TEXT NOT NULL,
            enabled INTEGER NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS discovered_accounts (
            account_key TEXT PRIMARY KEY, account_json TEXT NOT NULL, seen_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS collector_node_accounts (
            account_key TEXT NOT NULL, node_key TEXT NOT NULL,
            account_json TEXT NOT NULL, seen_at INTEGER NOT NULL,
            PRIMARY KEY (account_key, node_key)
        );
    """)
    db.execute("BEGIN IMMEDIATE")
    initialized = db.execute("SELECT 1 FROM monitor_settings WHERE key='accounts_initialized'").fetchone()
    if not initialized:
        row = db.execute("SELECT account_key FROM account_usage ORDER BY received_at DESC LIMIT 1").fetchone()
        key = row[0] if row else local_account.get("key", "unknown")
        if key != "unknown":
            account = dict(local_account) if local_account.get("key") == key else {"key": key}
            for node in db.execute("SELECT account_json FROM collector_nodes ORDER BY seen_at DESC"):
                known = json.loads(node[0])
                if known.get("key") == key:
                    account = {**known, **{field: value for field, value in account.items() if value is not None}}
                    break
            db.execute("INSERT OR IGNORE INTO monitored_accounts VALUES (?, ?, ?, 1, 0)",
                       (key, account.get("email") or account.get("name") or key, json.dumps(account)))
            db.execute("INSERT OR REPLACE INTO monitor_settings VALUES ('default_account', ?)", (key,))
        db.execute("INSERT INTO monitor_settings VALUES ('accounts_initialized', '1')")
    row = db.execute("SELECT value FROM monitor_settings WHERE key='default_account'").fetchone()
    default = row[0] if row else "unknown"
    for node in db.execute("SELECT * FROM collector_nodes"):
        account = json.loads(node["account_json"])
        key = account.get("key") or default
        db.execute("INSERT OR IGNORE INTO collector_node_accounts VALUES (?, ?, ?, ?)",
                   (key, node["node_key"], node["account_json"], node["seen_at"]))
        db.execute("INSERT OR IGNORE INTO discovered_accounts VALUES (?, ?, ?)",
                   (key, node["account_json"], node["seen_at"]))
    # Tables retain all old columns, with an additional account dimension in
    # their primary keys. Rebuilding and copying occur in one transaction.
    primary_keys = {
        "overview_samples": "account_key, observed_at",
        "session_samples": "account_key, observed_at, node_key, session_key",
        "collector_sessions": "account_key, node_key, session_key",
        "collector_session_usage": "account_key, node_key, session_key, bucket_at",
    }
    for table, primary_key in primary_keys.items():
        columns = list(db.execute(f"PRAGMA table_info({table})"))
        if any(column["name"] == "account_key" for column in columns):
            continue
        indexes = [row[0] for row in db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]
        definitions = ", ".join(f"{column['name']} {column['type']}" + (" NOT NULL" if column['notnull'] or column['pk'] else "")
                                for column in columns)
        db.execute(f"CREATE TABLE {table}_accounts ({definitions}, account_key TEXT NOT NULL, PRIMARY KEY ({primary_key}))")
        names = ", ".join(column["name"] for column in columns)
        if "node_key" in names:
            db.execute(f"""INSERT INTO {table}_accounts
                SELECT legacy.*, COALESCE(json_extract(n.account_json, '$.key'), ?)
                FROM {table} legacy LEFT JOIN collector_nodes n ON n.node_key=legacy.node_key""", (default,))
        else:
            db.execute(f"INSERT INTO {table}_accounts SELECT *, ? FROM {table}", (default,))
        db.execute(f"DROP TABLE {table}")
        db.execute(f"ALTER TABLE {table}_accounts RENAME TO {table}")
        for index in indexes:
            db.execute(index)
    db.execute("CREATE INDEX IF NOT EXISTS idx_account_events ON collector_session_usage(account_key, bucket_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_account_snapshots ON session_samples(account_key, observed_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_account_session_time ON session_samples(account_key, node_key, session_key, observed_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_session_retention ON session_samples(observed_at)")

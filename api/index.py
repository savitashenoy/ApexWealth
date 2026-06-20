from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import json, os, hashlib, uuid, time, operator, tempfile
from datetime import datetime, date
from urllib.parse import urlparse, unquote
import html as html_lib
import requests
try:
    from curl_cffi import requests as crequests
except Exception:  # local/dev fallback if curl_cffi is unavailable
    crequests = None
import yfinance as yf
import pandas as pd
import numpy as np

try:
    from .broker_normalizer import normalize_file, BROKER_HINTS
    from .import_trades import match_fifo_to_apex, holding_key, trade_key
    from .zerodha_layer import (
        ZerodhaError, kite_login_url, generate_session, get_profile, get_holdings as kite_get_holdings,
        place_order as kite_place_order, wait_for_complete as kite_wait_for_complete,
        normalise_exchange_symbol, extract_executed
    )
except Exception:
    # Vercel imports api/index.py as a module, local runs may import from cwd.
    from broker_normalizer import normalize_file, BROKER_HINTS
    from import_trades import match_fifo_to_apex, holding_key, trade_key
    from zerodha_layer import (
        ZerodhaError, kite_login_url, generate_session, get_profile, get_holdings as kite_get_holdings,
        place_order as kite_place_order, wait_for_complete as kite_wait_for_complete,
        normalise_exchange_symbol, extract_executed
    )

# Pullers/Draggers market page data is optional for auth/core APIs.
# On Vercel, api/index.py may be imported as a standalone function module,
# so normal relative imports can fail before Flask can return JSON. Keep this
# import fully guarded so login/signup never fail because Markets data failed.
def _load_weighted_index_data_safe():
    try:
        from .weighted_index_data import nifty50_data, banknifty_data, sensex_data
        return nifty50_data, banknifty_data, sensex_data, None
    except Exception as e1:
        try:
            from weighted_index_data import nifty50_data, banknifty_data, sensex_data
            return nifty50_data, banknifty_data, sensex_data, None
        except Exception as e2:
            try:
                import importlib.util
                module_path = os.path.join(os.path.dirname(__file__), 'weighted_index_data.py')
                spec = importlib.util.spec_from_file_location('weighted_index_data_safe', module_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod.nifty50_data, mod.banknifty_data, mod.sensex_data, None
            except Exception as e3:
                return [], [], [], f'{e1}; {e2}; {e3}'

nifty50_data, banknifty_data, sensex_data, WEIGHTED_INDEX_DATA_ERROR = _load_weighted_index_data_safe()

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None

app = Flask(__name__, static_folder='../static', static_url_path='/static')
CORS(app)

# Vercel serverless filesystem note:
# /var/task (the deployed project folder) is read-only at runtime. Writing JSON files
# into the repo's data/ directory works locally but fails on Vercel and can return an
# HTML 500 page, which the frontend then reports as: Unexpected token '<'.
# Use /tmp on Vercel so auth/add/remove APIs always return JSON. For permanent
# multi-user production storage, connect a persistent DB/KV later.
PROJECT_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data'))
DATA_DIR = os.environ.get('APEXWEALTH_DATA_DIR')
if not DATA_DIR:
    DATA_DIR = '/tmp/apexwealth_data' if os.environ.get('VERCEL') else PROJECT_DATA_DIR
os.makedirs(DATA_DIR, exist_ok=True)

USERS_FILE = os.path.join(DATA_DIR, 'users.json')
PORTFOLIOS_FILE = os.path.join(DATA_DIR, 'portfolios.json')
WATCHLISTS_FILE = os.path.join(DATA_DIR, 'watchlists.json')
TRADES_FILE = os.path.join(DATA_DIR, 'trades.json')
ALERTS_FILE = os.path.join(DATA_DIR, 'portfolio_alerts.json')
ZERODHA_SESSIONS_FILE = os.path.join(DATA_DIR, 'zerodha_sessions.json')
ZERODHA_CREDENTIALS_FILE = os.path.join(DATA_DIR, 'zerodha_credentials.json')

# Optional server-level Zerodha fallback credentials. Each Apex user can also save
# their own Kite API key/secret from Settings -> API Settings. The API secret is
# never sent back to the browser; only a masked/saved status is returned.
ZERODHA_API_KEY = os.environ.get('KITE_API_KEY') or os.environ.get('ZERODHA_API_KEY') or ''
ZERODHA_API_SECRET = os.environ.get('KITE_API_SECRET') or os.environ.get('ZERODHA_API_SECRET') or ''

def zerodha_configured(user_id=None):
    if user_id:
        creds = db_get_zerodha_credentials(user_id)
        if creds and creds.get('api_key') and creds.get('api_secret'):
            return True
    return bool(ZERODHA_API_KEY and ZERODHA_API_SECRET)

def _mask_secret(value, visible=4):
    value = str(value or '')
    if not value:
        return ''
    if len(value) <= visible:
        return '•' * len(value)
    return ('•' * max(4, len(value) - visible)) + value[-visible:]


# ─── PERSISTENT STORAGE: Neon PostgreSQL on Vercel ───────────────────────────
# Set one of these env vars in Vercel from your Neon dashboard:
# DATABASE_URL or POSTGRES_URL or NEON_DATABASE_URL
RAW_DATABASE_URL = (
    os.environ.get('DATABASE_URL')
    or os.environ.get('POSTGRES_URL')
    or os.environ.get('NEON_DATABASE_URL')
    or ''
).strip()
DB_INIT_DONE = False
DB_LAST_ERROR = None

def _mask_database_url(url):
    if not url:
        return ''
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ''
        dbname = (parsed.path or '').lstrip('/')
        return f'{parsed.scheme}://***:***@{host}/{dbname}'
    except Exception:
        return '***masked***'

def _validate_database_url(url):
    if not url:
        return None
    lowered = url.lower()
    parsed = urlparse(url)
    host = parsed.hostname or ''
    if not parsed.scheme.startswith('postgres'):
        return 'DATABASE_URL must start with postgresql:// or postgres://.'
    if not host:
        return 'DATABASE_URL is missing the Neon host name.'
    placeholder_tokens = ['host.neon.tech', 'user:', 'password@', 'dbname', '<', '>', 'your_', 'replace_']
    if any(token in lowered for token in placeholder_tokens) or host.upper().startswith('HOST'):
        return 'DATABASE_URL still contains placeholder text. Replace USER, PASSWORD, HOST and DBNAME with the exact Neon connection string.'
    if 'neon.tech' not in host:
        return f'DATABASE_URL host is {host}. For Neon it should usually end with .neon.tech or -pooler.*.neon.tech.'
    if 'sslmode=' not in lowered:
        return 'DATABASE_URL must include sslmode=require for Neon/Vercel.'
    return None

DATABASE_URL_ERROR = _validate_database_url(RAW_DATABASE_URL)
DATABASE_URL = RAW_DATABASE_URL if not DATABASE_URL_ERROR else ''

def db_configured():
    return bool(DATABASE_URL and psycopg is not None and not DATABASE_URL_ERROR)

def db_status_payload():
    return {
        'database_configured': bool(RAW_DATABASE_URL),
        'database_url_valid': not bool(DATABASE_URL_ERROR) if RAW_DATABASE_URL else False,
        'database_url_issue': DATABASE_URL_ERROR,
        'database_url_masked': _mask_database_url(RAW_DATABASE_URL),
        'psycopg_available': psycopg is not None,
    }

def db_connect():
    if DATABASE_URL_ERROR:
        raise RuntimeError(DATABASE_URL_ERROR)
    if not db_configured():
        raise RuntimeError('DATABASE_URL/POSTGRES_URL is not configured or psycopg is not installed')
    return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row, connect_timeout=10)

def init_db():
    """Create / migrate Neon tables once per cold start. Safe to run repeatedly.

    Important for Vercel/Neon upgrades:
    older deployments had watchlist PRIMARY KEY (user_id, symbol) and no
    group_name column. The migration must add group_name before any index or
    query references group_name, otherwise Neon raises: column "group_name" does not exist.
    """
    global DB_INIT_DONE, DB_LAST_ERROR
    if DB_INIT_DONE or not db_configured():
        return DB_INIT_DONE
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                # Core tables
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id TEXT PRIMARY KEY,
                        email TEXT UNIQUE NOT NULL,
                        password TEXT NOT NULL,
                        created TIMESTAMPTZ DEFAULT NOW(),
                        last_login TIMESTAMPTZ
                    )
                """)
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMPTZ")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'APPROVED'")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_enabled BOOLEAN NOT NULL DEFAULT TRUE")
                cur.execute("UPDATE users SET status='APPROVED' WHERE status IS NULL OR TRIM(status)=''")
                cur.execute("UPDATE users SET is_enabled=TRUE WHERE is_enabled IS NULL")

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS holdings (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        name TEXT,
                        buy_price DOUBLE PRECISION NOT NULL DEFAULT 0,
                        qty DOUBLE PRECISION NOT NULL DEFAULT 0,
                        date TEXT,
                        industry TEXT,
                        sector TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_holdings_user ON holdings(user_id)")

                # Watchlist table. If an older table already exists, this statement
                # does not change it; the migration block below upgrades it safely.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS watchlist (
                        user_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        name TEXT,
                        industry TEXT,
                        added TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)

                # Ensure grouped-watchlist columns exist BEFORE any index/query uses them.
                cur.execute("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS group_name TEXT")
                cur.execute("UPDATE watchlist SET group_name='Default' WHERE group_name IS NULL OR group_name='' ")
                cur.execute("ALTER TABLE watchlist ALTER COLUMN group_name SET DEFAULT 'Default'")
                cur.execute("ALTER TABLE watchlist ALTER COLUMN group_name SET NOT NULL")

                # Remove duplicate rows before creating the grouped primary key.
                cur.execute("""
                    DELETE FROM watchlist a
                    USING watchlist b
                    WHERE a.ctid < b.ctid
                      AND a.user_id = b.user_id
                      AND a.group_name = b.group_name
                      AND a.symbol = b.symbol
                """)

                # Drop any previous primary key, including the old (user_id, symbol) key.
                cur.execute("""
                    DO $$
                    DECLARE pk_name text;
                    BEGIN
                        SELECT conname INTO pk_name
                        FROM pg_constraint
                        WHERE conrelid = 'watchlist'::regclass
                          AND contype = 'p'
                        LIMIT 1;
                        IF pk_name IS NOT NULL THEN
                            EXECUTE format('ALTER TABLE watchlist DROP CONSTRAINT %I', pk_name);
                        END IF;
                    END $$;
                """)
                cur.execute("ALTER TABLE watchlist ADD CONSTRAINT watchlist_pkey PRIMARY KEY (user_id, group_name, symbol)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_user_group ON watchlist(user_id, group_name)")

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS watchlist_groups (
                        user_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        PRIMARY KEY (user_id, name)
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_groups_user ON watchlist_groups(user_id)")
                cur.execute("INSERT INTO watchlist_groups (user_id, name) SELECT DISTINCT user_id, COALESCE(NULLIF(group_name,''),'Default') FROM watchlist ON CONFLICT DO NOTHING")

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS trades (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        name TEXT,
                        buy_price DOUBLE PRECISION NOT NULL DEFAULT 0,
                        sell_price DOUBLE PRECISION NOT NULL DEFAULT 0,
                        qty DOUBLE PRECISION NOT NULL DEFAULT 0,
                        buy_date TEXT,
                        sell_date TEXT,
                        pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
                        pnl_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_user ON trades(user_id)")

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS portfolio_alerts (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        holding_id TEXT,
                        symbol TEXT NOT NULL,
                        column_name TEXT NOT NULL,
                        condition_op TEXT NOT NULL,
                        threshold DOUBLE PRECISION NOT NULL,
                        active BOOLEAN NOT NULL DEFAULT TRUE,
                        triggered_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("ALTER TABLE portfolio_alerts ADD COLUMN IF NOT EXISTS triggered_at TIMESTAMPTZ")
                cur.execute("ALTER TABLE portfolio_alerts ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'portfolio'")
                cur.execute("UPDATE portfolio_alerts SET source='portfolio' WHERE source IS NULL OR source=''")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_alerts_user ON portfolio_alerts(user_id)")

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS zerodha_sessions (
                        user_id TEXT PRIMARY KEY,
                        access_token TEXT NOT NULL,
                        public_token TEXT,
                        kite_user_id TEXT,
                        user_name TEXT,
                        email TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_zerodha_sessions_user ON zerodha_sessions(user_id)")

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS zerodha_credentials (
                        user_id TEXT PRIMARY KEY,
                        api_key TEXT NOT NULL,
                        api_secret TEXT NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_zerodha_credentials_user ON zerodha_credentials(user_id)")

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS zerodha_order_audit (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        order_id TEXT,
                        symbol TEXT NOT NULL,
                        transaction_type TEXT NOT NULL,
                        qty DOUBLE PRECISION NOT NULL DEFAULT 0,
                        price DOUBLE PRECISION NOT NULL DEFAULT 0,
                        status TEXT,
                        apex_updated BOOLEAN NOT NULL DEFAULT FALSE,
                        payload JSONB,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_zerodha_order_audit_user ON zerodha_order_audit(user_id)")
        DB_INIT_DONE = True
        DB_LAST_ERROR = None
        return True
    except Exception as e:
        DB_LAST_ERROR = str(e)
        return False

def _dt_to_str(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value

def _row_to_dict(row):
    return {k: _dt_to_str(v) for k, v in dict(row).items()}

def db_get_user(email):
    if not init_db():
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id, email, password, created, last_login, status, is_enabled FROM users WHERE email=%s', (email,))
            row = cur.fetchone()
    return _row_to_dict(row) if row else None

def db_create_user(email, password_hash, status='PENDING', is_enabled=True):
    if not init_db():
        raise RuntimeError(DB_LAST_ERROR or 'Database is not initialized')
    user_id = str(uuid.uuid4())
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO users (id, email, password, created, last_login, status, is_enabled) VALUES (%s,%s,%s,NOW(),NULL,%s,%s)', (user_id, email, password_hash, str(status or 'PENDING').upper(), bool(is_enabled)))
    return {'id': user_id, 'email': email}


def db_get_user_by_id(user_id):
    if not init_db():
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id, email, password, created, last_login, status, is_enabled FROM users WHERE id=%s', (user_id,))
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def db_update_last_login(user_id):
    if not init_db():
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE users SET last_login=NOW() WHERE id=%s RETURNING last_login', (user_id,))
            row = cur.fetchone()
    return _dt_to_str(row.get('last_login')) if row else None


def db_update_password_by_id(user_id, password_hash):
    if not init_db():
        raise RuntimeError(DB_LAST_ERROR or 'Database is not initialized')
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE users SET password=%s WHERE id=%s', (password_hash, user_id))
            return cur.rowcount

def db_update_password(email, password_hash):
    if not init_db():
        raise RuntimeError(DB_LAST_ERROR or 'Database is not initialized')
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE users SET password=%s WHERE email=%s', (password_hash, email))
            return cur.rowcount

def db_get_holdings(user_id):
    if not init_db():
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id, symbol, name, buy_price, qty, date, industry, sector FROM holdings WHERE user_id=%s ORDER BY created_at, symbol', (user_id,))
            rows = cur.fetchall() or []
    return [_row_to_dict(r) for r in rows]

def db_add_holding(user_id, holding):
    if not init_db():
        raise RuntimeError(DB_LAST_ERROR or 'Database is not initialized')
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO holdings (id, user_id, symbol, name, buy_price, qty, date, industry, sector) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)', (holding['id'], user_id, holding['symbol'], holding.get('name'), holding['buy_price'], holding['qty'], holding.get('date'), holding.get('industry',''), holding.get('sector','')))
    return holding

def db_update_holding(user_id, holding_id, buy_price=None, qty=None, holding_date=None):
    if not init_db():
        return None
    updates, vals = [], []
    if buy_price is not None:
        updates.append('buy_price=%s'); vals.append(float(buy_price))
    if qty is not None:
        updates.append('qty=%s'); vals.append(float(qty))
    if holding_date is not None:
        updates.append('date=%s'); vals.append(holding_date)
    if not updates:
        return 0
    vals.extend([user_id, holding_id])
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE holdings SET {', '.join(updates)} WHERE user_id=%s AND id=%s", vals)
            return cur.rowcount

def db_delete_holding(user_id, holding_id):
    if not init_db():
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM holdings WHERE user_id=%s AND id=%s', (user_id, holding_id))
            return cur.rowcount

def db_get_holding(user_id, holding_id):
    if not init_db():
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id, symbol, name, buy_price, qty, date, industry, sector FROM holdings WHERE user_id=%s AND id=%s', (user_id, holding_id))
            row = cur.fetchone()
    return _row_to_dict(row) if row else None

def db_set_holding_qty_or_delete(user_id, holding_id, new_qty):
    if new_qty <= 0:
        return db_delete_holding(user_id, holding_id)
    if not init_db():
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE holdings SET qty=%s WHERE user_id=%s AND id=%s', (float(new_qty), user_id, holding_id))
            return cur.rowcount

def normalize_watchlist_group(name):
    cleaned = str(name or '').strip()
    return cleaned[:60] if cleaned else 'Default'

def db_get_watchlist_groups(user_id):
    if not init_db():
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO watchlist_groups (user_id, name) VALUES (%s, 'Default') ON CONFLICT DO NOTHING", (user_id,))
            cur.execute('SELECT name FROM watchlist_groups WHERE user_id=%s ORDER BY CASE WHEN name=%s THEN 0 ELSE 1 END, created_at, name', (user_id, 'Default'))
            rows = cur.fetchall() or []
    groups = [r.get('name') for r in rows if r.get('name')]
    return groups or ['Default']

def db_add_watchlist_group(user_id, name):
    group_name = normalize_watchlist_group(name)
    if not init_db():
        raise RuntimeError(DB_LAST_ERROR or 'Database is not initialized')
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO watchlist_groups (user_id, name) VALUES (%s,%s) ON CONFLICT DO NOTHING', (user_id, group_name))
            return group_name

def db_delete_watchlist_group(user_id, group_name):
    group_name = normalize_watchlist_group(group_name)
    if group_name == 'Default':
        return 0
    if not init_db():
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM watchlist WHERE user_id=%s AND group_name=%s', (user_id, group_name))
            removed_items = cur.rowcount
            cur.execute('DELETE FROM watchlist_groups WHERE user_id=%s AND name=%s', (user_id, group_name))
            removed_group = cur.rowcount
    return {'removed_items': removed_items, 'removed_group': removed_group}

def db_get_watchlist(user_id, group_name='Default'):
    if not init_db():
        return None
    group_name = normalize_watchlist_group(group_name)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO watchlist_groups (user_id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (user_id, group_name))
            cur.execute('SELECT group_name, symbol, name, industry, added FROM watchlist WHERE user_id=%s AND group_name=%s ORDER BY created_at, symbol', (user_id, group_name))
            rows = cur.fetchall() or []
    return [_row_to_dict(r) for r in rows]

def db_add_watchlist(user_id, item, group_name='Default'):
    if not init_db():
        raise RuntimeError(DB_LAST_ERROR or 'Database is not initialized')
    group_name = normalize_watchlist_group(group_name)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO watchlist_groups (user_id, name) VALUES (%s,%s) ON CONFLICT DO NOTHING', (user_id, group_name))
            cur.execute('INSERT INTO watchlist (user_id, group_name, symbol, name, industry, added) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (user_id, group_name, symbol) DO NOTHING', (user_id, group_name, item['symbol'], item.get('name'), item.get('industry',''), item.get('added')))
            return cur.rowcount

def db_delete_watchlist(user_id, symbol, group_name='Default', purge_all=True):
    """Delete a watchlist symbol permanently.

    Uses both strict and compact symbol matching so special NSE symbols like
    J&KBANK are removed even if old rows were stored as J%26KBANK, J&amp;KBANK,
    J&KBANK.NS, or JKBANK during earlier migrations/imports.
    """
    if not init_db():
        return None
    target = _strip_exchange_suffix(symbol)
    target_compact = _compact_symbol_key(target)
    group_name = normalize_watchlist_group(group_name)
    sql_norm = """
        REPLACE(REPLACE(REPLACE(REPLACE(UPPER(TRIM(symbol)), '&AMP;', '&'), '%%26', '&'), '.NS', ''), '.BO', '')
    """
    sql_compact = """
        REGEXP_REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(UPPER(TRIM(symbol)), '&AMP;', '&'), '%%26', '&'), '.NS', ''), '.BO', ''), '[^A-Z0-9]', '', 'g')
    """
    with db_connect() as conn:
        with conn.cursor() as cur:
            if purge_all:
                cur.execute(f"""
                    DELETE FROM watchlist
                    WHERE user_id=%s
                      AND (({sql_norm})=%s OR ({sql_compact})=%s)
                """, (user_id, target, target_compact))
            else:
                cur.execute(f"""
                    DELETE FROM watchlist
                    WHERE user_id=%s
                      AND group_name=%s
                      AND (({sql_norm})=%s OR ({sql_compact})=%s)
                """, (user_id, group_name, target, target_compact))
            return cur.rowcount

def db_get_trades(user_id):
    if not init_db():
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id, symbol, name, buy_price, sell_price, qty, buy_date, sell_date, pnl, pnl_pct FROM trades WHERE user_id=%s ORDER BY created_at DESC', (user_id,))
            rows = cur.fetchall() or []
    return [_row_to_dict(r) for r in rows]

def db_add_trade(user_id, trade):
    if not init_db():
        raise RuntimeError(DB_LAST_ERROR or 'Database is not initialized')
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO trades (id, user_id, symbol, name, buy_price, sell_price, qty, buy_date, sell_date, pnl, pnl_pct) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', (trade['id'], user_id, trade['symbol'], trade.get('name'), trade['buy_price'], trade['sell_price'], trade['qty'], trade.get('buy_date'), trade.get('sell_date'), trade['pnl'], trade['pnl_pct']))
    return trade


def db_delete_trade(user_id, trade_id):
    if not init_db():
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM trades WHERE user_id=%s AND id=%s', (user_id, trade_id))
            return cur.rowcount


def db_get_zerodha_credentials(user_id):
    """Return user-level Zerodha Kite credentials, or env fallback if no user row exists."""
    if db_configured() and init_db():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT user_id, api_key, api_secret, created_at, updated_at FROM zerodha_credentials WHERE user_id=%s', (user_id,))
                row = cur.fetchone()
        if row:
            return _row_to_dict(row)
    else:
        blob = load_json(ZERODHA_CREDENTIALS_FILE)
        if user_id in blob:
            return blob.get(user_id)
    if ZERODHA_API_KEY and ZERODHA_API_SECRET:
        return {'user_id': user_id, 'api_key': ZERODHA_API_KEY, 'api_secret': ZERODHA_API_SECRET, 'source': 'env'}
    return None


def db_save_zerodha_credentials(user_id, api_key, api_secret):
    api_key = str(api_key or '').strip()
    api_secret = str(api_secret or '').strip()
    if not api_key or not api_secret:
        raise ValueError('Both Zerodha API Key and API Secret are required.')
    if db_configured() and init_db():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO zerodha_credentials (user_id, api_key, api_secret, updated_at)
                    VALUES (%s,%s,%s,NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                      api_key=EXCLUDED.api_key,
                      api_secret=EXCLUDED.api_secret,
                      updated_at=NOW()
                """, (user_id, api_key, api_secret))
        return True
    blob = load_json(ZERODHA_CREDENTIALS_FILE)
    blob[user_id] = {'user_id': user_id, 'api_key': api_key, 'api_secret': api_secret, 'updated_at': datetime.utcnow().isoformat()}
    save_json(ZERODHA_CREDENTIALS_FILE, blob)
    return True


def db_delete_zerodha_credentials(user_id):
    if db_configured() and init_db():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM zerodha_credentials WHERE user_id=%s', (user_id,))
                removed = cur.rowcount
                cur.execute('DELETE FROM zerodha_sessions WHERE user_id=%s', (user_id,))
                return removed
    blob = load_json(ZERODHA_CREDENTIALS_FILE)
    existed = 1 if user_id in blob else 0
    blob.pop(user_id, None)
    save_json(ZERODHA_CREDENTIALS_FILE, blob)
    sess = load_json(ZERODHA_SESSIONS_FILE)
    sess.pop(user_id, None)
    save_json(ZERODHA_SESSIONS_FILE, sess)
    return existed


def db_get_zerodha_session(user_id):
    if db_configured() and init_db():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT user_id, access_token, public_token, kite_user_id, user_name, email, created_at, updated_at FROM zerodha_sessions WHERE user_id=%s', (user_id,))
                row = cur.fetchone()
        return _row_to_dict(row) if row else None
    blob = load_json(ZERODHA_SESSIONS_FILE)
    return blob.get(user_id)


def db_save_zerodha_session(user_id, session):
    payload = dict(session or {})
    access_token = payload.get('access_token')
    if not access_token:
        raise ValueError('Missing Zerodha access token')
    if db_configured() and init_db():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO zerodha_sessions (user_id, access_token, public_token, kite_user_id, user_name, email, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                      access_token=EXCLUDED.access_token,
                      public_token=EXCLUDED.public_token,
                      kite_user_id=EXCLUDED.kite_user_id,
                      user_name=EXCLUDED.user_name,
                      email=EXCLUDED.email,
                      updated_at=NOW()
                """, (user_id, access_token, payload.get('public_token'), payload.get('kite_user_id') or payload.get('user_id'), payload.get('user_name') or payload.get('user_shortname'), payload.get('email')))
        return True
    blob = load_json(ZERODHA_SESSIONS_FILE)
    blob[user_id] = {**payload, 'updated_at': datetime.utcnow().isoformat()}
    save_json(ZERODHA_SESSIONS_FILE, blob)
    return True


def db_delete_zerodha_session(user_id):
    if db_configured() and init_db():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM zerodha_sessions WHERE user_id=%s', (user_id,))
                return cur.rowcount
    blob = load_json(ZERODHA_SESSIONS_FILE)
    existed = 1 if user_id in blob else 0
    blob.pop(user_id, None)
    save_json(ZERODHA_SESSIONS_FILE, blob)
    return existed


def db_add_zerodha_order_audit(user_id, order):
    row = {
        'id': str(uuid.uuid4()),
        'user_id': user_id,
        'order_id': order.get('order_id'),
        'symbol': order.get('symbol') or '',
        'transaction_type': order.get('transaction_type') or '',
        'qty': float(order.get('qty') or 0),
        'price': float(order.get('price') or 0),
        'status': order.get('status') or '',
        'apex_updated': bool(order.get('apex_updated')),
        'payload': order,
    }
    if db_configured() and init_db():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO zerodha_order_audit (id, user_id, order_id, symbol, transaction_type, qty, price, status, apex_updated, payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                """, (row['id'], user_id, row['order_id'], row['symbol'], row['transaction_type'], row['qty'], row['price'], row['status'], row['apex_updated'], json.dumps(order)))
    return row

def db_get_portfolio_alerts(user_id):
    if not init_db():
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, user_id, holding_id, symbol, column_name, condition_op, threshold, active, triggered_at, created_at, COALESCE(source,'portfolio') AS source FROM portfolio_alerts WHERE user_id=%s AND active=TRUE ORDER BY created_at DESC", (user_id,))
            rows = cur.fetchall() or []
    return [_row_to_dict(r) for r in rows]

def db_add_portfolio_alert(user_id, data):
    if not init_db():
        raise RuntimeError(DB_LAST_ERROR or 'Database is not initialized')
    alert_id = str(uuid.uuid4())
    source = str(data.get('source') or '').lower().strip()
    holding_id = str(data.get('holding_id') or '').strip()
    if source not in ('portfolio', 'watchlist'):
        source = 'portfolio' if holding_id else 'watchlist'
    column_name = str(data.get('column_name') or data.get('column') or 'ltp')
    condition_op = str(data.get('condition_op') or data.get('condition') or '>')
    allowed_cols = ('ltp', 'day_chg_pct') if source == 'watchlist' else ('ltp', 'pnl_pct', 'day_chg_pct')
    if column_name not in allowed_cols:
        raise ValueError('Invalid alert column')
    if condition_op not in ('>', '>=', '=', '<', '<='):
        raise ValueError('Invalid alert condition')
    threshold = float(data.get('threshold'))
    symbol = str(data.get('symbol') or '').upper().strip()
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO portfolio_alerts (id, user_id, holding_id, symbol, column_name, condition_op, threshold, source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)', (alert_id, user_id, holding_id, symbol, column_name, condition_op, threshold, source))
    return {'id': alert_id, 'user_id': user_id, 'holding_id': holding_id, 'symbol': symbol, 'column_name': column_name, 'condition_op': condition_op, 'threshold': threshold, 'active': True, 'source': source}

def db_update_portfolio_alert(user_id, alert_id, data):
    if not init_db():
        raise RuntimeError(DB_LAST_ERROR or 'Database is not initialized')
    column_name = str(data.get('column_name') or data.get('column') or 'ltp')
    condition_op = str(data.get('condition_op') or data.get('condition') or '>')
    source = str(data.get('source') or '').lower().strip()
    if not source:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT COALESCE(source, %s) AS source FROM portfolio_alerts WHERE user_id=%s AND id=%s', ('portfolio', user_id, alert_id))
                row = cur.fetchone()
                source = row['source'] if row else 'portfolio'
    allowed_cols = ('ltp', 'day_chg_pct') if source == 'watchlist' else ('ltp', 'pnl_pct', 'day_chg_pct')
    if column_name not in allowed_cols:
        raise ValueError('Invalid alert column')
    if condition_op not in ('>', '>=', '=', '<', '<='):
        raise ValueError('Invalid alert condition')
    threshold = float(data.get('threshold'))
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                UPDATE portfolio_alerts
                SET column_name=%s, condition_op=%s, threshold=%s, triggered_at=NULL, active=TRUE
                WHERE user_id=%s AND id=%s
            ''', (column_name, condition_op, threshold, user_id, alert_id))
            removed = cur.rowcount
    return {'id': alert_id, 'column_name': column_name, 'condition_op': condition_op, 'threshold': threshold, 'source': source, 'updated': removed}


def db_mark_portfolio_alert_triggered(user_id, alert_id):
    if not init_db():
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                UPDATE portfolio_alerts
                SET triggered_at = COALESCE(triggered_at, NOW())
                WHERE user_id=%s AND id=%s AND active=TRUE
                RETURNING triggered_at
            ''', (user_id, alert_id))
            row = cur.fetchone()
    return _dt_to_str(row['triggered_at']) if row else None


def db_delete_portfolio_alert(user_id, alert_id):
    if not init_db():
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM portfolio_alerts WHERE user_id=%s AND id=%s', (user_id, alert_id))
            return cur.rowcount

TOP_MOVERS_CACHE = {'ts': 0, 'data': None}
TOP_MOVERS_TTL = 300

NIFTY50_SYMBOLS = [
    'ADANIENT', 'ADANIPORTS', 'APOLLOHOSP', 'ASIANPAINT', 'AXISBANK',
    'BAJAJ-AUTO', 'BAJFINANCE', 'BAJAJFINSV', 'BEL', 'BHARTIARTL',
    'CIPLA', 'COALINDIA', 'DRREDDY', 'EICHERMOT', 'ETERNAL',
    'GRASIM', 'HCLTECH', 'HDFCBANK', 'HDFCLIFE', 'HEROMOTOCO',
    'HINDALCO', 'HINDUNILVR', 'ICICIBANK', 'INDUSINDBK', 'INFY',
    'ITC', 'JIOFIN', 'JSWSTEEL', 'KOTAKBANK', 'LT',
    'M&M', 'MARUTI', 'NESTLEIND', 'NTPC', 'ONGC',
    'POWERGRID', 'RELIANCE', 'SBILIFE', 'SHRIRAMFIN', 'SBIN',
    'SUNPHARMA', 'TCS', 'TATACONSUM', 'TATAMOTORS', 'TATASTEEL',
    'TECHM', 'TITAN', 'TRENT', 'ULTRACEMCO', 'WIPRO'
]

def load_json(path, default=None):
    if default is None:
        default = {}
    try:
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        return default
    return default

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp_path, path)

def get_request_json():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}

@app.before_request
def validate_persistent_storage_config():
    # On Vercel, a malformed DATABASE_URL should fail loudly as JSON instead of
    # silently falling back to ephemeral /tmp JSON storage.
    if not request.path.startswith('/api/') or request.path in ('/api/health/storage', '/api/health/startup'):
        return None
    if os.environ.get('VERCEL') and RAW_DATABASE_URL and DATABASE_URL_ERROR:
        return jsonify({
            'error': 'Invalid Neon DATABASE_URL configuration',
            'database_url_issue': DATABASE_URL_ERROR,
            'database_url_masked': _mask_database_url(RAW_DATABASE_URL),
            'fix': 'In Vercel Environment Variables, paste the exact Neon pooled connection string. Do not use HOST.neon.tech placeholder text. Redeploy after saving.'
        }), 500
    return None

@app.errorhandler(404)
def json_404(error):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'API endpoint not found', 'path': request.path}), 404
    return send_from_directory('..', 'index.html')

@app.errorhandler(405)
def json_405(error):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Method not allowed', 'path': request.path, 'method': request.method}), 405
    return jsonify({'error': 'Method not allowed'}), 405

@app.errorhandler(Exception)
def json_exception(error):
    if request.path.startswith('/api/'):
        return jsonify({'error': str(error) or 'Server error'}), 500
    raise error

def hash_password(pwd):
    return hashlib.sha256(pwd.encode()).hexdigest()

def get_nse_ticker(symbol):
    return f"{symbol}.NS"

_TICKER_META_CACHE = None

def _load_ticker_meta():
    global _TICKER_META_CACHE
    if _TICKER_META_CACHE is not None:
        return _TICKER_META_CACHE
    meta = {}
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'static', 'tickers.json'))
    try:
        with open(path, encoding='utf-8') as f:
            rows = json.load(f)
        for row in rows if isinstance(rows, list) else []:
            sym = str(row.get('symbol') or '').upper().strip()
            if not sym:
                continue
            clean = _strip_exchange_suffix(sym) if '_strip_exchange_suffix' in globals() else sym.replace('.NS','').replace('.BO','')
            meta[clean] = {
                'symbol': clean,
                'name': row.get('name') or clean,
                'industry': row.get('industry') or row.get('sector') or '',
                'sector': row.get('sector') or ''
            }
    except Exception:
        meta = {}
    _TICKER_META_CACHE = meta
    return meta

def lookup_ticker_meta(symbol):
    sym = _strip_exchange_suffix(symbol).upper().strip() if symbol else ''
    meta = _load_ticker_meta().get(sym)
    if meta:
        return dict(meta)
    compact = ''.join(ch for ch in sym if ch.isalnum())
    for key, val in _load_ticker_meta().items():
        if ''.join(ch for ch in key if ch.isalnum()) == compact:
            return dict(val)
    return {'symbol': sym, 'name': sym, 'industry': '', 'sector': ''}


def _strip_exchange_suffix(symbol):
    """Normalize ticker strings used by URLs, HTML attributes and Yahoo suffixes.

    Important for NSE symbols such as J&KBANK: depending on the browser/proxy,
    the backend may receive J&KBANK, J%26KBANK, J&amp;KBANK, or J&KBANK.NS.
    All of them should resolve to the same app symbol before delete/purge.
    """
    try:
        sym = unquote(str(symbol or ''))
    except Exception:
        sym = str(symbol or '')
    sym = html_lib.unescape(sym).upper().strip()
    return sym.replace('.NS', '').replace('.BO', '')


def _compact_symbol_key(symbol):
    """Comparison key used to purge legacy/encoded duplicate watchlist rows."""
    base = _strip_exchange_suffix(symbol)
    return ''.join(ch for ch in base if ch.isalnum())


def _clean_symbol(symbol):
    """Normalize app/user ticker symbols into NSE Yahoo symbols."""
    sym = str(symbol or '').upper().strip()
    if not sym:
        return sym
    if sym.startswith('^') or sym.endswith('.NS') or sym.endswith('.BO'):
        return sym
    return f"{sym}.NS"

YAHOO_CHART_URLS = [
    'https://query2.finance.yahoo.com/v8/finance/chart/{symbol}',
    'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}',
]
YAHOO_QUOTE_URLS = [
    'https://query2.finance.yahoo.com/v7/finance/quote',
    'https://query1.finance.yahoo.com/v7/finance/quote',
    'https://query2.finance.yahoo.com/v6/finance/quote',
    'https://query1.finance.yahoo.com/v6/finance/quote',
]
YAHOO_HEADERS = {
    # Vercel serverless IPs are often blocked by Yahoo when using the default Python TLS fingerprint.
    # curl_cffi with Chrome impersonation + browser-like headers is much more reliable.
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Origin': 'https://finance.yahoo.com',
    'Referer': 'https://finance.yahoo.com/',
    'Connection': 'keep-alive',
}
YAHOO_TIMEOUT = 12
QUOTE_CACHE = {}
QUOTE_CACHE_TTL = 60
CHART_CACHE = {}
CHART_CACHE_TTL = 120
_LAST_YAHOO_ERROR = None

def _http_get_json(url, params=None):
    """Yahoo GET helper tuned for Vercel. Uses Chrome TLS impersonation if available."""
    global _LAST_YAHOO_ERROR
    errors = []
    for attempt in range(2):
        try:
            if crequests is not None:
                r = crequests.get(url, params=params, headers=YAHOO_HEADERS, timeout=YAHOO_TIMEOUT, impersonate='chrome124')
            else:
                r = requests.get(url, params=params, headers=YAHOO_HEADERS, timeout=YAHOO_TIMEOUT)
            if r.status_code in (401, 403, 429):
                body = (r.text or '')[:180]
                raise RuntimeError(f'Yahoo HTTP {r.status_code}: {body}')
            r.raise_for_status()
            return r.json()
        except Exception as e:
            errors.append(str(e))
            time.sleep(0.25 * (attempt + 1))
    _LAST_YAHOO_ERROR = ' | '.join(errors[-2:])
    raise RuntimeError(_LAST_YAHOO_ERROR)

def _safe_float(v):
    try:
        if v is None or v != v:
            return None
        if hasattr(v, 'item'):
            v = v.item()
        return float(v)
    except Exception:
        return None

def _safe_int(v):
    try:
        f = _safe_float(v)
        return int(f) if f is not None else 0
    except Exception:
        return 0

def _yahoo_chart(symbol, range_='1mo', interval='1d', use_cache=True):
    """Lightweight Yahoo chart fetch that works better on Vercel than yfinance history."""
    yf_symbol = _clean_symbol(symbol)
    key = (yf_symbol, range_, interval)
    now = time.time()
    if use_cache and key in CHART_CACHE and now - CHART_CACHE[key]['ts'] < CHART_CACHE_TTL:
        return CHART_CACHE[key]['data']
    params = {
        'range': range_,
        'interval': interval,
        'includePrePost': 'false',
        'events': 'div,splits',
        'corsDomain': 'finance.yahoo.com'
    }
    last_err = None
    data = None
    for url in YAHOO_CHART_URLS:
        try:
            data = _http_get_json(url.format(symbol=yf_symbol), params=params)
            break
        except Exception as e:
            last_err = e
    if data is None:
        raise last_err or RuntimeError('Yahoo chart fetch failed')
    result = ((data.get('chart') or {}).get('result') or [None])[0]
    if not result:
        raise ValueError(((data.get('chart') or {}).get('error') or {}).get('description') or 'Yahoo chart returned no result')
    meta = result.get('meta') or {}
    timestamps = result.get('timestamp') or []
    quote = (((result.get('indicators') or {}).get('quote') or [{}])[0]) or {}
    rows = []
    for i, ts in enumerate(timestamps):
        close = _safe_float((quote.get('close') or [None])[i] if i < len(quote.get('close') or []) else None)
        if close is None:
            continue
        rows.append({
            'timestamp': int(ts),
            'date': datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d'),
            'datetime': datetime.fromtimestamp(int(ts)).isoformat(),
            'open': _safe_float((quote.get('open') or [None])[i] if i < len(quote.get('open') or []) else None),
            'high': _safe_float((quote.get('high') or [None])[i] if i < len(quote.get('high') or []) else None),
            'low': _safe_float((quote.get('low') or [None])[i] if i < len(quote.get('low') or []) else None),
            'close': close,
            'volume': _safe_int((quote.get('volume') or [None])[i] if i < len(quote.get('volume') or []) else None),
        })
    payload = {'symbol': yf_symbol, 'meta': meta, 'rows': rows}
    if use_cache:
        CHART_CACHE[key] = {'ts': now, 'data': payload}
    return payload

def _yahoo_quotes(symbols, use_cache=True):
    """Batch quote fetch from Yahoo's public quote endpoint."""
    yf_symbols = [_clean_symbol(s) for s in symbols if s]
    key = ','.join(sorted(yf_symbols))
    now = time.time()
    if use_cache and key in QUOTE_CACHE and now - QUOTE_CACHE[key]['ts'] < QUOTE_CACHE_TTL:
        return QUOTE_CACHE[key]['data']
    last_err = None
    raw = None
    for url in YAHOO_QUOTE_URLS:
        try:
            raw = _http_get_json(url, params={'symbols': ','.join(yf_symbols), 'fields': 'regularMarketPrice,regularMarketPreviousClose,regularMarketDayHigh,regularMarketDayLow,regularMarketVolume,regularMarketChangePercent,shortName,longName'})
            break
        except Exception as e:
            last_err = e
    if raw is None:
        raise last_err or RuntimeError('Yahoo quote fetch failed')
    data = ((raw.get('quoteResponse') or {}).get('result') or [])
    by_symbol = {item.get('symbol'): item for item in data if item.get('symbol')}
    if use_cache:
        QUOTE_CACHE[key] = {'ts': now, 'data': by_symbol}
    return by_symbol

def _row_pct(rows, lookback_rows):
    try:
        if not rows:
            return 0
        ltp = _safe_float(rows[-1].get('close'))
        idx = max(0, len(rows) - 1 - lookback_rows)
        base = _safe_float(rows[idx].get('close'))
        return round(((ltp - base) / base) * 100, 2) if ltp is not None and base else 0
    except Exception:
        return 0

def fetch_quote(symbol):
    """Fetch a quote using Yahoo's direct JSON endpoint first; fallback to yfinance locally."""
    sym = str(symbol or '').upper().replace('.NS', '').replace('.BO', '').strip()
    if not sym:
        return None
    try:
        # Batch quote endpoint is fast and more reliable on Vercel serverless.
        quotes = _yahoo_quotes([sym], use_cache=False)
        item = quotes.get(_clean_symbol(sym))
        if item:
            ltp = _safe_float(item.get('regularMarketPrice'))
            prev_close = _safe_float(item.get('regularMarketPreviousClose'))
            day_high = _safe_float(item.get('regularMarketDayHigh'))
            day_low = _safe_float(item.get('regularMarketDayLow'))
            volume = _safe_int(item.get('regularMarketVolume'))
            chg_pct = _safe_float(item.get('regularMarketChangePercent'))
            if ltp is not None:
                return {
                    'symbol': sym,
                    'ltp': round(ltp, 2),
                    'prev_close': round(prev_close if prev_close is not None else ltp, 2),
                    'day_high': round(day_high if day_high is not None else ltp, 2),
                    'day_low': round(day_low if day_low is not None else ltp, 2),
                    'volume': volume,
                    'day_chg_pct': round(chg_pct if chg_pct is not None else (((ltp - prev_close) / prev_close * 100) if prev_close else 0), 2),
                }
        # Chart fallback when quote endpoint omits a symbol.
        chart = _yahoo_chart(sym, '5d', '1d', use_cache=False)
        rows = chart.get('rows', [])
        if rows:
            latest = rows[-1]
            prev = rows[-2]['close'] if len(rows) >= 2 else (chart.get('meta') or {}).get('previousClose') or latest['close']
            ltp = _safe_float(latest.get('close'))
            prev_close = _safe_float(prev)
            return {
                'symbol': sym,
                'ltp': round(ltp, 2),
                'prev_close': round(prev_close if prev_close is not None else ltp, 2),
                'day_high': round(_safe_float(latest.get('high')) or ltp, 2),
                'day_low': round(_safe_float(latest.get('low')) or ltp, 2),
                'volume': _safe_int(latest.get('volume')),
                'day_chg_pct': round(((ltp - prev_close) / prev_close * 100), 2) if prev_close else 0,
            }
    except Exception:
        pass
    try:
        ticker = yf.Ticker(get_nse_ticker(sym))
        hist = ticker.history(period='2d', timeout=YAHOO_TIMEOUT)
        if hist.empty:
            return None
        latest = hist.iloc[-1]
        ltp = float(latest['Close'])
        prev_close = float(hist['Close'].iloc[-2]) if len(hist) >= 2 else ltp
        day_chg_pct = ((ltp - prev_close) / prev_close * 100) if prev_close else 0
        return {
            'symbol': sym,
            'ltp': round(ltp, 2),
            'prev_close': round(prev_close, 2),
            'day_high': round(float(latest['High']), 2),
            'day_low': round(float(latest['Low']), 2),
            'volume': int(latest['Volume']) if latest.get('Volume') is not None else 0,
            'day_chg_pct': round(day_chg_pct, 2),
        }
    except Exception:
        return None




def fetch_quotes_batch(symbols):
    """Batch quote helper for Portfolio screens.

    The old holdings endpoint called fetch_quote() one row at a time, which made
    Add/Edit/Sell/Delete feel slow because the frontend reloads the whole table
    after each action. This helper performs one yfinance batch lookup and returns
    app-symbol keyed quote dicts. Individual fetch_quote() remains as fallback.
    """
    clean_symbols = []
    seen = set()
    for sym in symbols or []:
        clean = _plain_symbol(sym)
        if clean and clean not in seen:
            clean_symbols.append(clean)
            seen.add(clean)
    out = {}
    if not clean_symbols:
        return out
    try:
        qmap = _yahoo_quotes(clean_symbols, use_cache=True)
        for clean in clean_symbols:
            item = qmap.get(_yf_symbol(clean)) or qmap.get(clean)
            if not item:
                continue
            ltp = _safe_float(item.get('regularMarketPrice'))
            prev = _safe_float(item.get('regularMarketPreviousClose')) or ltp
            if ltp is None:
                continue
            chg_pct = _safe_float(item.get('regularMarketChangePercent'))
            if chg_pct is None:
                chg_pct = ((ltp - prev) / prev * 100) if prev else 0
            out[clean] = {
                'symbol': clean,
                'ltp': round(ltp, 2),
                'prev_close': round(prev if prev is not None else ltp, 2),
                'day_high': round(_safe_float(item.get('regularMarketDayHigh')) or ltp, 2),
                'day_low': round(_safe_float(item.get('regularMarketDayLow')) or ltp, 2),
                'volume': _safe_int(item.get('regularMarketVolume')),
                'day_chg_pct': round(chg_pct, 2),
            }
    except Exception:
        out = {}
    return out

def fetch_return_profile(symbol, quote=None):
    """Return percentage changes for 1D, 1W, 1M and 1Y for a symbol."""
    returns = {'ret_1d': 0, 'ret_1w': 0, 'ret_1m': 0, 'ret_1y': 0}
    try:
        rows = _yahoo_chart(symbol, '1y', '1d').get('rows', [])
        if not rows:
            raise ValueError('empty yahoo chart')
        returns['ret_1d'] = round(float(quote.get('day_chg_pct', 0)), 2) if quote else _row_pct(rows, 1)
        returns['ret_1w'] = _row_pct(rows, 5)
        returns['ret_1m'] = _row_pct(rows, 22)
        returns['ret_1y'] = _row_pct(rows, min(252, max(1, len(rows) - 1)))
        return returns
    except Exception:
        pass
    try:
        ticker = yf.Ticker(get_nse_ticker(symbol))
        hist = ticker.history(period='1y', timeout=YAHOO_TIMEOUT)
        if hist.empty:
            if quote:
                returns['ret_1d'] = round(float(quote.get('day_chg_pct', 0)), 2)
            return returns
        ltp = float(hist['Close'].iloc[-1])
        def pct_from_rows(rows_back):
            idx = max(0, len(hist) - 1 - rows_back)
            base = float(hist['Close'].iloc[idx])
            return round(((ltp - base) / base) * 100, 2) if base else 0
        returns['ret_1d'] = round(float(quote.get('day_chg_pct', 0)), 2) if quote else pct_from_rows(1)
        returns['ret_1w'] = pct_from_rows(5)
        returns['ret_1m'] = pct_from_rows(22)
        returns['ret_1y'] = pct_from_rows(min(252, max(1, len(hist) - 1)))
        return returns
    except Exception:
        if quote:
            returns['ret_1d'] = round(float(quote.get('day_chg_pct', 0)), 2)
        return returns



# ─── VERCEL YFINANCE ACCESS LAYER ───────────────────────────────────────────
# The reference Vercel app uses yfinance directly from serverless functions with:
#   - yf.Tickers(...) for quote/info access
#   - yf.download(...) for batch historical returns
#   - short in-memory TTL caches to avoid repeated Yahoo calls
# This layer mirrors that approach and avoids direct Yahoo JSON endpoints which can
# be blocked from Vercel IPs.
YF_CACHE = {}
YF_QUOTE_TTL = 60
YF_HISTORY_TTL = 180


def _cache_get_yf(key, ttl):
    item = YF_CACHE.get(key)
    if item and (time.time() - item['ts'] < ttl):
        return item['data']
    return None


def _cache_set_yf(key, data):
    YF_CACHE[key] = {'ts': time.time(), 'data': data}
    return data


def _yf_symbol(symbol):
    sym = str(symbol or '').upper().strip()
    if not sym:
        return sym
    if sym.startswith('^') or sym.endswith('.NS') or sym.endswith('.BO'):
        return sym
    return f'{sym}.NS'


def _plain_symbol(symbol):
    # Robust plain app symbol used for Yahoo and matching. Handles imported broker
    # formats such as NSE:RELIANCE, RELIANCE-EQ, RELIANCE.NS and encoded symbols.
    try:
        sym = _strip_exchange_suffix(symbol)
    except Exception:
        sym = str(symbol or '').upper().replace('.NS', '').replace('.BO', '').strip()
    for prefix in ('NSE:', 'BSE:', 'NSE_', 'BSE_'):
        if sym.startswith(prefix):
            sym = sym[len(prefix):]
    for suffix in ('-EQ', ' EQ'):
        if sym.endswith(suffix):
            sym = sym[:-len(suffix)]
    return sym.strip()


def _yf_safe_val(val, default=None):
    try:
        if val is None:
            return default
        if hasattr(val, 'item'):
            val = val.item()
        if isinstance(val, float) and (np.isnan(val) or np.isinf(val)):
            return default
        return val
    except Exception:
        return default


def _yf_history(symbol, period='1mo', interval='1d', auto_adjust=False, use_cache=True):
    yf_sym = _yf_symbol(symbol)
    key = ('yf_hist', yf_sym, period, interval, auto_adjust)
    if use_cache:
        cached = _cache_get_yf(key, YF_HISTORY_TTL)
        if cached is not None:
            return cached.copy()
    try:
        hist = yf.Ticker(yf_sym).history(
            period=period,
            interval=interval,
            auto_adjust=auto_adjust,
            prepost=False,
            actions=False,
            timeout=12
        )
    except TypeError:
        hist = yf.Ticker(yf_sym).history(
            period=period,
            interval=interval,
            auto_adjust=auto_adjust,
            prepost=False,
            actions=False
        )
    if hist is None:
        hist = pd.DataFrame()
    hist = hist.dropna(how='all')
    return _cache_set_yf(key, hist.copy()) if use_cache else hist


def _yahoo_chart(symbol, range_='1mo', interval='1d', use_cache=True):
    """Compatibility wrapper used by existing routes, backed by yfinance history."""
    yf_sym = _yf_symbol(symbol)
    hist = _yf_history(yf_sym, period=range_, interval=interval, auto_adjust=False, use_cache=use_cache)
    rows = []
    if hist is not None and not hist.empty:
        for idx, row in hist.iterrows():
            close = _safe_float(row.get('Close'))
            if close is None:
                continue
            try:
                ts = int(idx.timestamp())
                dt_iso = idx.isoformat()
                dt_str = idx.strftime('%Y-%m-%d')
            except Exception:
                ts = int(time.time())
                dt_iso = str(idx)
                dt_str = str(idx)[:10]
            rows.append({
                'timestamp': ts,
                'date': dt_str,
                'datetime': dt_iso,
                'open': _safe_float(row.get('Open')),
                'high': _safe_float(row.get('High')),
                'low': _safe_float(row.get('Low')),
                'close': close,
                'volume': _safe_int(row.get('Volume')),
            })
    meta = {}
    if rows:
        meta = {
            'symbol': yf_sym,
            'regularMarketPrice': rows[-1]['close'],
            'previousClose': rows[-2]['close'] if len(rows) >= 2 else rows[-1]['close']
        }
    return {'symbol': yf_sym, 'meta': meta, 'rows': rows}


def _yahoo_quotes(symbols, use_cache=True):
    """Compatibility wrapper that returns Yahoo-like quote dicts, backed by yfinance."""
    yf_symbols = [_yf_symbol(s) for s in symbols if s]
    key = ('yf_quotes', ','.join(sorted(yf_symbols)))
    if use_cache:
        cached = _cache_get_yf(key, YF_QUOTE_TTL)
        if cached is not None:
            return cached
    out = {}
    if not yf_symbols:
        return out
    try:
        tickers_obj = yf.Tickers(' '.join(yf_symbols))
        for yf_sym in yf_symbols:
            try:
                t = tickers_obj.tickers.get(yf_sym) or yf.Ticker(yf_sym)
                fast = getattr(t, 'fast_info', {})
                try:
                    info = t.info or {}
                except Exception:
                    info = {}

                def fi(attr):
                    try:
                        if hasattr(fast, attr):
                            return getattr(fast, attr)
                        return fast.get(attr)
                    except Exception:
                        return None

                price = _safe_float(fi('last_price') or info.get('currentPrice') or info.get('regularMarketPrice'))
                prev = _safe_float(fi('previous_close') or info.get('previousClose') or info.get('regularMarketPreviousClose'))
                day_high = _safe_float(fi('day_high') or info.get('dayHigh') or info.get('regularMarketDayHigh'))
                day_low = _safe_float(fi('day_low') or info.get('dayLow') or info.get('regularMarketDayLow'))
                volume = _safe_int(info.get('regularMarketVolume') or info.get('volume') or fi('last_volume'))

                if price is None or prev is None:
                    # fast_info sometimes fails on Vercel for a few symbols; history gives a robust fallback.
                    hist = _yf_history(yf_sym, period='5d', interval='1d', auto_adjust=False, use_cache=use_cache)
                    if hist is not None and not hist.empty:
                        latest = hist.iloc[-1]
                        price = price if price is not None else _safe_float(latest.get('Close'))
                        prev = prev if prev is not None else (_safe_float(hist['Close'].iloc[-2]) if len(hist) >= 2 else price)
                        day_high = day_high if day_high is not None else _safe_float(latest.get('High'))
                        day_low = day_low if day_low is not None else _safe_float(latest.get('Low'))
                        volume = volume or _safe_int(latest.get('Volume'))

                if price is not None:
                    chg_pct = ((price - prev) / prev * 100) if prev else 0
                    out[yf_sym] = {
                        'symbol': yf_sym,
                        'shortName': info.get('shortName') or info.get('longName') or _plain_symbol(yf_sym),
                        'longName': info.get('longName') or info.get('shortName') or _plain_symbol(yf_sym),
                        'regularMarketPrice': price,
                        'regularMarketPreviousClose': prev if prev is not None else price,
                        'regularMarketDayHigh': day_high if day_high is not None else price,
                        'regularMarketDayLow': day_low if day_low is not None else price,
                        'regularMarketVolume': volume,
                        'regularMarketChangePercent': chg_pct,
                    }
            except Exception:
                continue
    except Exception:
        pass
    return _cache_set_yf(key, out) if use_cache else out


def fetch_quote(symbol):
    """Fetch NSE/BSE quote via yfinance, matching the uploaded Vercel reference app method."""
    sym = _plain_symbol(symbol)
    if not sym:
        return None
    try:
        qmap = _yahoo_quotes([sym], use_cache=True)
        item = qmap.get(_yf_symbol(sym))
        if not item:
            return None
        ltp = _safe_float(item.get('regularMarketPrice'))
        prev = _safe_float(item.get('regularMarketPreviousClose')) or ltp
        if ltp is None:
            return None
        chg_pct = _safe_float(item.get('regularMarketChangePercent'))
        if chg_pct is None:
            chg_pct = ((ltp - prev) / prev * 100) if prev else 0
        return {
            'symbol': sym,
            'ltp': round(ltp, 2),
            'prev_close': round(prev if prev is not None else ltp, 2),
            'day_high': round(_safe_float(item.get('regularMarketDayHigh')) or ltp, 2),
            'day_low': round(_safe_float(item.get('regularMarketDayLow')) or ltp, 2),
            'volume': _safe_int(item.get('regularMarketVolume')),
            'day_chg_pct': round(chg_pct, 2),
        }
    except Exception:
        return None


def fetch_return_profile(symbol, quote=None):
    returns = {'ret_1d': 0, 'ret_1w': 0, 'ret_1m': 0, 'ret_1y': 0}
    try:
        hist = _yf_history(symbol, period='1y', interval='1d', auto_adjust=True, use_cache=True)
        if hist is None or hist.empty or len(hist) < 2:
            if quote:
                returns['ret_1d'] = round(float(quote.get('day_chg_pct', 0)), 2)
            return returns
        closes = hist['Close'].dropna()
        if closes.empty:
            return returns
        ltp = float(closes.iloc[-1])

        def pct_return(n_periods):
            if len(closes) > n_periods:
                base = float(closes.iloc[-(n_periods + 1)])
                return round((ltp - base) / base * 100, 2) if base else 0
            return 0

        returns['ret_1d'] = round(float(quote.get('day_chg_pct', 0)), 2) if quote else pct_return(1)
        returns['ret_1w'] = pct_return(5)
        returns['ret_1m'] = pct_return(21)
        returns['ret_1y'] = pct_return(252) if len(closes) >= 252 else round((ltp - float(closes.iloc[0])) / float(closes.iloc[0]) * 100, 2)
        return returns
    except Exception:
        if quote:
            returns['ret_1d'] = round(float(quote.get('day_chg_pct', 0)), 2)
        return returns

# Serve index.html
@app.route('/')
def index():
    return send_from_directory('..', 'index.html')

@app.route('/api/health/storage')
def health_storage():
    db_ok = False
    db_error = None
    if db_configured():
        db_ok = init_db()
        db_error = DB_LAST_ERROR
        if db_ok:
            try:
                with db_connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute('SELECT COUNT(*) AS user_count FROM users')
                        user_count = cur.fetchone().get('user_count', 0)
                return jsonify({
                    'ok': True,
                    'storage': 'neon',
                    **db_status_payload(),
                    'tables_ready': True,
                    'user_count': user_count,
                    'vercel': bool(os.environ.get('VERCEL'))
                })
            except Exception as e:
                db_error = str(e)

    test_file = os.path.join(DATA_DIR, '_write_test.json')
    writable = False
    err = None
    try:
        save_json(test_file, {'ok': True, 'ts': datetime.now().isoformat()})
        writable = True
    except Exception as e:
        err = str(e)
    payload = {
        'ok': False if RAW_DATABASE_URL else (writable and not db_configured()),
        'storage': 'json-fallback' if writable else 'unavailable',
        **db_status_payload(),
        'tables_ready': db_ok,
        'database_error': db_error or DATABASE_URL_ERROR,
        'data_dir': DATA_DIR,
        'vercel': bool(os.environ.get('VERCEL')),
        'writable': writable,
        'error': err
    }
    if DATABASE_URL_ERROR:
        payload['fix'] = 'Replace the placeholder DATABASE_URL in Vercel with the exact Neon pooled connection string and redeploy.'
        payload['example_shape'] = 'postgresql://USER:PASSWORD@ep-xxxxx-pooler.REGION.aws.neon.tech/DBNAME?sslmode=require'
    return jsonify(payload)


@app.route('/api/health/startup')
def health_startup():
    return jsonify({
        'ok': True,
        'app': 'ApexWealth',
        'weighted_index_rows': {
            'nifty': len(nifty50_data or []),
            'banknifty': len(banknifty_data or []),
            'sensex': len(sensex_data or [])
        },
        'weighted_index_data_error': WEIGHTED_INDEX_DATA_ERROR,
        'vercel': bool(os.environ.get('VERCEL')),
        **db_status_payload()
    })

# ─── AUTH ─────────────────────────────────────────────────────────────────────

@app.route('/api/signup', methods=['POST'])
def signup():
    data = get_request_json()
    email = data.get('email', '').lower().strip()
    password = data.get('password', '')
    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400

    if db_configured():
        if db_get_user(email):
            return jsonify({'error': 'Email already registered'}), 409
        user = db_create_user(email, hash_password(password))
        return jsonify({'message': 'Your account is currently PENDING admin approval', 'user_id': user['id'], 'email': email, 'status': 'PENDING', 'storage': 'neon'})

    users = load_json(USERS_FILE)
    if email in users:
        return jsonify({'error': 'Email already registered'}), 409
    user_id = str(uuid.uuid4())
    users[email] = {'id': user_id, 'email': email, 'password': hash_password(password), 'created': str(datetime.now()), 'last_login': None, 'status': 'PENDING', 'is_enabled': True}
    save_json(USERS_FILE, users)
    return jsonify({'message': 'Your account is currently PENDING admin approval', 'user_id': user_id, 'email': email, 'status': 'PENDING', 'storage': 'json-fallback'})

@app.route('/api/login', methods=['POST'])
def login():
    data = get_request_json()
    email = data.get('email', '').lower().strip()
    password = data.get('password', '')

    if db_configured():
        user = db_get_user(email)
        if not user or user['password'] != hash_password(password):
            return jsonify({'error': 'Invalid credentials'}), 401
        if not bool(user.get('is_enabled', True)):
            return jsonify({'error': 'User account is disabled. Contact admin.'}), 403
        if str(user.get('status') or 'APPROVED').upper() != 'APPROVED':
            return jsonify({'error': 'Account is pending admin approval'}), 403
        last_login = db_update_last_login(user['id'])
        return jsonify({'message': 'Login successful', 'user_id': user['id'], 'email': email, 'last_login': last_login, 'storage': 'neon'})

    users = load_json(USERS_FILE)
    user = users.get(email)
    if not user or user['password'] != hash_password(password):
        return jsonify({'error': 'Invalid credentials'}), 401
    if not bool(user.get('is_enabled', True)):
        return jsonify({'error': 'User account is disabled. Contact admin.'}), 403
    if str(user.get('status') or 'APPROVED').upper() != 'APPROVED':
        return jsonify({'error': 'Account is pending admin approval'}), 403
    user['last_login'] = str(datetime.now())
    users[email] = user
    save_json(USERS_FILE, users)
    return jsonify({'message': 'Login successful', 'user_id': user['id'], 'email': email, 'last_login': user.get('last_login'), 'storage': 'json-fallback'})

@app.route('/api/profile/<user_id>', methods=['GET'])
def get_profile(user_id):
    if db_configured():
        user = db_get_user_by_id(user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        return jsonify({
            'user_id': user.get('id'),
            'email': user.get('email'),
            'password': '••••••••',
            'created': user.get('created'),
            'last_login': user.get('last_login'),
            'status': str(user.get('status') or 'APPROVED').upper(),
            'is_enabled': bool(user.get('is_enabled', True)),
            'storage': 'neon'
        })
    users = load_json(USERS_FILE)
    user = next((u for u in users.values() if str(u.get('id')) == str(user_id)), None)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    return jsonify({
        'user_id': user.get('id'),
        'email': user.get('email'),
        'password': '••••••••',
        'created': user.get('created'),
        'last_login': user.get('last_login'),
        'status': str(user.get('status') or 'APPROVED').upper(),
        'is_enabled': bool(user.get('is_enabled', True)),
        'storage': 'json-fallback'
    })

@app.route('/api/profile/<user_id>/password', methods=['POST'])
def update_profile_password(user_id):
    data = get_request_json()
    new_pwd = str(data.get('password', '')).strip()
    if len(new_pwd) < 4:
        return jsonify({'error': 'Password must be at least 4 characters'}), 400
    if db_configured():
        if not db_get_user_by_id(user_id):
            return jsonify({'error': 'User not found'}), 404
        db_update_password_by_id(user_id, hash_password(new_pwd))
        return jsonify({'message': 'Password updated', 'storage': 'neon'})
    users = load_json(USERS_FILE)
    for email, user in users.items():
        if str(user.get('id')) == str(user_id):
            user['password'] = hash_password(new_pwd)
            users[email] = user
            save_json(USERS_FILE, users)
            return jsonify({'message': 'Password updated', 'storage': 'json-fallback'})
    return jsonify({'error': 'User not found'}), 404

@app.route('/api/change-password', methods=['POST'])
def change_password():
    data = get_request_json()
    email = data.get('email', '').lower().strip()
    old_pwd = data.get('old_password', '')
    new_pwd = data.get('new_password', '')

    if db_configured():
        user = db_get_user(email)
        if not user or user['password'] != hash_password(old_pwd):
            return jsonify({'error': 'Invalid credentials'}), 401
        db_update_password(email, hash_password(new_pwd))
        return jsonify({'message': 'Password changed', 'storage': 'neon'})

    users = load_json(USERS_FILE)
    user = users.get(email)
    if not user or user['password'] != hash_password(old_pwd):
        return jsonify({'error': 'Invalid credentials'}), 401
    users[email]['password'] = hash_password(new_pwd)
    save_json(USERS_FILE, users)
    return jsonify({'message': 'Password changed', 'storage': 'json-fallback'})

# ─── PORTFOLIO ────────────────────────────────────────────────────────────────

@app.route('/api/holdings/<user_id>', methods=['GET'])
def get_holdings(user_id):
    holdings = db_get_holdings(user_id) if db_configured() else None
    if holdings is None:
        portfolios = load_json(PORTFOLIOS_FILE)
        holdings = portfolios.get(user_id, [])
    enriched = []
    quote_map = fetch_quotes_batch([h.get('symbol') for h in holdings])
    for h in holdings:
        clean_symbol = _plain_symbol(h.get('symbol'))
        q = quote_map.get(clean_symbol) or fetch_quote(h.get('symbol'))
        invested = float(h.get('buy_price', 0)) * float(h.get('qty', 0))
        if q:
            ltp = q['ltp']
            curr_val = ltp * float(h.get('qty', 0))
            pnl = curr_val - invested
            pnl_pct = (pnl / invested * 100) if invested else 0
            enriched.append({**h, 'symbol': clean_symbol or h.get('symbol'), 'ltp': ltp, 'quote_ok': True, 'day_chg_pct': q.get('day_chg_pct', 0),
                              'invested': round(invested, 2), 'curr_value': round(curr_val, 2),
                              'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct, 2)})
        else:
            # Do not display buy price as LTP when Yahoo has no quote. Showing the
            # buy price made imported rows look falsely live. Keep totals conservative.
            enriched.append({**h, 'symbol': clean_symbol or h.get('symbol'), 'ltp': None, 'quote_ok': False, 'day_chg_pct': 0,
                              'invested': round(invested, 2), 'curr_value': round(invested, 2),
                              'pnl': 0, 'pnl_pct': 0})
    return jsonify(enriched)

@app.route('/api/holdings/<user_id>', methods=['POST'])
def add_holding(user_id):
    data = get_request_json()
    holding = {
        'id': str(uuid.uuid4()),
        'symbol': _plain_symbol(data['symbol']),
        'name': data.get('name', data['symbol']),
        'buy_price': float(data['buy_price']),
        'qty': float(data['qty']),
        'date': data.get('date', str(date.today())),
        'industry': data.get('industry', data.get('sector', '')),
        'sector': ''
    }
    if db_configured():
        db_add_holding(user_id, holding)
    else:
        portfolios = load_json(PORTFOLIOS_FILE)
        if user_id not in portfolios:
            portfolios[user_id] = []
        portfolios[user_id].append(holding)
        save_json(PORTFOLIOS_FILE, portfolios)
    return jsonify({'message': 'Holding added', 'holding': holding, 'storage': 'neon' if db_configured() else 'json-fallback'})

@app.route('/api/holdings/<user_id>/<holding_id>', methods=['PUT'])
def edit_holding(user_id, holding_id):
    data = get_request_json()
    if db_configured():
        updated = db_update_holding(user_id, holding_id, data.get('buy_price'), data.get('qty'), data.get('date'))
        if updated:
            return jsonify({'message': 'Updated', 'storage': 'neon'})
        return jsonify({'error': 'Not found'}), 404

    portfolios = load_json(PORTFOLIOS_FILE)
    holdings = portfolios.get(user_id, [])
    for i, h in enumerate(holdings):
        if h['id'] == holding_id:
            holdings[i] = {**h, 'buy_price': float(data.get('buy_price', h['buy_price'])),
                           'qty': float(data.get('qty', h['qty'])), 'date': data.get('date', h['date'])}
            save_json(PORTFOLIOS_FILE, portfolios)
            return jsonify({'message': 'Updated', 'storage': 'json-fallback'})
    return jsonify({'error': 'Not found'}), 404

def _delete_holding_record(user_id, holding_id):
    if db_configured():
        deleted = db_delete_holding(user_id, holding_id)
        return int(deleted or 0)
    portfolios = load_json(PORTFOLIOS_FILE)
    holdings = portfolios.get(user_id, [])
    before = len(holdings)
    portfolios[user_id] = [h for h in holdings if str(h.get('id')) != str(holding_id)]
    deleted = before - len(portfolios[user_id])
    save_json(PORTFOLIOS_FILE, portfolios)
    return deleted

@app.route('/api/holdings/<user_id>/<holding_id>', methods=['DELETE'])
def delete_holding(user_id, holding_id):
    deleted = _delete_holding_record(user_id, holding_id)
    return jsonify({'message': 'Deleted', 'deleted': deleted})

@app.route('/api/holdings/<user_id>/<holding_id>/delete', methods=['POST'])
def delete_holding_post(user_id, holding_id):
    deleted = _delete_holding_record(user_id, holding_id)
    return jsonify({'message': 'Deleted', 'deleted': deleted})

@app.route('/api/sell/<user_id>/<holding_id>', methods=['POST'])
def sell_holding(user_id, holding_id):
    data = get_request_json()
    sell_price = float(data.get('sell_price', 0))

    if db_configured():
        holding = db_get_holding(user_id, holding_id)
    else:
        portfolios = load_json(PORTFOLIOS_FILE)
        holdings = portfolios.get(user_id, [])
        holding = next((h for h in holdings if h['id'] == holding_id), None)
    if not holding:
        return jsonify({'error': 'Not found'}), 404

    available_qty = float(holding.get('qty', 0))
    sell_qty = float(data.get('qty', available_qty))
    if sell_price <= 0:
        return jsonify({'error': 'Enter a valid sell price'}), 400
    if sell_qty <= 0:
        return jsonify({'error': 'Enter a valid sell quantity'}), 400
    if sell_qty > available_qty:
        return jsonify({'error': f'Sell quantity cannot exceed available quantity ({available_qty:g})'}), 400

    invested_for_sold_qty = holding['buy_price'] * sell_qty
    pnl = (sell_price - holding['buy_price']) * sell_qty
    trade = {
        'id': str(uuid.uuid4()),
        'symbol': holding['symbol'],
        'name': holding.get('name', holding['symbol']),
        'buy_price': holding['buy_price'],
        'sell_price': sell_price,
        'qty': sell_qty,
        'buy_date': holding['date'],
        'sell_date': str(date.today()),
        'pnl': round(pnl, 2),
        'pnl_pct': round((pnl / invested_for_sold_qty) * 100, 2) if invested_for_sold_qty else 0
    }

    remaining_qty = max(0, round(available_qty - sell_qty, 6))
    if db_configured():
        db_add_trade(user_id, trade)
        db_set_holding_qty_or_delete(user_id, holding_id, remaining_qty)
    else:
        trades = load_json(TRADES_FILE)
        if user_id not in trades:
            trades[user_id] = []
        trades[user_id].append(trade)
        save_json(TRADES_FILE, trades)
        portfolios = load_json(PORTFOLIOS_FILE)
        holdings = portfolios.get(user_id, [])
        if sell_qty == available_qty:
            portfolios[user_id] = [h for h in holdings if h['id'] != holding_id]
        else:
            for h in holdings:
                if h['id'] == holding_id:
                    h['qty'] = remaining_qty
                    break
            portfolios[user_id] = holdings
        save_json(PORTFOLIOS_FILE, portfolios)
    return jsonify({'message': 'Sold', 'trade': trade, 'remaining_qty': remaining_qty, 'storage': 'neon' if db_configured() else 'json-fallback'})


# ─── ZERODHA EXECUTION AND SYNC LAYER ────────────────────────────────────────

def _kite_credentials_or_400(user_id=None):
    creds = db_get_zerodha_credentials(user_id) if user_id else None
    if not creds and ZERODHA_API_KEY and ZERODHA_API_SECRET:
        creds = {'api_key': ZERODHA_API_KEY, 'api_secret': ZERODHA_API_SECRET, 'source': 'env'}
    if not creds or not creds.get('api_key') or not creds.get('api_secret'):
        return None, ({'error': 'Zerodha API credentials are not configured. Open Settings → API Settings and save your Kite API Key and API Secret.'}, 400)
    return {'api_key': creds.get('api_key'), 'api_secret': creds.get('api_secret'), 'source': creds.get('source', 'user')}, None


def _zerodha_safe_session(user_id):
    session = db_get_zerodha_session(user_id)
    if not session or not session.get('access_token'):
        return None
    return session


def _apply_confirmed_buy(user_id, symbol, avg_price, qty, order_date=None):
    meta = lookup_ticker_meta(symbol)
    holding = {
        'id': str(uuid.uuid4()),
        'symbol': _plain_symbol(symbol),
        'name': meta.get('name') or _plain_symbol(symbol),
        'buy_price': round(float(avg_price), 4),
        'qty': float(qty),
        'date': order_date or str(date.today()),
        'industry': meta.get('industry', ''),
        'sector': meta.get('sector', '')
    }
    if db_configured():
        db_add_holding(user_id, holding)
    else:
        portfolios = load_json(PORTFOLIOS_FILE)
        portfolios.setdefault(user_id, []).append(holding)
        save_json(PORTFOLIOS_FILE, portfolios)
    return holding


def _apply_confirmed_sell(user_id, holding_id, sell_price, sell_qty):
    if db_configured():
        holding = db_get_holding(user_id, holding_id)
    else:
        portfolios = load_json(PORTFOLIOS_FILE)
        holding = next((h for h in portfolios.get(user_id, []) if str(h.get('id')) == str(holding_id)), None)
    if not holding:
        raise ValueError('Apex holding not found for confirmed sell update')
    available_qty = float(holding.get('qty') or 0)
    sell_qty = float(sell_qty or 0)
    sell_price = float(sell_price or 0)
    if sell_qty <= 0 or sell_price <= 0:
        raise ValueError('Confirmed sell returned invalid quantity/price')
    if sell_qty > available_qty:
        raise ValueError(f'Confirmed sell quantity {sell_qty:g} exceeds Apex holding quantity {available_qty:g}')
    invested_for_sold_qty = float(holding['buy_price']) * sell_qty
    pnl = (sell_price - float(holding['buy_price'])) * sell_qty
    trade = {
        'id': str(uuid.uuid4()),
        'symbol': holding['symbol'],
        'name': holding.get('name', holding['symbol']),
        'buy_price': float(holding['buy_price']),
        'sell_price': round(sell_price, 4),
        'qty': sell_qty,
        'buy_date': holding.get('date'),
        'sell_date': str(date.today()),
        'pnl': round(pnl, 2),
        'pnl_pct': round((pnl / invested_for_sold_qty) * 100, 2) if invested_for_sold_qty else 0
    }
    remaining_qty = max(0, round(available_qty - sell_qty, 6))
    if db_configured():
        db_add_trade(user_id, trade)
        db_set_holding_qty_or_delete(user_id, holding_id, remaining_qty)
    else:
        trades = load_json(TRADES_FILE)
        trades.setdefault(user_id, []).append(trade)
        save_json(TRADES_FILE, trades)
        portfolios = load_json(PORTFOLIOS_FILE)
        holdings = portfolios.get(user_id, [])
        if remaining_qty <= 0:
            portfolios[user_id] = [h for h in holdings if str(h.get('id')) != str(holding_id)]
        else:
            for h in holdings:
                if str(h.get('id')) == str(holding_id):
                    h['qty'] = remaining_qty
                    break
            portfolios[user_id] = holdings
        save_json(PORTFOLIOS_FILE, portfolios)
    return trade, remaining_qty


@app.route('/api/zerodha/config', methods=['GET'])
def zerodha_config():
    return jsonify({'server_configured': bool(ZERODHA_API_KEY and ZERODHA_API_SECRET), 'api_key': ZERODHA_API_KEY if ZERODHA_API_KEY else None})


@app.route('/api/zerodha/credentials/<user_id>', methods=['GET'])
def zerodha_credentials_get(user_id):
    creds = db_get_zerodha_credentials(user_id)
    user_saved = bool(creds and creds.get('source') != 'env')
    return jsonify({
        'configured': bool(creds and creds.get('api_key') and creds.get('api_secret')),
        'user_saved': user_saved,
        'source': creds.get('source', 'user') if creds else None,
        'api_key': creds.get('api_key') if creds else '',
        'api_secret_masked': _mask_secret(creds.get('api_secret')) if creds else '',
        'updated_at': creds.get('updated_at') if creds else None,
    })


@app.route('/api/zerodha/credentials/<user_id>', methods=['POST'])
def zerodha_credentials_save(user_id):
    data = get_request_json()
    api_key = str(data.get('api_key') or '').strip()
    api_secret = str(data.get('api_secret') or '').strip()
    if not api_key or not api_secret:
        return jsonify({'error': 'Enter both Zerodha API Key and API Secret.'}), 400
    try:
        db_save_zerodha_credentials(user_id, api_key, api_secret)
        db_delete_zerodha_session(user_id)
        return jsonify({'message': 'Zerodha API settings saved. Reconnect Zerodha to generate a fresh access token.', 'configured': True, 'api_key': api_key, 'api_secret_masked': _mask_secret(api_secret)})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/zerodha/credentials/<user_id>/delete', methods=['POST'])
def zerodha_credentials_delete(user_id):
    removed = db_delete_zerodha_credentials(user_id)
    return jsonify({'message': 'Zerodha API settings removed', 'removed': int(removed or 0)})


@app.route('/api/zerodha/status/<user_id>', methods=['GET'])
def zerodha_status(user_id):
    session = _zerodha_safe_session(user_id)
    creds = db_get_zerodha_credentials(user_id)
    return jsonify({
        'configured': bool(creds and creds.get('api_key') and creds.get('api_secret')),
        'credentials_source': creds.get('source', 'user') if creds else None,
        'api_key': creds.get('api_key') if creds else None,
        'connected': bool(session),
        'kite_user_id': session.get('kite_user_id') if session else None,
        'user_name': session.get('user_name') if session else None,
        'updated_at': session.get('updated_at') if session else None,
    })


@app.route('/api/zerodha/login-url/<user_id>', methods=['GET'])
def zerodha_login_url_api(user_id):
    creds, err = _kite_credentials_or_400(user_id)
    if err:
        return jsonify(err[0]), err[1]
    return jsonify({'login_url': kite_login_url(creds['api_key'], state=user_id), 'api_key': creds['api_key']})


@app.route('/api/zerodha/session/<user_id>', methods=['POST'])
def zerodha_session_api(user_id):
    creds, err = _kite_credentials_or_400(user_id)
    if err:
        return jsonify(err[0]), err[1]
    data = get_request_json()
    request_token = str(data.get('request_token') or '').strip()
    if not request_token:
        return jsonify({'error': 'Paste the request_token returned by Zerodha login.'}), 400
    try:
        resp = generate_session(creds['api_key'], creds['api_secret'], request_token)
        sess = resp.get('data') or {}
        db_save_zerodha_session(user_id, sess)
        profile = {}
        try:
            profile_resp = get_profile(creds['api_key'], sess.get('access_token'))
            profile = profile_resp.get('data') or {}
            if profile:
                sess.update({'kite_user_id': profile.get('user_id'), 'user_name': profile.get('user_name') or profile.get('user_shortname'), 'email': profile.get('email')})
                db_save_zerodha_session(user_id, sess)
        except Exception:
            pass
        return jsonify({'message': 'Zerodha connected', 'connected': True, 'profile': profile})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/zerodha/disconnect/<user_id>', methods=['POST'])
def zerodha_disconnect(user_id):
    removed = db_delete_zerodha_session(user_id)
    return jsonify({'message': 'Zerodha disconnected', 'removed': int(removed or 0)})


@app.route('/api/zerodha/sync-holdings/<user_id>', methods=['POST'])
def zerodha_sync_holdings(user_id):
    creds, err = _kite_credentials_or_400(user_id)
    if err:
        return jsonify(err[0]), err[1]
    session = _zerodha_safe_session(user_id)
    if not session:
        return jsonify({'error': 'Connect Zerodha first.'}), 401
    try:
        broker_holdings = kite_get_holdings(creds['api_key'], session['access_token'])
        current = db_get_holdings(user_id) if db_configured() else load_json(PORTFOLIOS_FILE).get(user_id, [])
        existing_by_symbol = {_plain_symbol(h.get('symbol')): h for h in (current or [])}
        added, skipped = 0, 0
        for row in broker_holdings:
            qty = float(row.get('quantity') or row.get('t1_quantity') or 0)
            if qty <= 0:
                continue
            symbol = _plain_symbol(row.get('tradingsymbol') or row.get('symbol'))
            if not symbol:
                skipped += 1
                continue
            avg_price = float(row.get('average_price') or row.get('price') or 0)
            if avg_price <= 0:
                skipped += 1
                continue
            if symbol in existing_by_symbol:
                skipped += 1
                continue
            meta = lookup_ticker_meta(symbol)
            holding = {
                'id': str(uuid.uuid4()), 'symbol': symbol, 'name': meta.get('name') or symbol,
                'buy_price': round(avg_price, 4), 'qty': qty, 'date': str(date.today()),
                'industry': meta.get('industry', ''), 'sector': meta.get('sector', '')
            }
            if db_configured():
                db_add_holding(user_id, holding)
            else:
                portfolios = load_json(PORTFOLIOS_FILE)
                portfolios.setdefault(user_id, []).append(holding)
                save_json(PORTFOLIOS_FILE, portfolios)
            added += 1
        return jsonify({'message': 'Zerodha holdings synced', 'added': added, 'skipped_existing_or_invalid': skipped, 'broker_rows': len(broker_holdings)})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/zerodha/order/<user_id>', methods=['POST'])
def zerodha_order(user_id):
    creds, err = _kite_credentials_or_400(user_id)
    if err:
        return jsonify(err[0]), err[1]
    session = _zerodha_safe_session(user_id)
    if not session:
        return jsonify({'error': 'Connect Zerodha first.'}), 401
    data = get_request_json()
    tx = str(data.get('transaction_type') or data.get('side') or '').upper().strip()
    if tx not in ('BUY', 'SELL'):
        return jsonify({'error': 'transaction_type must be BUY or SELL'}), 400
    symbol = _plain_symbol(data.get('symbol'))
    exchange, tradingsymbol = normalise_exchange_symbol(symbol)
    qty = int(float(data.get('qty') or data.get('quantity') or 0))
    if qty <= 0:
        return jsonify({'error': 'Enter a valid order quantity'}), 400
    order_type = str(data.get('order_type') or 'MARKET').upper()
    product = str(data.get('product') or 'CNC').upper()
    validity = str(data.get('validity') or 'DAY').upper()
    variety = str(data.get('variety') or 'regular').lower()
    price = float(data.get('price') or 0)
    payload = {
        'exchange': exchange,
        'tradingsymbol': tradingsymbol,
        'transaction_type': tx,
        'quantity': qty,
        'product': product,
        'order_type': order_type,
        'validity': validity,
    }
    if order_type == 'LIMIT':
        if price <= 0:
            return jsonify({'error': 'Limit orders require a valid price'}), 400
        payload['price'] = price
    try:
        placed = kite_place_order(creds['api_key'], session['access_token'], variety=variety, **payload)
        order_id = (placed.get('data') or {}).get('order_id')
        if not order_id:
            return jsonify({'error': 'Zerodha did not return an order_id', 'response': placed}), 400
        final = kite_wait_for_complete(creds['api_key'], session['access_token'], order_id, timeout_sec=int(data.get('confirm_timeout_sec') or 12))
        status = str(final.get('status') or '').upper()
        exec_qty, exec_price = extract_executed(final, fallback_qty=qty, fallback_price=price)
        audit = {'order_id': order_id, 'symbol': symbol, 'transaction_type': tx, 'qty': exec_qty, 'price': exec_price, 'status': status, 'apex_updated': False, 'final': final}
        if status != 'COMPLETE':
            db_add_zerodha_order_audit(user_id, audit)
            return jsonify({'message': 'Order placed but not confirmed complete. Apex holdings were not updated.', 'order_id': order_id, 'status': status, 'apex_updated': False, 'order': final}), 202
        if exec_qty <= 0 or exec_price <= 0:
            db_add_zerodha_order_audit(user_id, audit)
            return jsonify({'error': 'Order is COMPLETE but executed quantity/average price was not available. Apex holdings were not updated.', 'order_id': order_id, 'order': final}), 400
        apex = None
        if tx == 'BUY':
            apex = _apply_confirmed_buy(user_id, symbol, exec_price, exec_qty)
        else:
            holding_id = str(data.get('holding_id') or '').strip()
            if not holding_id:
                return jsonify({'error': 'holding_id is required for broker SELL so Apex can update the correct holding after confirmation.', 'order_id': order_id}), 400
            trade, remaining_qty = _apply_confirmed_sell(user_id, holding_id, exec_price, exec_qty)
            apex = {'trade': trade, 'remaining_qty': remaining_qty}
        audit['apex_updated'] = True
        db_add_zerodha_order_audit(user_id, audit)
        return jsonify({'message': 'Order COMPLETE. Apex updated from confirmed Zerodha execution.', 'order_id': order_id, 'status': status, 'apex_updated': True, 'executed_qty': exec_qty, 'executed_price': exec_price, 'apex': apex, 'order': final})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

# ─── IMPORT BROKER TRADES ─────────────────────────────────────────────────────

BROKER_OPTIONS = ['auto'] + sorted(list(BROKER_HINTS.keys()))

@app.route('/api/import-trades/brokers', methods=['GET'])
def import_trade_brokers():
    return jsonify({'brokers': BROKER_OPTIONS, 'dedupe_mode': 'dedup_by_symbol_date_qty_price', 'holdings_mode': 'aggregate_by_symbol'})

@app.route('/api/import-trades/<user_id>', methods=['POST'])
def import_trades_from_broker(user_id):
    if 'file' not in request.files:
        return jsonify({'error': 'Upload a broker CSV file.'}), 400
    upload = request.files['file']
    if not upload or not upload.filename:
        return jsonify({'error': 'Upload a broker CSV file.'}), 400
    filename = upload.filename
    if not filename.lower().endswith('.csv'):
        return jsonify({'error': 'Only CSV broker tradebook/P&L files are supported for import.'}), 400
    broker = (request.form.get('broker') or 'auto').strip().lower() or 'auto'
    if broker not in BROKER_OPTIONS:
        broker = 'auto'

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix='apexwealth_broker_', suffix='.csv')
        os.close(fd)
        upload.save(tmp_path)
        parsed = normalize_file(tmp_path, broker=broker)
        open_holdings, closed_trades, match_warnings = match_fifo_to_apex(parsed.trades, lookup_ticker_meta, aggregate_holdings=True)

        warnings = []
        warnings.extend(parsed.warnings or [])
        warnings.extend(match_warnings or [])

        if db_configured():
            existing_holdings = db_get_holdings(user_id) or []
            existing_trades = db_get_trades(user_id) or []
        else:
            portfolios = load_json(PORTFOLIOS_FILE)
            trades_blob = load_json(TRADES_FILE)
            existing_holdings = portfolios.get(user_id, [])
            existing_trades = trades_blob.get(user_id, [])

        existing_h_keys = {holding_key(h) for h in existing_holdings}
        existing_t_keys = {trade_key(t) for t in existing_trades}
        holdings_added = 0
        trades_added = 0
        holdings_skipped = 0
        trades_skipped = 0

        if db_configured():
            for h in open_holdings:
                if holding_key(h) in existing_h_keys:
                    holdings_skipped += 1
                    continue
                h = {**h, 'id': str(uuid.uuid4()), 'date': h.get('date') or str(date.today())}
                db_add_holding(user_id, h)
                existing_h_keys.add(holding_key(h))
                holdings_added += 1
            for t in closed_trades:
                if trade_key(t) in existing_t_keys:
                    trades_skipped += 1
                    continue
                t = {**t, 'id': str(uuid.uuid4())}
                db_add_trade(user_id, t)
                existing_t_keys.add(trade_key(t))
                trades_added += 1
        else:
            portfolios = load_json(PORTFOLIOS_FILE)
            trades_blob = load_json(TRADES_FILE)
            portfolios.setdefault(user_id, [])
            trades_blob.setdefault(user_id, [])
            for h in open_holdings:
                if holding_key(h) in existing_h_keys:
                    holdings_skipped += 1
                    continue
                h = {**h, 'id': str(uuid.uuid4()), 'date': h.get('date') or str(date.today())}
                portfolios[user_id].append(h)
                existing_h_keys.add(holding_key(h))
                holdings_added += 1
            for t in closed_trades:
                if trade_key(t) in existing_t_keys:
                    trades_skipped += 1
                    continue
                t = {**t, 'id': str(uuid.uuid4())}
                trades_blob[user_id].append(t)
                existing_t_keys.add(trade_key(t))
                trades_added += 1
            save_json(PORTFOLIOS_FILE, portfolios)
            save_json(TRADES_FILE, trades_blob)

        if holdings_skipped or trades_skipped:
            warnings.append(f'Duplicate protection skipped {holdings_skipped} holdings and {trades_skipped} closed trades already present for this user.')

        return jsonify({
            'message': 'Import completed',
            'detected_broker': parsed.detected_broker,
            'source_format': parsed.source_format,
            'header_row': parsed.header_row,
            'mapping': parsed.mapping,
            'warnings': warnings,
            'holdings_added': holdings_added,
            'trades_added': trades_added,
            'holdings_skipped': holdings_skipped,
            'trades_skipped': trades_skipped,
            'normalized_rows': int(len(parsed.trades)) if hasattr(parsed.trades, '__len__') else 0,
            'dedupe_mode': 'dedup_by_symbol_date_qty_price',
            'holdings_mode': 'aggregate_by_symbol',
            'storage': 'neon' if db_configured() else 'json-fallback'
        })
    except Exception as exc:
        return jsonify({'error': str(exc) or 'Import failed'}), 500
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

# ─── WATCHLIST ────────────────────────────────────────────────────────────────

def _watchlist_group_from_request():
    data = get_request_json() if request.method in ('POST', 'PUT', 'PATCH') else {}
    return normalize_watchlist_group(request.args.get('group') or data.get('group_name') or data.get('group') or 'Default')

def _json_watchlist_groups_blob(watchlists, user_id):
    raw = watchlists.get(user_id, [])
    if isinstance(raw, dict):
        groups = list(raw.keys()) or ['Default']
    else:
        groups = ['Default']
    return groups

@app.route('/api/watchlist-groups/<user_id>', methods=['GET'])
def get_watchlist_groups(user_id):
    groups = db_get_watchlist_groups(user_id) if db_configured() else None
    if groups is None:
        watchlists = load_json(WATCHLISTS_FILE)
        groups = _json_watchlist_groups_blob(watchlists, user_id)
    return jsonify({'groups': groups or ['Default']})

@app.route('/api/watchlist-groups/<user_id>', methods=['POST'])
def add_watchlist_group(user_id):
    data = get_request_json()
    group_name = normalize_watchlist_group(data.get('name') or data.get('group_name') or data.get('group'))
    if db_configured():
        group_name = db_add_watchlist_group(user_id, group_name)
        return jsonify({'message': 'Group ready', 'group': group_name, 'storage': 'neon'})
    watchlists = load_json(WATCHLISTS_FILE)
    raw = watchlists.get(user_id, [])
    if not isinstance(raw, dict):
        raw = {'Default': raw if isinstance(raw, list) else []}
    raw.setdefault(group_name, [])
    watchlists[user_id] = raw
    save_json(WATCHLISTS_FILE, watchlists)
    return jsonify({'message': 'Group ready', 'group': group_name, 'storage': 'json-fallback'})


@app.route('/api/watchlist-groups/<user_id>/<path:group_name>', methods=['DELETE'])
def delete_watchlist_group(user_id, group_name):
    group_name = normalize_watchlist_group(group_name)
    if group_name == 'Default':
        return jsonify({'error': 'Default group cannot be removed'}), 400
    if db_configured():
        result = db_delete_watchlist_group(user_id, group_name)
        return jsonify({'message': 'Group removed', 'group': group_name, 'removed_items': (result or {}).get('removed_items', 0), 'storage': 'neon'})
    watchlists = load_json(WATCHLISTS_FILE)
    raw = watchlists.get(user_id, [])
    removed = 0
    if isinstance(raw, dict) and group_name in raw:
        removed = len(raw.get(group_name) or [])
        raw.pop(group_name, None)
        watchlists[user_id] = raw
        save_json(WATCHLISTS_FILE, watchlists)
    return jsonify({'message': 'Group removed', 'group': group_name, 'removed_items': removed, 'storage': 'json-fallback'})

@app.route('/api/watchlist-groups/<user_id>/delete', methods=['POST'])
def delete_watchlist_group_post(user_id):
    data = get_request_json()
    group_name = normalize_watchlist_group(data.get('group_name') or data.get('group') or data.get('name'))
    return delete_watchlist_group(user_id, group_name)

@app.route('/api/watchlist/<user_id>', methods=['GET'])
def get_watchlist(user_id):
    group_name = _watchlist_group_from_request()
    items = db_get_watchlist(user_id, group_name) if db_configured() else None
    if items is None:
        watchlists = load_json(WATCHLISTS_FILE)
        raw = watchlists.get(user_id, [])
        if isinstance(raw, dict):
            items = raw.get(group_name, [])
        else:
            items = raw if group_name == 'Default' else []
    enriched = []
    for item in items:
        q = fetch_quote(item['symbol'])
        returns = fetch_return_profile(item['symbol'], q)
        base = {**item, 'group_name': item.get('group_name', group_name)}
        if q:
            enriched.append({**base, **q, **returns})
        else:
            enriched.append({**base, **returns})
    return jsonify(enriched)

@app.route('/api/watchlist/<user_id>', methods=['POST'])
def add_watchlist(user_id):
    data = get_request_json()
    group_name = normalize_watchlist_group(request.args.get('group') or data.get('group_name') or data.get('group') or 'Default')
    symbol = data['symbol'].upper()
    item = {'symbol': symbol, 'name': data.get('name', symbol),
            'industry': data.get('industry', data.get('sector', '')), 'added': str(date.today()), 'group_name': group_name}
    if db_configured():
        inserted = db_add_watchlist(user_id, item, group_name)
        if not inserted:
            return jsonify({'error': 'Already in selected watchlist group'}), 409
        return jsonify({'message': 'Added to watchlist', 'group': group_name, 'storage': 'neon'})

    watchlists = load_json(WATCHLISTS_FILE)
    raw = watchlists.get(user_id, [])
    if not isinstance(raw, dict):
        raw = {'Default': raw if isinstance(raw, list) else []}
    raw.setdefault(group_name, [])
    if any(w['symbol'] == symbol for w in raw[group_name]):
        return jsonify({'error': 'Already in selected watchlist group'}), 409
    raw[group_name].append(item)
    watchlists[user_id] = raw
    save_json(WATCHLISTS_FILE, watchlists)
    return jsonify({'message': 'Added to watchlist', 'group': group_name, 'storage': 'json-fallback'})

def _remove_watchlist_record(user_id, symbol, group_name='Default'):
    group_name = normalize_watchlist_group(group_name)
    target = _strip_exchange_suffix(symbol)
    if db_configured():
        # Purge across all groups so legacy duplicates cannot be restored
        # by the next refresh. The selected group is still returned for UI context.
        removed = db_delete_watchlist(user_id, target, group_name, purge_all=True)
        return int(removed or 0)
    watchlists = load_json(WATCHLISTS_FILE)
    raw = watchlists.get(user_id, [])
    removed = 0
    if isinstance(raw, dict):
        target_compact = _compact_symbol_key(target)
        for g, current in list(raw.items()):
            current = current if isinstance(current, list) else []
            before = len(current)
            raw[g] = [w for w in current if _strip_exchange_suffix(w.get('symbol')) != target and _compact_symbol_key(w.get('symbol')) != target_compact]
            removed += before - len(raw[g])
        watchlists[user_id] = raw
    else:
        current = raw if isinstance(raw, list) else []
        before = len(current)
        target_compact = _compact_symbol_key(target)
        watchlists[user_id] = [w for w in current if _strip_exchange_suffix(w.get('symbol')) != target and _compact_symbol_key(w.get('symbol')) != target_compact]
        removed = before - len(watchlists[user_id])
    save_json(WATCHLISTS_FILE, watchlists)
    return removed

@app.route('/api/watchlist/<user_id>/<path:symbol>', methods=['DELETE'])
def remove_watchlist(user_id, symbol):
    group_name = _watchlist_group_from_request()
    removed = _remove_watchlist_record(user_id, symbol, group_name)
    return jsonify({'message': 'Removed', 'removed': removed, 'group': group_name})

@app.route('/api/watchlist/<user_id>/<path:symbol>/delete', methods=['POST'])
def remove_watchlist_post(user_id, symbol):
    group_name = _watchlist_group_from_request()
    removed = _remove_watchlist_record(user_id, symbol, group_name)
    return jsonify({'message': 'Removed', 'removed': removed, 'group': group_name})

@app.route('/api/watchlist-delete/<user_id>', methods=['POST'])
def remove_watchlist_json(user_id):
    # JSON-body delete avoids special-character path issues for symbols like J&KBANK.
    data = get_request_json()
    symbol = data.get('symbol') or data.get('ticker')
    group_name = normalize_watchlist_group(data.get('group') or data.get('group_name') or request.args.get('group') or 'Default')
    if not symbol:
        return jsonify({'error': 'symbol is required'}), 400
    removed = _remove_watchlist_record(user_id, symbol, group_name)
    return jsonify({'message': 'Removed', 'removed': removed, 'group': group_name, 'symbol': _strip_exchange_suffix(symbol)})


@app.route('/api/portfolio-alerts/<user_id>', methods=['GET'])
def get_portfolio_alerts(user_id):
    alerts = db_get_portfolio_alerts(user_id) if db_configured() else None
    if alerts is None:
        data = load_json(ALERTS_FILE)
        alerts = data.get(user_id, [])
    return jsonify(alerts or [])

@app.route('/api/portfolio-alerts/<user_id>', methods=['POST'])
def add_portfolio_alert(user_id):
    data = get_request_json()
    if not data.get('symbol'):
        return jsonify({'error': 'symbol is required'}), 400
    if data.get('threshold') in (None, ''):
        return jsonify({'error': 'threshold is required'}), 400
    if db_configured():
        try:
            alert = db_add_portfolio_alert(user_id, data)
        except Exception as e:
            return jsonify({'error': str(e)}), 400
        return jsonify({'message': 'Alert saved', 'alert': alert, 'storage': 'neon'})
    alerts = load_json(ALERTS_FILE)
    alert = {
        'id': str(uuid.uuid4()), 'user_id': user_id,
        'holding_id': str(data.get('holding_id') or ''),
        'symbol': str(data.get('symbol') or '').upper().strip(),
        'column_name': data.get('column_name') or data.get('column') or 'ltp',
        'condition_op': data.get('condition_op') or data.get('condition') or '>',
        'threshold': float(data.get('threshold')), 'active': True,
        'source': data.get('source') or ('portfolio' if data.get('holding_id') else 'watchlist'),
        'created_at': datetime.utcnow().isoformat()
    }
    alerts.setdefault(user_id, []).append(alert)
    save_json(ALERTS_FILE, alerts)
    return jsonify({'message': 'Alert saved', 'alert': alert, 'storage': 'json-fallback'})

@app.route('/api/portfolio-alerts/<user_id>/<alert_id>', methods=['PUT'])
def update_portfolio_alert(user_id, alert_id):
    data = get_request_json()
    if data.get('threshold') in (None, ''):
        return jsonify({'error': 'threshold is required'}), 400
    if db_configured():
        try:
            alert = db_update_portfolio_alert(user_id, alert_id, data)
        except Exception as e:
            return jsonify({'error': str(e)}), 400
        return jsonify({'message': 'Alert updated', 'alert': alert, 'storage': 'neon'})
    alerts = load_json(ALERTS_FILE)
    current = alerts.get(user_id, [])
    updated = 0
    for a in current:
        if str(a.get('id')) == str(alert_id):
            a['column_name'] = data.get('column_name') or data.get('column') or 'ltp'
            a['condition_op'] = data.get('condition_op') or data.get('condition') or '>'
            a['threshold'] = float(data.get('threshold'))
            a['triggered_at'] = None
            a['active'] = True
            updated = 1
    alerts[user_id] = current
    save_json(ALERTS_FILE, alerts)
    return jsonify({'message': 'Alert updated', 'updated': updated, 'storage': 'json-fallback'})

@app.route('/api/portfolio-alerts/<user_id>/<alert_id>/triggered', methods=['POST'])
def mark_portfolio_alert_triggered(user_id, alert_id):
    if db_configured():
        ts = db_mark_portfolio_alert_triggered(user_id, alert_id)
        return jsonify({'message': 'Alert marked triggered', 'triggered_at': ts, 'storage': 'neon'})
    alerts = load_json(ALERTS_FILE)
    ts = datetime.utcnow().isoformat()
    for a in alerts.get(user_id, []):
        if str(a.get('id')) == str(alert_id) and a.get('active', True):
            a['triggered_at'] = a.get('triggered_at') or ts
            ts = a['triggered_at']
    save_json(ALERTS_FILE, alerts)
    return jsonify({'message': 'Alert marked triggered', 'triggered_at': ts, 'storage': 'json-fallback'})

@app.route('/api/portfolio-alerts/<user_id>/<alert_id>', methods=['DELETE'])
def delete_portfolio_alert(user_id, alert_id):
    if db_configured():
        removed = db_delete_portfolio_alert(user_id, alert_id)
        return jsonify({'message': 'Alert removed', 'removed': removed, 'storage': 'neon'})
    alerts = load_json(ALERTS_FILE)
    current = alerts.get(user_id, [])
    before = len(current)
    alerts[user_id] = [a for a in current if str(a.get('id')) != str(alert_id)]
    save_json(ALERTS_FILE, alerts)
    return jsonify({'message': 'Alert removed', 'removed': before - len(alerts[user_id]), 'storage': 'json-fallback'})

# ─── TRADES HISTORY ───────────────────────────────────────────────────────────

@app.route('/api/trades/<user_id>', methods=['GET'])
def get_trades(user_id):
    items = db_get_trades(user_id) if db_configured() else None
    if items is None:
        trades = load_json(TRADES_FILE)
        items = trades.get(user_id, [])
    return jsonify(items)


@app.route('/api/trades/<user_id>/<trade_id>', methods=['DELETE'])
@app.route('/api/trades/<user_id>/<trade_id>/delete', methods=['POST'])
def delete_trade(user_id, trade_id):
    if db_configured():
        removed = db_delete_trade(user_id, trade_id)
        if removed is None:
            return jsonify({'error': DB_LAST_ERROR or 'Database unavailable'}), 500
        return jsonify({'message': 'Trade deleted', 'removed': removed, 'storage': 'neon'})
    trades = load_json(TRADES_FILE)
    current = trades.get(user_id, [])
    before = len(current)
    trades[user_id] = [t for t in current if str(t.get('id')) != str(trade_id)]
    save_json(TRADES_FILE, trades)
    return jsonify({'message': 'Trade deleted', 'removed': before - len(trades[user_id]), 'storage': 'json-fallback'})


# ─── INDEX PULLERS & DRAGGERS ────────────────────────────────────────────────
PD_CACHE_TTL_SECONDS = int(os.getenv('PULLERS_DRAGGERS_CACHE_TTL_SECONDS', '60'))
PD_CACHE = {}

PD_INDEX_CONFIG = {
    'nifty': {
        'name': 'Nifty 50',
        'ticker': '^NSEI',
        'data': nifty50_data,
        'label': 'Nifty',
        'base_value': 24085.70,
    },
    'banknifty': {
        'name': 'Bank Nifty',
        'ticker': '^NSEBANK',
        'data': banknifty_data,
        'label': 'Banknifty',
        'base_value': 57585.05,
    },
    'sensex': {
        'name': 'BSE Sensex',
        'ticker': '^BSESN',
        'data': sensex_data,
        'label': 'Sensex',
        'base_value': 77155.62,
    },
}

def _pd_float(value, default=0.0):
    try:
        if value is None:
            return default
        if isinstance(value, float) and value != value:
            return default
        return float(value)
    except Exception:
        return default

def _pd_price_from_hist(hist):
    try:
        if hist is None or hist.empty or 'Close' not in hist.columns:
            return 0.0, 0.0, 0.0
        close = hist['Close'].dropna()
        if close.empty:
            return 0.0, 0.0, 0.0
        current = _pd_float(close.iloc[-1])
        previous = _pd_float(close.iloc[-2] if len(close) > 1 else current)
        change = current - previous
        pct = (change / previous * 100.0) if previous else 0.0
        return current, change, pct
    except Exception:
        return 0.0, 0.0, 0.0

def _pd_yf_symbol(symbol):
    raw = str(symbol or '').strip().upper()
    if raw.startswith('^') or raw.endswith('.NS') or raw.endswith('.BO'):
        return raw
    return f'{raw}.NS'

def _pd_fetch_prices(symbols):
    if not symbols:
        return {}
    tickers = [_pd_yf_symbol(s) for s in symbols]
    out = {}
    try:
        raw = yf.download(
            tickers=' '.join(tickers),
            period='5d',
            interval='1d',
            group_by='ticker',
            auto_adjust=False,
            progress=False,
            threads=True,
            timeout=12,
        )
        for original, ticker in zip(symbols, tickers):
            try:
                if isinstance(raw.columns, pd.MultiIndex) and ticker in raw.columns.get_level_values(0):
                    hist = raw[ticker]
                else:
                    hist = raw
                current, change, pct = _pd_price_from_hist(hist)
                if current:
                    out[original] = {'rate': current, 'change': change, 'change_pct': pct}
            except Exception:
                continue
    except Exception:
        return {}
    return out

def _pd_fetch_index_value(index_key):
    cfg = PD_INDEX_CONFIG[index_key]
    base_value = cfg['base_value']
    try:
        # Prefer the app's direct Yahoo chart helper when available; fall back to yfinance.
        rows = _yahoo_chart(cfg['ticker'], '5d', '1d').get('rows', [])
        closes = [_pd_float(r.get('close')) for r in rows if _pd_float(r.get('close'))]
        if closes:
            current = closes[-1]
            previous = closes[-2] if len(closes) > 1 else current
            change = current - previous
            pct = (change / previous * 100.0) if previous else 0.0
            return {'value': current, 'change': change, 'change_pct': pct, 'previous': previous}
    except Exception:
        pass
    try:
        hist = yf.download(cfg['ticker'], period='5d', interval='1d', auto_adjust=False, progress=False, threads=False, timeout=12)
        current, change, pct = _pd_price_from_hist(hist)
        if current:
            return {'value': current, 'change': change, 'change_pct': pct, 'previous': current - change}
    except Exception:
        pass
    return {'value': base_value, 'change': 0.0, 'change_pct': 0.0, 'previous': base_value}

def _pd_fallback_move(symbol, rank):
    seed = sum(ord(c) for c in str(symbol)) + int(rank or 1) * 17
    rate = 100 + (seed % 9000) + ((seed % 99) / 100.0)
    pct = ((seed % 420) - 170) / 100.0
    if int(rank or 1) % 11 == 0:
        pct = -abs(pct) - 0.35
    elif int(rank or 1) % 7 == 0:
        pct = abs(pct) + 0.45
    change = rate * pct / 100.0
    return {'rate': rate, 'change': change, 'change_pct': pct}

def _pd_build_payload(index_key):
    now = time.time()
    cached = PD_CACHE.get(index_key)
    if cached and now - cached[0] < PD_CACHE_TTL_SECONDS:
        return cached[1]
    cfg = PD_INDEX_CONFIG[index_key]
    rows = [dict(x) for x in cfg['data']]
    symbols = [r.get('Symbol/Ticker') for r in rows]
    live_prices = _pd_fetch_prices(symbols)
    index_info = _pd_fetch_index_value(index_key)
    previous_index = index_info.get('previous') or cfg['base_value']

    enriched = []
    for r in rows:
        symbol = r.get('Symbol/Ticker', '')
        rank = int(r.get('Rank') or 0)
        quote = live_prices.get(symbol) or _pd_fallback_move(symbol, rank)
        pct = _pd_float(quote.get('change_pct'))
        weight = _pd_float(r.get('Weightage (%)'))
        w_point = previous_index * (weight / 100.0) * (pct / 100.0)
        enriched.append({
            'rank': rank,
            'company': r.get('Company Name', ''),
            'symbol': symbol,
            'rate': round(_pd_float(quote.get('rate')), 2),
            'change_pct': round(pct, 2),
            'w_point': round(w_point, 2),
            'weight': round(weight, 2),
        })

    pullers = sorted([x for x in enriched if x['w_point'] > 0], key=lambda x: x['w_point'], reverse=True)
    draggers = sorted([x for x in enriched if x['w_point'] < 0], key=lambda x: x['w_point'])
    unchanged = [x for x in enriched if x['w_point'] == 0]
    pull_total = round(sum(x['w_point'] for x in pullers), 2)
    drag_total = round(sum(x['w_point'] for x in draggers), 2)

    if abs(_pd_float(index_info.get('change'))) < 0.001:
        derived_change = pull_total + drag_total
        current_value = cfg['base_value'] + derived_change
        index_info = {
            'value': round(current_value, 2),
            'change': round(derived_change, 2),
            'change_pct': round((derived_change / cfg['base_value'] * 100.0), 2),
            'previous': cfg['base_value'],
        }

    payload = {
        'key': index_key,
        'name': cfg['name'],
        'label': cfg['label'],
        'status': 'Market Closed',
        'last_updated': datetime.now().strftime('%I:%M:%S %p').lower(),
        'index': {
            'value': round(_pd_float(index_info.get('value')), 2),
            'change': round(_pd_float(index_info.get('change')), 2),
            'change_pct': round(_pd_float(index_info.get('change_pct')), 2),
        },
        'summary': {
            'pull_count': len(pullers),
            'drag_count': len(draggers),
            'unchanged_count': len(unchanged),
            'pull_total': pull_total,
            'drag_total': drag_total,
            'net': round(pull_total + drag_total, 2),
        },
        'pullers': pullers,
        'draggers': draggers,
        'unchanged': unchanged,
    }
    PD_CACHE[index_key] = (now, payload)
    return payload

@app.route('/api/market/pullers-draggers', methods=['GET'])
@app.route('/api/pullers-draggers', methods=['GET'])
def api_market_pullers_draggers():
    index_key = request.args.get('index', 'nifty').lower()
    if index_key not in PD_INDEX_CONFIG:
        return jsonify({'error': 'Invalid index. Use nifty, banknifty, or sensex.'}), 400
    if request.args.get('refresh') == '1':
        PD_CACHE.pop(index_key, None)
    return jsonify(_pd_build_payload(index_key))

# ─── MARKET DATA ──────────────────────────────────────────────────────────────

@app.route('/api/quote/<symbol>', methods=['GET'])
def get_quote(symbol):
    q = fetch_quote(symbol)
    if q:
        return jsonify(q)
    return jsonify({'error': 'Quote unavailable'}), 404

INDEX_MAP = {
    'nifty50': {'name': 'Nifty 50', 'ticker': '^NSEI'},
    'banknifty': {'name': 'Nifty Bank', 'ticker': '^NSEBANK'},
    'sensex': {'name': 'BSE Sensex', 'ticker': '^BSESN'},
}

def _safe_float(v):
    try:
        if v is None or v != v:
            return None
        return float(v)
    except Exception:
        return None

def _pct_change_from(hist, lookback_rows):
    try:
        if hist is None or hist.empty:
            return 0
        ltp = _safe_float(hist['Close'].iloc[-1])
        if ltp is None:
            return 0
        idx = max(0, len(hist) - 1 - lookback_rows)
        base = _safe_float(hist['Close'].iloc[idx])
        if not base:
            return 0
        return round(((ltp - base) / base) * 100, 2)
    except Exception:
        return 0

@app.route('/api/market/indices', methods=['GET'])
def market_indices():
    result = []
    for key, meta in INDEX_MAP.items():
        try:
            daily_rows = _yahoo_chart(meta['ticker'], '1y', '1d').get('rows', [])
            intra_rows = _yahoo_chart(meta['ticker'], '1d', '5m').get('rows', [])
            source = intra_rows if intra_rows else daily_rows
            if not source:
                continue
            latest = source[-1]
            ltp = _safe_float(latest.get('close'))
            prev = _safe_float((daily_rows[-2] or {}).get('close')) if len(daily_rows) >= 2 else _safe_float((daily_rows[-1] or {}).get('close')) if daily_rows else ltp
            day_high = max([_safe_float(r.get('high')) or _safe_float(r.get('close')) or 0 for r in source]) if source else ltp
            day_low = min([_safe_float(r.get('low')) or _safe_float(r.get('close')) or 0 for r in source]) if source else ltp
            chg_abs = round((ltp - prev), 2) if ltp is not None and prev is not None else 0
            chg_pct = round(((ltp - prev) / prev) * 100, 2) if ltp is not None and prev else 0
            result.append({
                'key': key,
                'name': meta['name'],
                'value': round(ltp, 2) if ltp is not None else None,
                'chg': chg_abs,
                'chg_pct': chg_pct,
                'day_high': round(day_high, 2) if day_high is not None else None,
                'day_low': round(day_low, 2) if day_low is not None else None,
                'ret_1d': chg_pct,
                'ret_1w': _row_pct(daily_rows, 5),
                'ret_1m': _row_pct(daily_rows, 22),
                'ret_1y': _row_pct(daily_rows, min(252, max(1, len(daily_rows)-1))),
            })
        except Exception:
            pass
    return jsonify(result)

@app.route('/api/market/index-chart/<index_key>', methods=['GET'])
def market_index_chart(index_key):
    meta = INDEX_MAP.get(index_key.lower())
    if not meta:
        return jsonify({'error': 'Unknown index'}), 404
    period = request.args.get('period', '1d')
    period_map = {
        '1d': ('1d', '5m'),
        '1w': ('5d', '30m'),
        '1m': ('1mo', '1d'),
        '1y': ('1y', '1wk'),
    }
    yf_period, interval = period_map.get(period, ('1d', '5m'))
    try:
        rows = _yahoo_chart(meta['ticker'], yf_period, interval, use_cache=False).get('rows', [])
        if not rows and interval != '1d':
            rows = _yahoo_chart(meta['ticker'], yf_period, '1d', use_cache=False).get('rows', [])
        data = []
        for row in rows:
            close = _safe_float(row.get('close'))
            if close is None:
                continue
            dt = datetime.fromtimestamp(row['timestamp'])
            label = dt.strftime('%H:%M') if period == '1d' else dt.strftime('%d %b')
            data.append({
                'date': row.get('datetime') or row.get('date'),
                'label': label,
                'close': round(close, 2),
                'high': round(_safe_float(row.get('high')) or close, 2),
                'low': round(_safe_float(row.get('low')) or close, 2),
            })
        return jsonify({'key': index_key, 'name': meta['name'], 'period': period, 'data': data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/market/top-movers', methods=['GET'])
def top_movers():
    """Get top movers from all NIFTY 50 stocks using yfinance batch download and a 5-minute cache."""
    now = time.time()
    if TOP_MOVERS_CACHE.get('data') and (now - TOP_MOVERS_CACHE.get('ts', 0) < TOP_MOVERS_TTL):
        return jsonify(TOP_MOVERS_CACHE['data'])

    movers = []
    yf_symbols = [_yf_symbol(s) for s in NIFTY50_SYMBOLS]
    try:
        hist = yf.download(
            ' '.join(yf_symbols),
            period='5d',
            interval='1d',
            group_by='ticker',
            auto_adjust=False,
            progress=False,
            threads=True,
            timeout=12,
        )
        for raw_sym, yf_sym in zip(NIFTY50_SYMBOLS, yf_symbols):
            try:
                frame = hist[yf_sym] if len(yf_symbols) > 1 else hist
                frame = frame.dropna(how='all')
                if frame.empty:
                    continue
                latest = frame.iloc[-1]
                prev_close = _safe_float(frame['Close'].iloc[-2]) if len(frame) >= 2 else _safe_float(latest.get('Close'))
                ltp = _safe_float(latest.get('Close'))
                if ltp is None:
                    continue
                chg_pct = ((ltp - prev_close) / prev_close * 100) if prev_close else 0
                movers.append({
                    'symbol': raw_sym,
                    'ltp': round(ltp, 2),
                    'prev_close': round(prev_close if prev_close is not None else ltp, 2),
                    'day_high': round(_safe_float(latest.get('High')) or ltp, 2),
                    'day_low': round(_safe_float(latest.get('Low')) or ltp, 2),
                    'volume': _safe_int(latest.get('Volume')),
                    'day_chg_pct': round(chg_pct, 2),
                })
            except Exception:
                continue
    except Exception:
        # Fallback to the same yfinance quote method used elsewhere.
        for sym in NIFTY50_SYMBOLS:
            q = fetch_quote(sym)
            if q:
                movers.append(q)

    gainers = sorted([m for m in movers if m['day_chg_pct'] > 0], key=lambda x: -x['day_chg_pct'])[:5]
    losers = sorted([m for m in movers if m['day_chg_pct'] < 0], key=lambda x: x['day_chg_pct'])[:5]
    data = {'gainers': gainers, 'losers': losers, 'count': len(movers), 'cached_for_seconds': TOP_MOVERS_TTL, 'source': 'yfinance_download_batch'}
    TOP_MOVERS_CACHE['ts'] = now
    TOP_MOVERS_CACHE['data'] = data
    return jsonify(data)



def _format_statement_date(col):
    try:
        return col.strftime('%d-%b-%Y')
    except Exception:
        return str(col)[:10]

def _clean_financial_value(value):
    try:
        if value is None:
            return None
        # pandas/numpy NaN support without importing pandas globally
        if value != value:
            return None
        if hasattr(value, 'item'):
            value = value.item()
        if isinstance(value, (int, float)):
            return round(float(value), 2)
        return str(value)
    except Exception:
        return None

def _statement_to_payload(df, title, max_periods=4):
    try:
        if df is None or df.empty:
            return {'title': title, 'columns': [], 'rows': []}
        df = df.iloc[:, :max_periods]
        columns = [_format_statement_date(c) for c in df.columns]
        rows = []
        for metric, row in df.iterrows():
            values = {}
            for original_col, label in zip(df.columns, columns):
                values[label] = _clean_financial_value(row.get(original_col))
            rows.append({'metric': str(metric), 'values': values})
        return {'title': title, 'columns': columns, 'rows': rows}
    except Exception as e:
        return {'title': title, 'columns': [], 'rows': [], 'error': str(e)}

@app.route('/api/fundamentals/<symbol>', methods=['GET'])
def get_fundamentals(symbol):
    """Return last 4 annual/quarterly financial statement columns from yfinance."""
    try:
        ticker = yf.Ticker(get_nse_ticker(symbol))
        return jsonify({
            'symbol': symbol.upper(),
            'annual_income_statement': _statement_to_payload(ticker.financials, 'Annual Income Statement'),
            'quarterly_income_statement': _statement_to_payload(ticker.quarterly_income_stmt, 'Quarterly Income Statement'),
            'quarterly_balance_sheet': _statement_to_payload(ticker.quarterly_balance_sheet, 'Quarterly Balance Sheet'),
            'annual_cash_flow': _statement_to_payload(ticker.get_cash_flow(freq='yearly'), 'Annual Cash Flow'),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _safe_float(v):
    try:
        if v is None or v != v:
            return None
        if hasattr(v, 'item'):
            v = v.item()
        return float(v)
    except Exception:
        return None

def _first_available(df, labels, col_idx=0):
    try:
        if df is None or df.empty or len(df.columns) <= col_idx:
            return None
        for label in labels:
            if label in df.index:
                return _safe_float(df.iloc[df.index.get_loc(label), col_idx])
        # fallback: case-insensitive contains
        idx_lower = {str(i).lower(): i for i in df.index}
        for label in labels:
            l = label.lower()
            for low, real in idx_lower.items():
                if l == low or l in low:
                    return _safe_float(df.loc[real].iloc[col_idx])
    except Exception:
        return None
    return None

def _pct_change(new, old):
    try:
        if new is None or old in (None, 0):
            return None
        return round(((new - old) / abs(old)) * 100, 2)
    except Exception:
        return None

def _cagr(values):
    vals = [v for v in values if v not in (None, 0)]
    try:
        if len(vals) < 2:
            return None, ''
        latest, oldest = vals[0], vals[-1]
        years = len(vals) - 1
        if oldest <= 0 or latest <= 0:
            return None, f'({years}Y)'
        return round(((latest / oldest) ** (1 / years) - 1) * 100, 2), f'({years}Y)'
    except Exception:
        return None, ''

def _fmt_backend(v, suffix=''):
    if v is None:
        return '—'
    try:
        if suffix == '%':
            return f"{v:.1f}%"
        if suffix == 'x':
            return f"{v:.2f}x"
        return f"{v:,.2f}"
    except Exception:
        return str(v)

def _score_high(v, bad, ok, good, great):
    if v is None:
        return 5
    if v >= great: return 10
    if v >= good: return 8
    if v >= ok: return 6
    if v >= bad: return 4
    return 2

def _score_low(v, great, good, ok, bad):
    if v is None:
        return 5
    if v <= great: return 10
    if v <= good: return 8
    if v <= ok: return 6
    if v <= bad: return 4
    return 2

@app.route('/api/analysis/snapshot-score/<symbol>', methods=['GET'])
def get_snapshot_score(symbol):
    """Snapshot and Score data for Analysis tabs, derived from yfinance statements."""
    try:
        yf_symbol = get_nse_ticker(symbol)
        t = yf.Ticker(yf_symbol)
        info = {}
        try:
            info = t.info or {}
        except Exception:
            info = {}
        qinc = t.quarterly_income_stmt
        fin = t.financials
        bs = t.balance_sheet
        qbs = t.quarterly_balance_sheet
        cf = t.get_cash_flow(freq='yearly')
        hist = t.history(period='1y')

        # Snapshot growth rows
        growth_rows = []
        q_metrics = [
            ('Revenue', ['Total Revenue', 'Operating Revenue']),
            ('Operating Profit', ['Operating Income', 'EBIT']),
            ('Net Profit', ['Net Income', 'Net Income Common Stockholders']),
            ('Diluted EPS', ['Diluted EPS', 'Basic EPS'])
        ]
        for display, labels in q_metrics:
            latest = _first_available(qinc, labels, 0)
            prior = _first_available(qinc, labels, 1)
            same_ly = _first_available(qinc, labels, 4)
            growth_rows.append({
                'metric': display,
                'latest': round(latest, 2) if latest is not None else None,
                'prior': round(prior, 2) if prior is not None else None,
                'same_ly': round(same_ly, 2) if same_ly is not None else None,
                'yoy_pct': _pct_change(latest, same_ly),
                'qoq_pct': _pct_change(latest, prior)
            })

        revenue = _first_available(fin, ['Total Revenue', 'Operating Revenue'], 0)
        prior_revenue = _first_available(fin, ['Total Revenue', 'Operating Revenue'], 1)
        revenue_vals = []
        eps_vals = []
        if fin is not None and not fin.empty:
            for i in range(min(6, len(fin.columns))):
                revenue_vals.append(_first_available(fin, ['Total Revenue', 'Operating Revenue'], i))
                eps_vals.append(_first_available(fin, ['Basic EPS', 'Diluted EPS'], i))
        revenue_growth_series = []
        for i in range(len(revenue_vals)-1):
            pct = _pct_change(revenue_vals[i], revenue_vals[i+1])
            if pct is not None:
                revenue_growth_series.append(pct)
        revenue_volatility = round(float(np.std(revenue_growth_series, ddof=1)), 2) if len(revenue_growth_series) >= 2 else None
        eps_cagr, eps_cagr_period = _cagr(eps_vals)
        net_income = _first_available(fin, ['Net Income', 'Net Income Common Stockholders'], 0)
        prior_net_income = _first_available(fin, ['Net Income', 'Net Income Common Stockholders'], 1)
        ebit = _first_available(fin, ['EBIT', 'Operating Income'], 0)
        operating_income = _first_available(fin, ['Operating Income', 'EBIT'], 0)
        ebitda = _first_available(fin, ['EBITDA', 'Normalized EBITDA'], 0)
        prior_ebitda = _first_available(fin, ['EBITDA', 'Normalized EBITDA'], 1)
        cfo_vals = []
        for i in range(min(5, len(cf.columns) if cf is not None and not cf.empty else 0)):
            cfo_vals.append(_first_available(cf, ['Operating Cash Flow', 'Total Cash From Operating Activities'], i))
        latest_cfo = cfo_vals[0] if cfo_vals else None
        capex = _first_available(cf, ['Capital Expenditure', 'Capital Expenditures'], 0) if cf is not None and not cf.empty else None
        free_cash_flow = (latest_cfo + capex) if latest_cfo is not None and capex is not None else None
        cfo_cagr, cfo_period = _cagr(cfo_vals)
        cfo_margin = round((latest_cfo / revenue) * 100, 2) if latest_cfo is not None and revenue else None
        cfo_np_ratio = round(latest_cfo / net_income, 2) if latest_cfo is not None and net_income else None
        operating_margin = round((operating_income / revenue) * 100, 2) if operating_income is not None and revenue else None
        net_margin_snapshot = round((net_income / revenue) * 100, 2) if net_income is not None and revenue else None
        operating_cf_margin = round((latest_cfo / revenue) * 100, 2) if latest_cfo is not None and revenue else None
        free_cf_margin = round((free_cash_flow / revenue) * 100, 2) if free_cash_flow is not None and revenue else None
        total_assets = _first_available(bs, ['Total Assets'], 0)
        prior_total_assets = _first_available(bs, ['Total Assets'], 1)
        current_liab = _first_available(bs, ['Current Liabilities', 'Total Current Liabilities'], 0)
        capital_employed = (total_assets - current_liab) if total_assets is not None and current_liab is not None else None
        roce = round((ebit / capital_employed) * 100, 2) if ebit is not None and capital_employed else None

        # More score metrics
        latest_rev = revenue
        old_rev = _first_available(fin, ['Total Revenue', 'Operating Revenue'], min(3, max(0, len(fin.columns)-1))) if fin is not None and not fin.empty else None
        rev_cagr = _pct_change(latest_rev, old_rev)
        net_margin = round((net_income / revenue) * 100, 2) if net_income is not None and revenue else None
        roe = info.get('returnOnEquity')
        try: roe = round(float(roe) * 100, 2) if roe is not None and abs(float(roe)) < 2 else _safe_float(roe)
        except Exception: roe = None
        debt = _first_available(bs, ['Total Debt', 'Total Liabilities Net Minority Interest'], 0)
        equity = _first_available(bs, ['Stockholders Equity', 'Total Equity Gross Minority Interest'], 0)
        de_ratio = round(debt / equity, 2) if debt is not None and equity else None
        current_assets = _first_available(bs, ['Current Assets', 'Total Current Assets'], 0)
        current_ratio = round(current_assets / current_liab, 2) if current_assets is not None and current_liab else None
        asset_turnover = round(revenue / total_assets, 2) if revenue is not None and total_assets else None
        momentum_1y = None
        try:
            if hist is not None and not hist.empty and len(hist) > 20:
                last = float(hist['Close'].iloc[-1])
                first = float(hist['Close'].iloc[0])
                momentum_1y = round((last - first) / first * 100, 2) if first else None
        except Exception:
            pass

        revenue_growth_latest = _pct_change(revenue, prior_revenue)
        ebitda_growth = _pct_change(ebitda, prior_ebitda)
        net_income_growth = _pct_change(net_income, prior_net_income)
        asset_growth = _pct_change(total_assets, prior_total_assets)

        score_metrics = [
            {'metric':'Revenue Growth', 'value':rev_cagr, 'benchmark':'>20% strong', 'score':_score_high(rev_cagr, 0, 8, 15, 25), 'pillar':'Growth', 'suffix':'%'},
            {'metric':'Net Margin', 'value':net_margin, 'benchmark':'>15% strong', 'score':_score_high(net_margin, 0, 8, 15, 25), 'pillar':'Profitability', 'suffix':'%'},
            {'metric':'ROE', 'value':roe, 'benchmark':'>18% strong', 'score':_score_high(roe, 0, 10, 18, 25), 'pillar':'Profitability', 'suffix':'%'},
            {'metric':'CFO Margin', 'value':cfo_margin, 'benchmark':'>15% strong', 'score':_score_high(cfo_margin, 0, 6, 12, 20), 'pillar':'Cash Flow', 'suffix':'%'},
            {'metric':'CFO / Net Profit', 'value':cfo_np_ratio, 'benchmark':'>1.0x strong', 'score':_score_high(cfo_np_ratio, 0.3, 0.7, 1.0, 1.4), 'pillar':'Cash Flow', 'suffix':'x'},
            {'metric':'Debt / Equity', 'value':de_ratio, 'benchmark':'<0.5x strong', 'score':_score_low(de_ratio, 0.2, 0.5, 1.0, 2.0), 'pillar':'Balance Sheet', 'suffix':'x'},
            {'metric':'Current Ratio', 'value':current_ratio, 'benchmark':'>1.5x healthy', 'score':_score_high(current_ratio, 0.8, 1.1, 1.5, 2.0), 'pillar':'Balance Sheet', 'suffix':'x'},
            {'metric':'Asset Turnover', 'value':asset_turnover, 'benchmark':'>1.0x efficient', 'score':_score_high(asset_turnover, 0.2, 0.5, 1.0, 1.5), 'pillar':'Efficiency', 'suffix':'x'},
            {'metric':'1Y Price Momentum', 'value':momentum_1y, 'benchmark':'>20% positive', 'score':_score_high(momentum_1y, -20, 0, 20, 50), 'pillar':'Momentum', 'suffix':'%'},
        ]
        weights = {'Growth':20, 'Profitability':20, 'Cash Flow':25, 'Balance Sheet':15, 'Efficiency':10, 'Momentum':10}
        icons = {'Growth':'📈', 'Profitability':'💰', 'Cash Flow':'🌊', 'Balance Sheet':'⚖️', 'Efficiency':'⚙️', 'Momentum':'🚀'}
        pillars = []
        total = 0
        for p_name, wt in weights.items():
            vals = [m['score'] for m in score_metrics if m['pillar'] == p_name]
            avg = sum(vals) / len(vals) * 10 if vals else 50
            total += avg * wt / 100
            items = []
            for m in [m for m in score_metrics if m['pillar'] == p_name][:3]:
                cls = 'si-pass' if m['score'] >= 7 else 'si-warn' if m['score'] >= 5 else 'si-fail'
                items.append({'text': f"{m['metric']}: {_fmt_backend(m['value'], m['suffix'])}", 'cls': cls})
            pillars.append({'name':p_name, 'weight':wt, 'score':round(avg, 1), 'icon':icons[p_name], 'items':items})
        total = round(total, 1)
        rating = 'BUY' if total >= 70 else 'SELL' if total < 45 else 'HOLD'
        pills = [
            {'text': f"ROE {_fmt_backend(roe, '%')}", 'cls': 'good' if (roe or 0) >= 18 else 'warn'},
            {'text': f"CFO/NP {_fmt_backend(cfo_np_ratio, 'x')}", 'cls': 'good' if (cfo_np_ratio or 0) >= 1 else 'warn'},
            {'text': f"D/E {_fmt_backend(de_ratio, 'x')}", 'cls': 'good' if de_ratio is not None and de_ratio <= .5 else 'bad'},
        ]
        details = [{'metric':m['metric'], 'value':_fmt_backend(m['value'], m['suffix']), 'benchmark':m['benchmark'], 'score':f"{m['score']}/10", 'signal':'✓' if m['score']>=7 else '~' if m['score']>=5 else '✗'} for m in score_metrics]

        # Simple CANSLIM and Piotroski models
        cans_criteria = [
            {'criterion':'C - Current EPS/Sales', 'metric':'QoQ Net Profit Growth', 'result':_fmt_backend(growth_rows[2]['qoq_pct'], '%'), 'pass': (growth_rows[2]['qoq_pct'] or 0) > 20},
            {'criterion':'A - Annual earnings', 'metric':'Revenue growth', 'result':_fmt_backend(rev_cagr, '%'), 'pass': (rev_cagr or 0) > 15},
            {'criterion':'N - New high / momentum', 'metric':'1Y Price Momentum', 'result':_fmt_backend(momentum_1y, '%'), 'pass': (momentum_1y or 0) > 20},
            {'criterion':'S - Supply/demand', 'metric':'Latest volume available', 'result': 'Available' if hist is not None and not hist.empty else '—', 'pass': hist is not None and not hist.empty},
            {'criterion':'L - Leader', 'metric':'ROE', 'result':_fmt_backend(roe, '%'), 'pass': (roe or 0) > 18},
            {'criterion':'I - Institutional quality', 'metric':'Market cap/liquidity proxy', 'result':'Pass' if info.get('marketCap') else 'Limited', 'pass': bool(info.get('marketCap'))},
            {'criterion':'M - Market direction', 'metric':'Stock 1Y trend', 'result':_fmt_backend(momentum_1y, '%'), 'pass': (momentum_1y or 0) > 0},
        ]
        cans_score = round(sum(1 for c in cans_criteria if c['pass']) / 7 * 10)
        pio_criteria = [
            {'criterion':'Positive ROA', 'metric':'Net income positive', 'result':'Yes' if (net_income or 0)>0 else 'No', 'pass': (net_income or 0)>0},
            {'criterion':'Positive CFO', 'metric':'Operating cash flow', 'result':_fmt_backend(latest_cfo), 'pass': (latest_cfo or 0)>0},
            {'criterion':'Accrual quality', 'metric':'CFO > Net profit', 'result':_fmt_backend(cfo_np_ratio, 'x'), 'pass': (cfo_np_ratio or 0)>1},
            {'criterion':'Lower leverage', 'metric':'Debt/equity < 1', 'result':_fmt_backend(de_ratio, 'x'), 'pass': de_ratio is not None and de_ratio < 1},
            {'criterion':'Higher liquidity', 'metric':'Current ratio > 1', 'result':_fmt_backend(current_ratio, 'x'), 'pass': (current_ratio or 0)>1},
            {'criterion':'No dilution proxy', 'metric':'Shares info available', 'result':'Check', 'pass': True},
            {'criterion':'Higher margin', 'metric':'Net margin positive', 'result':_fmt_backend(net_margin, '%'), 'pass': (net_margin or 0)>0},
            {'criterion':'Higher turnover', 'metric':'Asset turnover', 'result':_fmt_backend(asset_turnover, 'x'), 'pass': (asset_turnover or 0)>0.5},
            {'criterion':'Profitability quality', 'metric':'ROE > 12%', 'result':_fmt_backend(roe, '%'), 'pass': (roe or 0)>12},
        ]
        pio_score = sum(1 for c in pio_criteria if c['pass'])
        return jsonify({
            'symbol': symbol.upper(),
            'name': info.get('longName') or info.get('shortName') or symbol.upper(),
            'snapshot': {
                'growth': growth_rows,
                'profitability_cashflow': {
                    'operating_margin': operating_margin,
                    'net_margin': net_margin_snapshot,
                    'operating_cash_flow_margin': operating_cf_margin,
                    'free_cash_flow_margin': free_cf_margin,
                    'revenue_volatility': revenue_volatility,
                    'eps_cagr': eps_cagr,
                    'eps_cagr_period': eps_cagr_period
                },
                'growth_quality': {
                    'revenue_growth': revenue_growth_latest,
                    'ebitda_growth': ebitda_growth,
                    'net_income_growth': net_income_growth,
                    'asset_growth': asset_growth
                },
                'cashflow': {'cfo_cagr': cfo_cagr, 'cfo_cagr_period': cfo_period, 'cfo_margin': cfo_margin, 'cfo_np_ratio': cfo_np_ratio, 'roce': roce}
            },
            'score': {'total': total, 'rating': rating, 'pillars': pillars, 'pills': pills, 'details': details, 'canslim': {'score': cans_score, 'criteria': cans_criteria}, 'piotroski': {'score': pio_score, 'criteria': pio_criteria}}
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/chart/<symbol>', methods=['GET'])
def get_chart(symbol):
    period = request.args.get('period', '1mo')
    interval = '1d'
    if period == '1d':
        interval = '5m'
    elif period in ('5d', '1w'):
        period = '5d'
        interval = '30m'
    try:
        rows = _yahoo_chart(symbol, period, interval, use_cache=False).get('rows', [])
        data = []
        for row in rows:
            data.append({
                'date': row.get('date'),
                'open': round(_safe_float(row.get('open')) or _safe_float(row.get('close')) or 0, 2),
                'high': round(_safe_float(row.get('high')) or _safe_float(row.get('close')) or 0, 2),
                'low': round(_safe_float(row.get('low')) or _safe_float(row.get('close')) or 0, 2),
                'close': round(_safe_float(row.get('close')) or 0, 2),
                'volume': _safe_int(row.get('volume'))
            })
        return jsonify(data)
    except Exception:
        try:
            ticker = yf.Ticker(get_nse_ticker(symbol))
            hist = ticker.history(period=period, timeout=YAHOO_TIMEOUT)
            data = []
            for idx, row in hist.iterrows():
                data.append({
                    'date': str(idx.date()),
                    'open': round(float(row['Open']), 2),
                    'high': round(float(row['High']), 2),
                    'low': round(float(row['Low']), 2),
                    'close': round(float(row['Close']), 2),
                    'volume': int(row['Volume'])
                })
            return jsonify(data)
        except Exception as e:
            return jsonify({'error': str(e)}), 500



@app.route('/api/health/yahoo', methods=['GET'])
def yahoo_health():
    """Deployment diagnostic: confirms yfinance/Yahoo access from Vercel."""
    symbol = request.args.get('symbol', 'RELIANCE')
    started = time.time()
    quote_error = None
    chart_error = None
    q = None
    chart_ok = False
    try:
        try:
            q = fetch_quote(symbol)
        except Exception as qe:
            quote_error = str(qe)
        try:
            chart_ok = bool(_yahoo_chart(symbol, '5d', '1d', use_cache=False).get('rows'))
        except Exception as ce:
            chart_error = str(ce)
        return jsonify({
            'ok': bool(q) or chart_ok,
            'method': 'yfinance_serverless',
            'symbol': symbol.upper(),
            'quote': q,
            'quote_error': quote_error,
            'chart_ok': chart_ok,
            'chart_error': chart_error,
            'yfinance_version': getattr(yf, '__version__', 'unknown'),
            'elapsed_ms': int((time.time() - started) * 1000)
        })
    except Exception as e:
        return jsonify({'ok': False, 'method': 'yfinance_serverless', 'symbol': symbol.upper(), 'error': str(e), 'elapsed_ms': int((time.time() - started) * 1000)}), 500


# ─── ADMIN PANEL ─────────────────────────────────────────────────────────────
ADMIN_UID = os.environ.get('APEXWEALTH_ADMIN_UID', 'superuser')
ADMIN_PASSWORD = os.environ.get('APEXWEALTH_ADMIN_PASSWORD', 'June021999')
ADMIN_TOKEN = hashlib.sha256(f'{ADMIN_UID}:{ADMIN_PASSWORD}:apexwealth-admin'.encode()).hexdigest()

def _admin_authorized():
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer ') and auth.split(' ', 1)[1] == ADMIN_TOKEN:
        return True
    token = request.headers.get('X-Admin-Token', '')
    return token == ADMIN_TOKEN

def require_admin():
    if not _admin_authorized():
        return jsonify({'error': 'Admin authorization required'}), 401
    return None

def _user_display(row):
    d = _row_to_dict(row) if isinstance(row, dict) else dict(row)
    return {
        'id': d.get('id'),
        'username': d.get('email'),
        'password': '••••••••',
        'user_since': _dt_to_str(d.get('created')),
        'status': str(d.get('status') or 'APPROVED').upper(),
        'enabled': bool(d.get('is_enabled', True))
    }

def db_admin_list_users(search=''):
    if not init_db():
        raise RuntimeError(DB_LAST_ERROR or 'Database is not initialized')
    like = f'%{search.lower()}%'
    with db_connect() as conn:
        with conn.cursor() as cur:
            if search:
                cur.execute('SELECT id, email, password, created, status, is_enabled FROM users WHERE LOWER(email) LIKE %s ORDER BY created DESC, email', (like,))
            else:
                cur.execute('SELECT id, email, password, created, status, is_enabled FROM users ORDER BY created DESC, email')
            rows = cur.fetchall() or []
    return [_user_display(r) for r in rows]

def db_admin_get_user_by_id(user_id):
    if not init_db():
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id, email, password, created, last_login, status, is_enabled FROM users WHERE id=%s', (user_id,))
            row = cur.fetchone()
    return _row_to_dict(row) if row else None

def db_admin_create_user(username, password):
    username = str(username or '').lower().strip()
    if not username or not password:
        raise ValueError('User Name and Password are required')
    if db_configured():
        if db_get_user(username):
            raise ValueError('User already exists')
        u = db_create_user(username, hash_password(password), status='APPROVED', is_enabled=True)
        user = db_get_user(username)
        return _user_display(user)
    users = load_json(USERS_FILE)
    if username in users:
        raise ValueError('User already exists')
    user_id = str(uuid.uuid4())
    users[username] = {'id': user_id, 'email': username, 'password': hash_password(password), 'created': str(datetime.now()), 'status': 'APPROVED', 'is_enabled': True}
    save_json(USERS_FILE, users)
    return {'id': user_id, 'username': username, 'password': '••••••••', 'user_since': users[username]['created'], 'status': users[username].get('status', 'APPROVED'), 'enabled': bool(users[username].get('is_enabled', True))}

def db_admin_update_user(user_id, username=None, password=None, status=None, enabled=None):
    username = str(username or '').lower().strip() if username is not None else None
    if db_configured():
        if not init_db():
            raise RuntimeError(DB_LAST_ERROR or 'Database is not initialized')
        existing = db_admin_get_user_by_id(user_id)
        if not existing:
            raise ValueError('User not found')
        updates, vals = [], []
        if username:
            duplicate = db_get_user(username)
            if duplicate and duplicate.get('id') != user_id:
                raise ValueError('User Name already exists')
            updates.append('email=%s'); vals.append(username)
        if password:
            updates.append('password=%s'); vals.append(hash_password(password))
        if status is not None:
            status_val = str(status or '').upper().strip()
            if status_val not in ('PENDING', 'APPROVED'):
                raise ValueError('Status must be PENDING or APPROVED')
            updates.append('status=%s'); vals.append(status_val)
        if enabled is not None:
            updates.append('is_enabled=%s'); vals.append(bool(enabled))
        if updates:
            vals.append(user_id)
            with db_connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"UPDATE users SET {', '.join(updates)} WHERE id=%s", vals)
        updated = db_admin_get_user_by_id(user_id)
        return _user_display(updated)
    users = load_json(USERS_FILE)
    found_key = None
    for k, v in users.items():
        if v.get('id') == user_id:
            found_key = k; break
    if not found_key:
        raise ValueError('User not found')
    record = users[found_key]
    if username and username != found_key:
        if username in users:
            raise ValueError('User Name already exists')
        users.pop(found_key)
        record['email'] = username
        found_key = username
        users[found_key] = record
    if password:
        users[found_key]['password'] = hash_password(password)
    if status is not None:
        status_val = str(status or '').upper().strip()
        if status_val not in ('PENDING', 'APPROVED'):
            raise ValueError('Status must be PENDING or APPROVED')
        users[found_key]['status'] = status_val
    if enabled is not None:
        users[found_key]['is_enabled'] = bool(enabled)
    save_json(USERS_FILE, users)
    return {'id': user_id, 'username': found_key, 'password': '••••••••', 'user_since': users[found_key].get('created'), 'status': users[found_key].get('status', 'APPROVED'), 'enabled': bool(users[found_key].get('is_enabled', True))}

def db_admin_delete_user(user_id):
    if db_configured():
        if not init_db():
            raise RuntimeError(DB_LAST_ERROR or 'Database is not initialized')
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM trades WHERE user_id=%s', (user_id,))
                cur.execute('DELETE FROM watchlist WHERE user_id=%s', (user_id,))
                cur.execute('DELETE FROM holdings WHERE user_id=%s', (user_id,))
                cur.execute('DELETE FROM users WHERE id=%s', (user_id,))
                return cur.rowcount
    users = load_json(USERS_FILE)
    portfolios = load_json(PORTFOLIOS_FILE)
    watchlists = load_json(WATCHLISTS_FILE)
    trades = load_json(TRADES_FILE)
    found_key = None
    for k, v in users.items():
        if v.get('id') == user_id:
            found_key = k; break
    if not found_key:
        return 0
    users.pop(found_key, None)
    portfolios.pop(user_id, None); watchlists.pop(user_id, None); trades.pop(user_id, None)
    save_json(USERS_FILE, users); save_json(PORTFOLIOS_FILE, portfolios); save_json(WATCHLISTS_FILE, watchlists); save_json(TRADES_FILE, trades)
    return 1

@app.route('/Admin')
@app.route('/admin')
def admin_page():
    return send_from_directory('..', 'admin.html')

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    data = get_request_json()
    uid = str(data.get('uid', '')).strip()
    password = str(data.get('password', ''))
    if uid == ADMIN_UID and password == ADMIN_PASSWORD:
        return jsonify({'ok': True, 'message': 'Admin login successful', 'token': ADMIN_TOKEN})
    return jsonify({'ok': False, 'error': 'Invalid admin credentials'}), 401

@app.route('/api/admin/users', methods=['GET'])
def admin_list_users():
    auth_error = require_admin()
    if auth_error:
        return auth_error
    try:
        search = request.args.get('search', '').strip()
        users = db_admin_list_users(search) if db_configured() else []
        if not db_configured():
            raw = load_json(USERS_FILE)
            for _, v in raw.items():
                username = v.get('email') or _
                if search and search.lower() not in str(username).lower():
                    continue
                users.append({'id': v.get('id'), 'username': username, 'password': '••••••••', 'user_since': v.get('created'), 'status': v.get('status', 'APPROVED'), 'enabled': bool(v.get('is_enabled', True))})
        return jsonify({'ok': True, 'users': users, 'storage': 'neon' if db_configured() else 'json-fallback'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/admin/users', methods=['POST'])
def admin_create_user():
    auth_error = require_admin()
    if auth_error:
        return auth_error
    data = get_request_json()
    try:
        user = db_admin_create_user(data.get('username'), data.get('password'))
        return jsonify({'ok': True, 'message': 'User created', 'user': user})
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/admin/users/<user_id>', methods=['PUT'])
def admin_update_user(user_id):
    auth_error = require_admin()
    if auth_error:
        return auth_error
    data = get_request_json()
    try:
        user = db_admin_update_user(user_id, data.get('username'), data.get('password'), data.get('status') if 'status' in data else None, data.get('enabled') if 'enabled' in data else None)
        return jsonify({'ok': True, 'message': 'User updated', 'user': user})
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/admin/users/<user_id>', methods=['DELETE'])
def admin_delete_user(user_id):
    auth_error = require_admin()
    if auth_error:
        return auth_error
    try:
        deleted = db_admin_delete_user(user_id)
        if not deleted:
            return jsonify({'ok': False, 'error': 'User not found'}), 404
        return jsonify({'ok': True, 'message': 'User deleted'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ──────────────────────────────────────────────────────────────────────────────
# SCREENER / INTEGRATED STOCK SCANNER
# ──────────────────────────────────────────────────────────────────────────────
SCREENER_DATA_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data', 'ScannerData.xlsx'))

def screener_normalize_symbol(symbol):
    s = str(symbol or '').strip().upper()
    if not s or s in {'NAN', 'NONE'}:
        return ''
    if s.startswith('NSE:'):
        s = s.replace('NSE:', '', 1)
    if '.' not in s:
        s = f'{s}.NS'
    return s

def screener_sheet_names():
    if not os.path.exists(SCREENER_DATA_FILE):
        return []
    try:
        return pd.ExcelFile(SCREENER_DATA_FILE).sheet_names
    except Exception:
        return []

def screener_load_symbols(sheet_name):
    sheets = screener_sheet_names()
    if sheet_name not in sheets:
        raise ValueError(f"Sheet '{sheet_name}' not found")
    df = pd.read_excel(SCREENER_DATA_FILE, sheet_name=sheet_name, header=None, dtype=str)
    raw_values = df.values.ravel().tolist()
    symbols, seen = [], set()
    for value in raw_values:
        sym = screener_normalize_symbol(value)
        if not sym or sym in seen:
            continue
        if len(sym) > 25 or ' ' in sym:
            continue
        symbols.append(sym)
        seen.add(sym)
    return symbols

def screener_ema(prices, period):
    return prices.ewm(span=period, adjust=False).mean()

def screener_rsi(prices, period=14):
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def screener_bollinger_position(close, length=20, std=2.0, mode='volume'):
    if len(close.dropna()) < length:
        return 'NA'
    ma = close.rolling(length).mean()
    sd = close.rolling(length).std()
    lower = ma - std * sd
    upper = ma + std * sd
    middle = ma
    vals = [lower.iloc[-1], middle.iloc[-1], upper.iloc[-1], close.iloc[-1]]
    if any(pd.isna(x) for x in vals):
        return 'NA'
    bb_lower, bb_middle, bb_upper, current_price = map(float, [lower.iloc[-1], middle.iloc[-1], upper.iloc[-1], close.iloc[-1]])
    tolerance = 0.01
    if current_price > (bb_upper + tolerance): return 'Above Band'
    if current_price < (bb_lower - tolerance): return 'Below Band'
    if abs(current_price - bb_upper) <= tolerance: return 'At Upper'
    if abs(current_price - bb_lower) <= tolerance: return 'At Lower'
    if abs(current_price - bb_middle) <= tolerance: return 'At Middle'
    band_width = bb_upper - bb_lower
    if band_width <= 0: return 'Mid Band'
    pct = ((current_price - bb_lower) / band_width) * 100
    if mode == 'priceaction':
        half_upper = bb_middle + 0.5 * (bb_upper - bb_middle)
        half_lower = bb_lower + 0.5 * (bb_middle - bb_lower)
        if current_price > half_upper: return 'Upper Zone'
        if current_price > bb_middle: return 'Above Mid'
        if current_price > half_lower: return 'Below Mid'
        return 'Lower Zone'
    if pct > 75: return 'Upper Band'
    if pct > 60: return 'Above Mid'
    if pct >= 40: return 'Mid Band'
    if pct >= 25: return 'Below Mid'
    return 'Lower Band'

def screener_fetch_history(symbol, interval, period=None, days=None, auto_adjust=False):
    symbol = screener_normalize_symbol(symbol)
    try:
        ticker = yf.Ticker(symbol)
        if period:
            df = ticker.history(period=period, interval=interval, auto_adjust=auto_adjust)
        else:
            end = datetime.now()
            start = end - pd.Timedelta(days=days or 365)
            df = ticker.history(start=start, end=end, interval=interval, auto_adjust=auto_adjust)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={c: str(c).lower() for c in df.columns})
        return df.dropna(subset=['close'])
    except Exception:
        return pd.DataFrame()

def screener_check_ema_symbol(symbol, config):
    timeframe = config.get('timeframe', 'Weekly')
    lookback_days = int(config.get('lookback_days', 20))
    ema1 = int(config.get('ema1', 9)); ema2 = int(config.get('ema2', 18)); ema3 = int(config.get('ema3', 27))
    if timeframe == '60min':
        interval='1h'; periods=lookback_days * 7; days=90
    elif timeframe == 'Daily':
        interval='1d'; periods=lookback_days; days=730
    else:
        interval='1wk'; periods=max(5, int(lookback_days/7)); days=1460
    min_needed = max(30, ema1, ema2, ema3) + 10
    df = screener_fetch_history(symbol, interval=interval, days=days)
    if df.empty or len(df) < min_needed:
        return False, 'Insufficient data'
    close = df['close']
    df[f'ema{ema1}'] = screener_ema(close, ema1)
    df[f'ema{ema2}'] = screener_ema(close, ema2)
    df[f'ema{ema3}'] = screener_ema(close, ema3)
    df['rsi14'] = screener_rsi(close, 14)
    df = df.dropna()
    if len(df) < 10:
        return False, 'Insufficient indicator data'
    recent = df.tail(periods)
    below_all = ((recent['close'] < recent[f'ema{ema1}']) & (recent['close'] < recent[f'ema{ema2}']) & (recent['close'] < recent[f'ema{ema3}']))
    latest = df.iloc[-1]
    current = float(latest['close'])
    above_now = current > float(latest[f'ema{ema1}']) and current > float(latest[f'ema{ema2}']) and current > float(latest[f'ema{ema3}'])
    if not below_all.any() or not above_now:
        return False, 'No bullish EMA reversal'
    return True, {
        'symbol': screener_normalize_symbol(symbol),
        'current_price': round(current, 2),
        'rsi14': round(float(latest['rsi14']), 2),
        'ema1_diff_pct': round(((current - float(latest[f'ema{ema1}'])) / float(latest[f'ema{ema1}'])) * 100, 2),
        'ema2_diff_pct': round(((current - float(latest[f'ema{ema2}'])) / float(latest[f'ema{ema2}'])) * 100, 2),
        'ema3_diff_pct': round(((current - float(latest[f'ema{ema3}'])) / float(latest[f'ema{ema3}'])) * 100, 2),
    }

def screener_prepare_volume_data(df, interval):
    if interval in ['1h','1d']:
        return df
    return df.resample('1h').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna(subset=['high','close'])

def screener_check_volume_symbol(symbol, config):
    interval = config.get('interval', '15m')
    volume_threshold = float(config.get('volume_threshold', 2.0))
    price_threshold = float(config.get('price_threshold', 3.0))
    min_price = float(config.get('min_price', 100.0))
    rsi_threshold = float(config.get('rsi_threshold', 55.0))
    rsi_length = int(config.get('rsi_length', 14))
    period = '3mo' if interval == '1d' else '30d'
    df = screener_fetch_history(symbol, interval=interval, period=period)
    if df.empty:
        return False, 'No data'
    df = screener_prepare_volume_data(df, interval)
    if len(df) < max(21, rsi_length + 5):
        return False, 'Insufficient data'
    prev_5_vol = float(df.iloc[-10:-5]['volume'].mean())
    curr_5_vol = float(df.iloc[-5:]['volume'].mean())
    prev_5_price = float(df.iloc[-10:-5]['close'].mean())
    curr_5_price = float(df.iloc[-5:]['close'].mean())
    current_price = float(df.iloc[-1]['close'])
    if prev_5_vol <= 0 or prev_5_price <= 0:
        return False, 'Zero denominator'
    rsi_series = screener_rsi(df['close'], rsi_length)
    current_rsi = float(rsi_series.iloc[-1])
    if pd.isna(current_rsi):
        return False, 'RSI not available'
    volume_ratio = curr_5_vol / prev_5_vol
    price_change_pct = ((curr_5_price - prev_5_price) / prev_5_price) * 100
    bb_position = screener_bollinger_position(df['close'])
    if volume_ratio >= volume_threshold and price_change_pct >= price_threshold and current_price > min_price and current_rsi > rsi_threshold and curr_5_vol > prev_5_vol:
        return True, {
            'symbol': screener_normalize_symbol(symbol),
            'prev_5_vol': round(prev_5_vol),
            'curr_5_vol': round(curr_5_vol),
            'current_price': round(current_price, 2),
            'volume_ratio': round(volume_ratio, 2),
            'price_change_pct': round(price_change_pct, 2),
            'rsi': round(current_rsi, 1),
            'bb_position': bb_position,
        }
    return False, 'No volume breakout'


def screener_clean_display_symbol(symbol):
    return screener_normalize_symbol(symbol).replace('.NS','').replace('.BO','')

# Opening Range Breakout + OpenHigh/OpenLow
def screener_check_ohl_symbol(symbol):
    df = screener_fetch_history(symbol, interval='1d', period='1mo')
    if df.empty or len(df) < 2:
        return False, 'Insufficient daily data'
    df = df.sort_index()
    latest = df.iloc[-1]
    d_open = float(latest['open']); d_high = float(latest['high']); d_low = float(latest['low']); d_close = float(latest['close'])
    open_hl = '-'; action = '-'
    if abs(d_open - d_high) < 0.05:
        open_hl, action = 'OpenHigh', 'Bearish'
    elif abs(d_open - d_low) < 0.05:
        open_hl, action = 'OpenLow', 'Bullish'
    else:
        return False, 'Neither OpenHigh nor OpenLow'
    rsi = screener_rsi(df['close'], 14).iloc[-1]
    prev_close = float(df.iloc[-2]['close'])
    change_pct = ((d_close - prev_close) / prev_close) * 100 if prev_close else 0
    return True, {
        'result_type': 'ohl', 'symbol': screener_clean_display_symbol(symbol), 'ltp': round(d_close,2),
        'change_pct': round(change_pct,2), 'rsi14': round(float(rsi),2) if not pd.isna(rsi) else None,
        'open_hl': open_hl, 'action_type': action,
    }

def screener_check_orb_symbol(symbol, config):
    start_time = config.get('start_time', '09:15')
    end_time = config.get('end_time', '10:00')
    vol_mult = float(config.get('vol_multiplier', 1.5))
    interval = config.get('interval', '15m')
    df = screener_fetch_history(symbol, interval=interval, period='2d')
    if df.empty or len(df) < 5:
        return False, 'Insufficient intraday data'
    try:
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC').tz_convert('Asia/Kolkata')
        else:
            df.index = df.index.tz_convert('Asia/Kolkata')
    except Exception:
        return False, 'Timezone conversion failed'
    df = df.sort_index()
    last_date = df.index[-1].date()
    today = df[df.index.date == last_date].copy()
    if today.empty:
        return False, 'No current day data'
    try:
        range_data = today.between_time(start_time, end_time)
    except Exception:
        return False, 'Invalid ORB time range'
    if range_data.empty:
        return False, 'Opening range empty'
    opening_high = float(range_data['high'].max()); opening_low = float(range_data['low'].min())
    avg_range_volume = float(range_data['volume'].mean()) or 1.0
    latest = today.iloc[-1]
    current_close = float(latest['close']); current_volume = float(latest['volume'])
    if current_volume <= avg_range_volume * vol_mult:
        return False, 'Volume condition not met'
    signal = None; breakout_level = 0.0
    if current_close > opening_high:
        signal = 'Bullish'; breakout_level = opening_high
    elif current_close < opening_low:
        signal = 'Bearish'; breakout_level = opening_low
    if not signal:
        return False, 'No ORB breakout'
    daily_open = float(today.iloc[0]['open']); daily_high = float(today['high'].max()); daily_low = float(today['low'].min())
    open_status = '-'
    if abs(daily_open - daily_high) < 0.05: open_status = 'OpenHigh'
    elif abs(daily_open - daily_low) < 0.05: open_status = 'OpenLow'
    rsi = screener_rsi(df['close'], 14).iloc[-1]
    prev_close = float(df['close'].iloc[-2]) if len(df) > 1 else current_close
    change_pct = ((current_close - prev_close) / prev_close) * 100 if prev_close else 0
    vol_x = current_volume / avg_range_volume if avg_range_volume else 0
    return True, {
        'result_type': 'orb', 'symbol': screener_clean_display_symbol(symbol), 'signal': signal,
        'breakout_level': round(breakout_level,2), 'ltp': round(current_close,2), 'open_hl': open_status,
        'change_pct': round(change_pct,2), 'rsi14': round(float(rsi),2) if not pd.isna(rsi) else None,
        'vol_x': round(vol_x,1),
    }

def screener_check_orb_app_symbol(symbol, config):
    rows = []
    if bool(config.get('run_ohl', True)):
        matched, details = screener_check_ohl_symbol(symbol)
        if matched and isinstance(details, dict): rows.append(details)
    if bool(config.get('run_orb', True)):
        matched, details = screener_check_orb_symbol(symbol, config)
        if matched and isinstance(details, dict): rows.append(details)
    return rows

SCREENER_INTRADAY_MAP = {'5 minute':'5m', '15 minute':'15m', '60 minute':'60m'}
SCREENER_OPS = {'<': operator.lt, '<=': operator.le, '=': lambda a,b: abs(a-b)<1e-9, '>=': operator.ge, '>': operator.gt}

def screener_candle_value(offset_str, period, value_type, data_d, data_w, data_m, intraday_data):
    try:
        offset = int(str(offset_str).split(' ')[0])
        if period in SCREENER_INTRADAY_MAP:
            df = intraday_data.get(SCREENER_INTRADAY_MAP[period])
        elif period == 'Day': df = data_d
        elif period == 'Week': df = data_w
        else: df = data_m
        if df is None or df.empty: return None
        idx = offset - 1
        if abs(idx) > len(df.index): return None
        col = str(value_type).lower()
        if col not in df.columns: return None
        return float(df[col].iloc[idx])
    except Exception:
        return None

def screener_check_priceaction_symbol(symbol, config):
    conditions = [c for c in config.get('conditions', []) if c.get('active')]
    if not conditions:
        return False, 'No active conditions'
    required_intraday = {SCREENER_INTRADAY_MAP[p] for cond in conditions for p in [cond.get('period1'), cond.get('period2')] if p in SCREENER_INTRADAY_MAP}
    data_d = screener_fetch_history(symbol, interval='1d', period='1y', auto_adjust=True)
    if data_d.empty or len(data_d) < 21:
        return False, 'Insufficient daily data'
    data_w = screener_fetch_history(symbol, interval='1wk', period='5y', auto_adjust=True)
    data_m = screener_fetch_history(symbol, interval='1mo', period='5y', auto_adjust=True)
    if data_w.empty or data_m.empty:
        return False, 'Insufficient weekly/monthly data'
    intraday_data = {}
    for interval in required_intraday:
        intraday_data[interval] = screener_fetch_history(symbol, interval=interval, period='60d', auto_adjust=True)
    for cond in conditions:
        val1 = screener_candle_value(cond.get('offset1','0 (current)'), cond.get('period1','Day'), cond.get('value1','CLOSE'), data_d, data_w, data_m, intraday_data)
        val2 = screener_candle_value(cond.get('offset2','-1 (ago)'), cond.get('period2','Month'), cond.get('value2','HIGH'), data_d, data_w, data_m, intraday_data)
        op = SCREENER_OPS.get(cond.get('operator','<'))
        if val1 is None or val2 is None or op is None or not op(val1, val2):
            return False, 'Conditions not met'
    close = data_d['close']; latest_d = data_d.iloc[-1]; latest_w = data_w.iloc[-1]; latest_m = data_m.iloc[-1]
    rsi = screener_rsi(close, 14).iloc[-1]
    bb_pos = screener_bollinger_position(close, mode='priceaction')
    ltp = float(latest_d['close'])
    prev_d = float(data_d['close'].iloc[-2]) if len(data_d) > 1 else ltp
    prev_w = float(data_w['close'].iloc[-2]) if len(data_w) > 1 else ltp
    prev_m = float(data_m['close'].iloc[-2]) if len(data_m) > 1 else ltp
    volume = int(latest_d.get('volume', 0))
    prev_10_vol_max = float(data_d['volume'].iloc[-11:-1].max()) if 'volume' in data_d.columns and len(data_d) >= 11 else 0
    return True, {
        'symbol': screener_normalize_symbol(symbol), 'ltp': round(ltp,2),
        'change_pct': round(((ltp-prev_d)/prev_d)*100,2) if prev_d else 0,
        'rsi_val': round(float(rsi),2) if not pd.isna(rsi) else None, 'bb_pos': bb_pos,
        'd_close_pct': round(((ltp-prev_d)/prev_d)*100,2) if prev_d else 0,
        'w_close_pct': round(((ltp-prev_w)/prev_w)*100,2) if prev_w else 0,
        'm_close_pct': round(((ltp-prev_m)/prev_m)*100,2) if prev_m else 0,
        'volume': volume, 'vol10day_high': bool(volume > prev_10_vol_max) if prev_10_vol_max else False,
    }


# ─── Big Order Scanner ───────────────────────────────────────────────────────
BIG_ORDER_TIMEFRAMES = {
    "15min": {"interval": "15m", "period": "60d"},
    "30min": {"interval": "30m", "period": "60d"},
    "60min": {"interval": "60m", "period": "180d"},
    "1day": {"interval": "1d", "period": "1y"},
}
BIG_ORDER_PCT_MEDIUM = 90.0
BIG_ORDER_PCT_LARGE = 97.0
BIG_ORDER_MIN_HISTORY_BARS = 50
BIG_ORDER_LOOKBACK_BARS_TO_CHECK = 3
BIG_ORDER_REL_VOLUME_WINDOW = 20
BIG_ORDER_MIN_AVG_DOLLAR_VOLUME_20 = 5_00_00_000
BIG_ORDER_REL_VOLUME_SCORE_CAP = 5.0
BIG_ORDER_LIQUIDITY_SCORE_CAP = 25_00_00_000

def _big_order_previous_average(series, window):
    return series.shift(1).rolling(window=window, min_periods=window).mean()

def _big_order_safe_ratio(numerator, denominator):
    denominator = denominator.replace(0, np.nan)
    return (numerator / denominator).replace([np.inf, -np.inf], np.nan)

def _big_order_normalize_100(value, cap):
    if pd.isna(value) or cap <= 0:
        return 0.0
    return max(0.0, min((float(value) / cap) * 100.0, 100.0))

def _big_order_composite_score(percentile, rel_volume_20, avg_dollar_volume_20, bars_ago):
    percentile_score = max(0.0, min(float(percentile), 100.0))
    rel_volume_score = _big_order_normalize_100(rel_volume_20, BIG_ORDER_REL_VOLUME_SCORE_CAP)
    liquidity_score = _big_order_normalize_100(avg_dollar_volume_20, BIG_ORDER_LIQUIDITY_SCORE_CAP)
    max_age = max(BIG_ORDER_LOOKBACK_BARS_TO_CHECK - 1, 1)
    recency_score = max(0.0, 100.0 * (1.0 - (bars_ago / max_age)))
    return round((0.40 * percentile_score) + (0.30 * rel_volume_score) + (0.20 * liquidity_score) + (0.10 * recency_score), 2)

def _big_order_size_label(percentile):
    if percentile >= BIG_ORDER_PCT_LARGE:
        return 'Large'
    if percentile >= BIG_ORDER_PCT_MEDIUM:
        return 'Medium'
    return 'Small'

def _big_order_time_since(bar_time):
    try:
        now = pd.Timestamp.now(tz=bar_time.tzinfo) if getattr(bar_time, 'tzinfo', None) else pd.Timestamp.now()
        delta = now - bar_time
        total_minutes = int(delta.total_seconds() // 60)
        if total_minutes <= 0:
            return 'Now'
        days, rem = divmod(total_minutes, 1440)
        hours, minutes = divmod(rem, 60)
        if days > 0:
            return f'{days}D'
        if hours > 0:
            return f'{hours}h{minutes}m'
        return f'{minutes}m' if minutes > 0 else 'Now'
    except Exception:
        return ''

def screener_check_bigorder_symbol(symbol, config=None):
    """Big Order Scanner adapted from the attached Flask app, using ScannerData.xlsx symbols."""
    config = config or {}
    selected_tfs = config.get('timeframes') or list(BIG_ORDER_TIMEFRAMES.keys())
    
    try:
        min_score = float(config.get('min_score')) if config.get('min_score') not in ('', None) else None
    except Exception:
        min_score = None
    size_filter = str(config.get('size', 'All') or 'All')
    side_filter = str(config.get('side', 'All') or 'All')
    symbol = screener_normalize_symbol(symbol)
    rows = []
    for tf_label, params in BIG_ORDER_TIMEFRAMES.items():
        if tf_label not in selected_tfs:
            continue
        df = screener_fetch_history(symbol, interval=params['interval'], period=params['period'], auto_adjust=False)
        if df is None or df.empty:
            continue
        # screener_fetch_history returns lowercase OHLCV columns.
        df = df.rename(columns={'open':'Open','high':'High','low':'Low','close':'Close','volume':'Volume'}).copy()
        df = df.dropna(subset=['Close', 'Volume'])
        df = df[df['Volume'] > 0]
        if len(df) > 1:
            df = df.iloc[:-1].copy()
        min_required = max(BIG_ORDER_MIN_HISTORY_BARS, BIG_ORDER_REL_VOLUME_WINDOW + BIG_ORDER_LOOKBACK_BARS_TO_CHECK + 1)
        if len(df) < min_required:
            continue
        raw_volume = df['Volume']
        dollar_metric = df['Volume'] * df['Close']
        vol_metric = dollar_metric
        direction = np.sign(df['Close'].diff()).fillna(0)
        pct_rank = vol_metric.rank(pct=True) * 100.0
        avg_volume_20 = _big_order_previous_average(raw_volume, BIG_ORDER_REL_VOLUME_WINDOW)
        rel_volume_20 = _big_order_safe_ratio(raw_volume, avg_volume_20)
        avg_dollar_volume_20 = _big_order_previous_average(dollar_metric, BIG_ORDER_REL_VOLUME_WINDOW)
        rel_dollar_volume_20 = _big_order_safe_ratio(dollar_metric, avg_dollar_volume_20)
        n = len(df)
        check_n = min(BIG_ORDER_LOOKBACK_BARS_TO_CHECK, n)
        for i in range(n - check_n, n):
            pct = float(pct_rank.iloc[i])
            size_label = _big_order_size_label(pct)
            if size_label == 'Small':
                continue
            avg_vol = float(avg_volume_20.iloc[i]) if pd.notna(avg_volume_20.iloc[i]) else np.nan
            rel_vol = float(rel_volume_20.iloc[i]) if pd.notna(rel_volume_20.iloc[i]) else np.nan
            avg_dollar_vol = float(avg_dollar_volume_20.iloc[i]) if pd.notna(avg_dollar_volume_20.iloc[i]) else np.nan
            rel_dollar_vol = float(rel_dollar_volume_20.iloc[i]) if pd.notna(rel_dollar_volume_20.iloc[i]) else np.nan
            if pd.isna(avg_dollar_vol) or avg_dollar_vol < BIG_ORDER_MIN_AVG_DOLLAR_VOLUME_20:
                continue
            bars_ago = n - 1 - i
            score = _big_order_composite_score(pct, rel_vol, avg_dollar_vol, bars_ago)
            side = {1: 'Long', -1: 'Short', 0: 'Flat'}.get(int(direction.iloc[i]), 'Flat')
            if size_filter != 'All' and size_filter != size_label:
                continue
            if side_filter != 'All' and side_filter != side:
                continue
            if min_score is not None and score < min_score:
                continue
            price = float(df['Close'].iloc[i])
            raw_vol = float(df['Volume'].iloc[i])
            ticker_display = screener_clean_display_symbol(symbol)
            rows.append({
                'symbol': ticker_display,
                'Ticker': ticker_display,
                'timeframe': tf_label,
                'Timeframe': tf_label,
                'side': side,
                'Side': side,
                'size': size_label,
                'Size': size_label,
                'composite_score': round(score, 2),
                'CompositeScore': round(score, 2),
                'percentile': round(pct, 2),
                'Percentile': round(pct, 2),
                'rel_volume_20': round(rel_vol, 2) if pd.notna(rel_vol) else '',
                'RelVolume20': round(rel_vol, 2) if pd.notna(rel_vol) else '',
                'price': round(price, 2),
                'Price': round(price, 2),
                'volume': int(raw_vol),
                'Volume': int(raw_vol),
                'dollar_volume': round(raw_vol * price, 2),
                'avg_dollar_volume_20': round(avg_dollar_vol, 2) if pd.notna(avg_dollar_vol) else '',
                'rel_dollar_volume_20': round(rel_dollar_vol, 2) if pd.notna(rel_dollar_vol) else '',
                'bar_time': str(df.index[i]),
                'time_since': _big_order_time_since(df.index[i]),
                'TimeSince': _big_order_time_since(df.index[i]),
            })
    rows.sort(key=lambda r: (float(r.get('CompositeScore') or 0), float(r.get('Percentile') or 0)), reverse=True)
    return rows

def screener_scan_symbol(scanner, symbol, config):
    if scanner == 'ema':
        matched, details = screener_check_ema_symbol(symbol, config)
        return [details] if matched and isinstance(details, dict) else []
    if scanner == 'volume':
        matched, details = screener_check_volume_symbol(symbol, config)
        return [details] if matched and isinstance(details, dict) else []
    if scanner == 'orb':
        return screener_check_orb_app_symbol(symbol, config)
    if scanner == 'priceaction':
        matched, details = screener_check_priceaction_symbol(symbol, config)
        return [details] if matched and isinstance(details, dict) else []
    if scanner == 'bigorder':
        return screener_check_bigorder_symbol(symbol, config)
    raise ValueError('Unknown scanner')

def screener_iter_scan_events(sheet_name, scanner, config, max_symbols=None):
    symbols = screener_load_symbols(sheet_name)
    if max_symbols:
        symbols = symbols[:int(max_symbols)]
    total = len(symbols); matches = 0; errors = []
    def event(payload):
        return json.dumps(payload, default=str) + '\n'
    yield event({'type':'start','sheet':sheet_name,'scanner':scanner,'total':total,'scanned':0,'matches':0,'percent':0,'symbol':''})
    for i, symbol in enumerate(symbols, start=1):
        yield event({'type':'progress','symbol':symbol,'scanned':i-1,'total':total,'matches':matches,'percent':round((i-1)/total*100,2) if total else 100,'message':f'Scanning {symbol} ({i}/{total})'})
        try:
            for details in screener_scan_symbol(scanner, symbol, config):
                matches += 1
                yield event({'type':'result','symbol':symbol,'row':details,'scanned':i,'total':total,'matches':matches,'percent':round(i/total*100,2) if total else 100})
        except Exception as exc:
            errors.append({'symbol':symbol, 'error':str(exc)[:120]})
        yield event({'type':'progress','symbol':symbol,'scanned':i,'total':total,'matches':matches,'percent':round(i/total*100,2) if total else 100,'message':f'Completed {symbol} ({i}/{total})'})
    yield event({'type':'done','sheet':sheet_name,'scanner':scanner,'scanned':total,'total':total,'matches':matches,'percent':100,'errors':errors[:25],'message':f'Scan completed: {matches} matches from {total} symbols.'})

@app.route('/api/screener/sheets', methods=['GET'])
def api_screener_sheets():
    return jsonify({'sheets': screener_sheet_names(), 'data_file': os.path.basename(SCREENER_DATA_FILE), 'exists': os.path.exists(SCREENER_DATA_FILE)})

@app.route('/api/screener/symbols', methods=['GET'])
def api_screener_symbols():
    sheet = request.args.get('sheet', '')
    try:
        symbols = screener_load_symbols(sheet)
        return jsonify({'sheet': sheet, 'count': len(symbols), 'symbols': symbols[:1000]})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400

@app.route('/api/screener/scan_stream/<scanner>', methods=['POST'])
def api_screener_scan_stream(scanner):
    if scanner not in {'ema', 'volume', 'orb', 'priceaction', 'bigorder'}:
        return jsonify({'error': "scanner must be one of: ema, volume, orb, priceaction, bigorder"}), 400
    payload = get_request_json()
    sheet = payload.get('sheet')
    config = payload.get('config', {})
    max_symbols = payload.get('max_symbols')
    if max_symbols in ('', None):
        max_symbols = None
    try:
        return Response(
            screener_iter_scan_events(sheet, scanner, config, max_symbols),
            mimetype='application/x-ndjson',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
        )
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


if __name__ == '__main__':
    app.run(debug=True, port=5000)

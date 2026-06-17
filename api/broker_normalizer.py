"""
Reliable multi-broker tradebook / P&L normalizer.

Handles:
- CSV files with metadata before the real header row, e.g. Dhan P&L reports.
- Different broker strings/headers using alias scoring instead of fixed broker branches.
- Tradebooks and P&L/holding-style files that contain Buy Qty / Sell Qty columns.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

CANONICAL_COLUMNS = [
    "symbol",
    "isin",
    "trade_date",
    "trade_type",
    "quantity",
    "price",
    "value",
    "broker",
    "source_format",
    "raw_symbol",
]

COLUMN_ALIASES: Dict[str, List[str]] = {
    "symbol": [
        "symbol", "trading symbol", "tradingsymbol", "scrip", "scrip name", "scrip_name",
        "stock", "stock name", "stock_name", "instrument", "instrument name", "security name",
        "company", "company name", "name", "contract", "script name", "scheme name",
    ],
    "isin": ["isin", "isin code", "isin_code", "isin no", "isin number"],
    "trade_date": [
        "trade date", "trade_date", "date", "order date", "order_date", "transaction date",
        "transaction_date", "execution date", "trade time", "exchange timestamp", "order execution time",
        "order_execution_time", "created at", "timestamp",
    ],
    "trade_type": [
        "trade type", "trade_type", "transaction type", "transaction_type", "type", "side",
        "buy/sell", "buy sell", "action", "order type", "trade side", "txn type",
    ],
    "quantity": [
        "quantity", "qty", "trade qty", "trade_qty", "filled qty", "executed qty", "executed quantity",
        "order qty", "order quantity", "buy qty", "buy qty.", "sell qty", "sell qty.", "net qty", "net quantity",
    ],
    "price": [
        "price", "avg price", "avg_price", "average price", "average_price", "trade price",
        "execution price", "rate", "avg. buy price", "avg buy price", "avg. sell price", "avg sell price",
    ],
    "value": [
        "value", "trade value", "trade_value", "turnover", "amount", "net amount", "net_amount",
        "total value", "total_value", "buy value", "sell value", "contract value", "gross amount",
    ],
}

BROKER_HINTS: Dict[str, List[str]] = {
    "zerodha": ["symbol", "trade_type", "auction", "order_execution_time", "kite", "zerodha"],
    "dhan": ["dhan", "pnl report", "ucc", "scrip name", "buy qty.", "avg. buy price", "realised p&l"],
    "angelone": ["angel", "angel one", "scrip", "transaction type", "net amount"],
    "groww": ["groww", "stock name", "order date", "average price", "total value"],
    "upstox": ["upstox", "instrument", "transaction type", "trade value"],
    "icici": ["icici", "icicidirect", "order ref", "exchange order no"],
    "kotak": ["kotak", "settlement", "contract note"],
}

BUY_WORDS = {
    "b", "buy", "buying", "bought", "purchase", "purchased", "long", "debit",
}
SELL_WORDS = {
    "s", "sell", "selling", "sold", "sale", "short", "credit",
}

DATE_FORMATS = [
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y",
    "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
]


@dataclass
class ParseResult:
    trades: pd.DataFrame
    detected_broker: str
    source_format: str
    header_row: int
    mapping: Dict[str, str]
    warnings: List[str]


def clean_header(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\ufeff", "").strip().lower()
    text = re.sub(r"[\n\r\t]+", " ", text)
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def compact_header(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_header(value))


def normalize_columns(columns: Iterable[object]) -> List[str]:
    seen: Dict[str, int] = {}
    out: List[str] = []
    for col in columns:
        base = clean_header(col) or "blank"
        count = seen.get(base, 0)
        seen[base] = count + 1
        out.append(base if count == 0 else f"{base}_{count + 1}")
    return out


def numeric_clean(series: pd.Series) -> pd.Series:
    cleaned = (
        series.astype(str)
        .str.replace("₹", "", regex=False)
        .str.replace(",", "", regex=False)
        .str.replace("--", "", regex=False)
        .str.replace("-", "-", regex=False)
        .str.strip()
    )
    # Convert values like (123.45) into -123.45
    cleaned = cleaned.str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    return pd.to_numeric(cleaned, errors="coerce")


def parse_date_series(series: pd.Series) -> pd.Series:
    """Parse broker dates without turning ISO dates like 2025-03-12 into 2025-12-03."""
    values = series.astype(str).str.strip()
    parsed_values = []
    for value in values:
        if not value or value.lower() in {"nan", "none", "nat"}:
            parsed_values.append(pd.NaT)
            continue
        # ISO / API timestamps are normally year-first. Indian contract notes are often day-first.
        year_first = bool(re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", value))
        parsed = pd.to_datetime(value, errors="coerce", dayfirst=not year_first)
        if pd.isna(parsed):
            parsed = pd.to_datetime(value, errors="coerce", dayfirst=False)
        parsed_values.append(parsed)
    parsed_series = pd.Series(parsed_values, index=series.index)
    return parsed_series.dt.date.astype("string").replace("<NA>", "")


def detect_header_row(path: Path, max_scan_rows: int = 40) -> Tuple[int, List[str]]:
    alias_compacts = {compact_header(a) for aliases in COLUMN_ALIASES.values() for a in aliases}
    best_row = 0
    best_score = -1
    best_cells: List[str] = []

    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
        reader = csv.reader(f)
        for idx, row in enumerate(reader):
            if idx >= max_scan_rows:
                break
            cells = [clean_header(c) for c in row]
            compact_cells = [compact_header(c) for c in cells]
            score = 0
            score += sum(1 for c in compact_cells if c in alias_compacts)
            score += sum(1 for c in compact_cells if any(k in c for k in ["symbol", "scrip", "isin", "quantity", "qty", "price", "value", "date"]))
            score += 3 if len([c for c in cells if c]) >= 5 else 0
            if score > best_score:
                best_row = idx
                best_score = score
                best_cells = cells

    return best_row, best_cells


def read_csv_reliably(path: str | Path) -> Tuple[pd.DataFrame, int, List[str]]:
    path = Path(path)
    header_row, header_cells = detect_header_row(path)
    read_attempts = [
        dict(skiprows=header_row, engine="python"),
        dict(skiprows=header_row, sep=None, engine="python"),
        dict(engine="python"),
    ]
    last_error: Optional[Exception] = None
    for kwargs in read_attempts:
        try:
            df = pd.read_csv(path, **kwargs)
            df = df.dropna(how="all")
            df.columns = normalize_columns(df.columns)
            return df, header_row, header_cells
        except Exception as exc:  # pragma: no cover - used for messy real broker exports
            last_error = exc
    raise ValueError(f"Unable to read CSV file: {last_error}")


def score_broker(text: str, broker: str) -> int:
    return sum(1 for hint in BROKER_HINTS.get(broker, []) if hint in text)


def detect_broker(df: pd.DataFrame, header_cells: List[str], requested: str = "auto") -> str:
    requested = (requested or "auto").strip().lower()
    if requested != "auto":
        return requested
    parts = list(df.columns) + header_cells + df.head(3).astype(str).values.flatten().tolist()
    text = " ".join("" if pd.isna(x) else str(x) for x in parts).lower()
    scores = {broker: score_broker(text, broker) for broker in BROKER_HINTS}
    detected, score = max(scores.items(), key=lambda item: item[1])
    return detected if score > 0 else "generic"


def build_mapping(df: pd.DataFrame) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    col_compact = {col: compact_header(col) for col in df.columns}

    for canonical, aliases in COLUMN_ALIASES.items():
        alias_compacts = [compact_header(a) for a in aliases]
        exact = [col for col, comp in col_compact.items() if comp in alias_compacts]
        if exact:
            mapping[canonical] = exact[0]
            continue
        partial = [
            col for col, comp in col_compact.items()
            if any(alias in comp or comp in alias for alias in alias_compacts if len(alias) >= 4)
        ]
        if partial:
            mapping[canonical] = partial[0]
    return mapping


def normalize_trade_type(series: pd.Series) -> pd.Series:
    cleaned = series.astype(str).str.strip().str.lower().str.replace(r"[^a-z]", "", regex=True)
    def _map(v: str) -> str:
        if v in BUY_WORDS or "buy" in v or "purchase" in v:
            return "BUY"
        if v in SELL_WORDS or "sell" in v or "sold" in v or "sale" in v:
            return "SELL"
        return ""
    return cleaned.map(_map)


def make_standard_row_df(df: pd.DataFrame, mapping: Dict[str, str], broker: str) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    for col in CANONICAL_COLUMNS:
        out[col] = ""

    for canonical, source in mapping.items():
        if canonical in out.columns and source in df.columns:
            out[canonical] = df[source]

    out["broker"] = broker
    out["source_format"] = "tradebook"
    out["raw_symbol"] = out["symbol"]

    if "trade_type" in out:
        out["trade_type"] = normalize_trade_type(out["trade_type"])
    out["quantity"] = numeric_clean(out["quantity"]).fillna(0)
    out["price"] = numeric_clean(out["price"]).fillna(0.0)
    out["value"] = numeric_clean(out["value"]).fillna(0.0)

    zero_value = (out["value"] == 0) & (out["quantity"] > 0) & (out["price"] > 0)
    out.loc[zero_value, "value"] = out.loc[zero_value, "quantity"] * out.loc[zero_value, "price"]

    if "trade_date" in out.columns:
        out["trade_date"] = parse_date_series(out["trade_date"])

    out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.(NS|BO)$", "", regex=True)
    out = out[(out["symbol"] != "") & (out["quantity"] > 0)]
    return out[CANONICAL_COLUMNS]


def make_pnl_expanded_df(df: pd.DataFrame, broker: str) -> pd.DataFrame:
    """Expand P&L files with Buy Qty/Buy Value and Sell Qty/Sell Value into trade rows."""
    col_lookup = {compact_header(c): c for c in df.columns}
    def col(*names: str) -> Optional[str]:
        for name in names:
            key = compact_header(name)
            if key in col_lookup:
                return col_lookup[key]
        return None

    symbol_col = col("scrip name", "scrip", "symbol", "stock name", "instrument")
    isin_col = col("isin code", "isin")
    buy_qty_col = col("buy qty.", "buy qty", "buy quantity")
    buy_price_col = col("avg. buy price", "avg buy price", "average buy price")
    buy_value_col = col("buy value")
    sell_qty_col = col("sell qty.", "sell qty", "sell quantity")
    sell_price_col = col("avg. sell price", "avg sell price", "average sell price")
    sell_value_col = col("sell value")

    rows: List[Dict[str, object]] = []
    report_date = ""
    for _, r in df.iterrows():
        symbol = str(r.get(symbol_col, "")).strip() if symbol_col else ""
        if not symbol or symbol.lower().startswith("net p&l"):
            continue
        isin = str(r.get(isin_col, "")).strip() if isin_col else ""
        bq = numeric_clean(pd.Series([r.get(buy_qty_col, 0) if buy_qty_col else 0])).iloc[0]
        sq = numeric_clean(pd.Series([r.get(sell_qty_col, 0) if sell_qty_col else 0])).iloc[0]
        if pd.notna(bq) and bq > 0:
            price = numeric_clean(pd.Series([r.get(buy_price_col, 0) if buy_price_col else 0])).iloc[0]
            value = numeric_clean(pd.Series([r.get(buy_value_col, 0) if buy_value_col else 0])).iloc[0]
            rows.append({
                "symbol": symbol, "isin": isin, "trade_date": report_date, "trade_type": "BUY",
                "quantity": float(bq), "price": float(price or 0), "value": float(value or (bq * (price or 0))),
                "broker": broker, "source_format": "pnl_expanded", "raw_symbol": symbol,
            })
        if pd.notna(sq) and sq > 0:
            price = numeric_clean(pd.Series([r.get(sell_price_col, 0) if sell_price_col else 0])).iloc[0]
            value = numeric_clean(pd.Series([r.get(sell_value_col, 0) if sell_value_col else 0])).iloc[0]
            rows.append({
                "symbol": symbol, "isin": isin, "trade_date": report_date, "trade_type": "SELL",
                "quantity": float(sq), "price": float(price or 0), "value": float(value or (sq * (price or 0))),
                "broker": broker, "source_format": "pnl_expanded", "raw_symbol": symbol,
            })

    out = pd.DataFrame(rows, columns=CANONICAL_COLUMNS)
    if not out.empty:
        out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.(NS|BO)$", "", regex=True)
    return out


def is_pnl_style(df: pd.DataFrame) -> bool:
    compact_cols = {compact_header(c) for c in df.columns}
    return bool({"buyqty", "buyqty.", "buyvalue"} & compact_cols) and bool({"sellqty", "sellqty.", "sellvalue"} & compact_cols)


def normalize_file(path: str | Path, broker: str = "auto") -> ParseResult:
    warnings: List[str] = []
    raw_df, header_row, header_cells = read_csv_reliably(path)
    detected_broker = detect_broker(raw_df, header_cells, broker)
    mapping = build_mapping(raw_df)

    if is_pnl_style(raw_df):
        trades = make_pnl_expanded_df(raw_df, detected_broker)
        source_format = "pnl_expanded"
        if trades.empty:
            warnings.append("Detected a P&L style file but could not expand buy/sell rows.")
    else:
        trades = make_standard_row_df(raw_df, mapping, detected_broker)
        source_format = "tradebook"
        if trades["trade_type"].eq("").any() if not trades.empty else False:
            warnings.append("Some rows have unknown trade type. Add the broker string to BUY_WORDS/SELL_WORDS if needed.")

    if trades.empty:
        warnings.append("No usable trade rows found. Check if the file is a tradebook or add column aliases.")

    return ParseResult(
        trades=trades,
        detected_broker=detected_broker,
        source_format=source_format,
        header_row=header_row + 1,
        mapping=mapping,
        warnings=warnings,
    )

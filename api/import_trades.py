from __future__ import annotations

import math
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Tuple

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if pd is not None and pd.isna(value):
            return default
        text = str(value).replace(',', '').strip()
        if not text or text.lower() in {'nan', 'none', 'nat'}:
            return default
        return float(text)
    except Exception:
        return default


def _clean_symbol(symbol: object) -> str:
    text = '' if symbol is None else str(symbol).strip().upper()
    text = text.replace('NSE:', '').replace('BSE:', '').replace('NSE_', '').replace('BSE_', '')
    for suffix in ('.NS', '.BO', '-EQ', ' EQ'):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
    return text.strip()


def _date_key(value: object):
    text = '' if value is None else str(value).strip()
    if not text or text.lower() in {'nan', 'none', 'nat'}:
        return datetime.max
    try:
        if pd is not None:
            dt = pd.to_datetime(text, errors='coerce', dayfirst=True)
            if not pd.isna(dt):
                return dt.to_pydatetime()
    except Exception:
        pass
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(text[:10], fmt)
        except Exception:
            continue
    return datetime.max


def _trade_rows(trades) -> List[dict]:
    if trades is None:
        return []
    if hasattr(trades, 'to_dict'):
        return trades.to_dict('records')
    return list(trades)


def match_fifo_to_apex(
    normalized_trades,
    meta_lookup: Callable[[str], Dict[str, str]] | None = None,
    aggregate_holdings: bool = True,
) -> Tuple[List[dict], List[dict], List[str]]:
    """Convert normalized BUY/SELL rows into ApexWealth holdings and trades.

    Holdings are aggregated one row per symbol by weighted average buy price.
    Closed trades are kept as FIFO matched lots so the Performance Report can show
    each realized P&L row with the same shape as ApexWealth's sell_holding route.
    """
    rows = _trade_rows(normalized_trades)
    warnings: List[str] = []
    if not rows:
        return [], [], ['No normalized trades available for FIFO matching.']

    by_symbol: Dict[str, List[dict]] = {}
    skipped_unknown = 0
    skipped_qty = 0
    for r in rows:
        symbol = _clean_symbol(r.get('symbol'))
        side = str(r.get('trade_type') or '').upper().strip()
        qty = _safe_float(r.get('quantity'))
        price = _safe_float(r.get('price'))
        value = _safe_float(r.get('value'))
        if not symbol:
            continue
        if side not in {'BUY', 'SELL'}:
            skipped_unknown += 1
            continue
        if qty <= 0:
            skipped_qty += 1
            continue
        if price <= 0 and value > 0:
            price = value / qty
        if price <= 0:
            skipped_qty += 1
            continue
        rr = dict(r)
        rr.update({'symbol': symbol, 'trade_type': side, 'quantity': qty, 'price': price})
        by_symbol.setdefault(symbol, []).append(rr)

    if skipped_unknown:
        warnings.append(f'{skipped_unknown} rows skipped — unknown trade type.')
    if skipped_qty:
        warnings.append(f'{skipped_qty} rows skipped — missing/invalid quantity or price.')

    open_lots: List[dict] = []
    closed: List[dict] = []

    for symbol, sym_rows in sorted(by_symbol.items()):
        sym_rows.sort(key=lambda r: (_date_key(r.get('trade_date')), 0 if r.get('trade_type') == 'BUY' else 1))
        lots: List[dict] = []
        meta = meta_lookup(symbol) if meta_lookup else {}
        name = meta.get('name') or symbol
        industry = meta.get('industry') or meta.get('sector') or ''
        sector = meta.get('sector') or ''
        unmatched_sell_qty = 0.0

        for r in sym_rows:
            side = r.get('trade_type')
            qty = _safe_float(r.get('quantity'))
            price = _safe_float(r.get('price'))
            tdate = str(r.get('trade_date') or '').strip()
            if side == 'BUY':
                lots.append({'qty': qty, 'price': price, 'date': tdate})
                continue

            sell_qty_left = qty
            sell_price = price
            while sell_qty_left > 1e-12 and lots:
                lot = lots[0]
                lot_qty = _safe_float(lot.get('qty'))
                matched_qty = min(sell_qty_left, lot_qty)
                buy_price = _safe_float(lot.get('price'))
                invested = matched_qty * buy_price
                pnl = (sell_price - buy_price) * matched_qty
                closed.append({
                    'symbol': symbol,
                    'name': name,
                    'buy_price': round(buy_price, 4),
                    'sell_price': round(sell_price, 4),
                    'qty': round(matched_qty, 6),
                    'buy_date': str(lot.get('date') or ''),
                    'sell_date': tdate,
                    'pnl': round(pnl, 2),
                    'pnl_pct': round((pnl / invested) * 100, 2) if invested else 0,
                })
                lot['qty'] = lot_qty - matched_qty
                sell_qty_left -= matched_qty
                if lot['qty'] <= 1e-12:
                    lots.pop(0)
            if sell_qty_left > 1e-12:
                unmatched_sell_qty += sell_qty_left

        if unmatched_sell_qty > 1e-12:
            warnings.append(f'{symbol}: {unmatched_sell_qty:g} sell quantity could not be matched to earlier buys.')

        if aggregate_holdings:
            rem_lots = [l for l in lots if _safe_float(l.get('qty')) > 1e-12]
            total_qty = sum(_safe_float(l.get('qty')) for l in rem_lots)
            invested = sum(_safe_float(l.get('qty')) * _safe_float(l.get('price')) for l in rem_lots)
            if total_qty > 1e-12:
                dated = [str(l.get('date') or '').strip() for l in rem_lots if str(l.get('date') or '').strip()]
                open_lots.append({
                    'symbol': symbol,
                    'name': name,
                    'buy_price': round(invested / total_qty, 4),
                    'qty': round(total_qty, 6),
                    'date': min(dated, key=_date_key) if dated else '',
                    'industry': industry,
                    'sector': sector,
                })
        else:
            for lot in lots:
                rem_qty = _safe_float(lot.get('qty'))
                if rem_qty > 1e-12:
                    open_lots.append({
                        'symbol': symbol,
                        'name': name,
                        'buy_price': round(_safe_float(lot.get('price')), 4),
                        'qty': round(rem_qty, 6),
                        'date': str(lot.get('date') or ''),
                        'industry': industry,
                        'sector': sector,
                    })

    return open_lots, closed, warnings


def holding_key(h: dict) -> tuple:
    return (
        _clean_symbol(h.get('symbol')),
        str(h.get('date') or ''),
        round(_safe_float(h.get('qty')), 6),
        round(_safe_float(h.get('buy_price')), 4),
    )


def trade_key(t: dict) -> tuple:
    return (
        _clean_symbol(t.get('symbol')),
        str(t.get('buy_date') or ''),
        str(t.get('sell_date') or ''),
        round(_safe_float(t.get('qty')), 6),
        round(_safe_float(t.get('buy_price')), 4),
        round(_safe_float(t.get('sell_price')), 4),
    )

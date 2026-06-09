# ApexWealth — Portfolio Management

A full-stack portfolio management app with Flask + yfinance backend and a dark, premium UI.

## Features
- **Login/Signup** with session management, change password
- **Dashboard** — Stock holdings count, gainers/losers today, top gainer/loser, avg portfolio change
- **Portfolio** — Total invested, current value, unrealised P&L, return %, holdings count
  - **Holdings Tab** — Add/Edit/Sell positions, live LTP from NSE via yfinance
  - **Asset Allocation** — Industry/stock donut charts, invested vs value bar, P&L by stock
  - **Performance Report** — Completed trades history, cumulative P&L chart, win rate
- **Watchlist** — Track stocks with live prices
- **Analysis** — Price chart, volume, daily returns for any NSE stock
- **Markets** — NIFTY 50, SENSEX, NIFTY BANK indices + top gainers/losers

## Tech Stack
- **Backend:** Flask + yfinance (NSE live data via `.NS` suffix)
- **Frontend:** Vanilla HTML/CSS/JS + Chart.js
- **Storage:** JSON files (users.json, portfolios.json, watchlists.json, trades.json)
- **Stock Universe:** 2388 NSE equities (from EQUITY_completed_with_Industry.csv)

## Local Development

```bash
pip install flask flask-cors yfinance
cd api
python index.py
# Open http://localhost:5000
```

## Vercel Deployment

1. Install Vercel CLI: `npm i -g vercel`
2. From project root: `vercel`
3. Follow prompts — it auto-detects Python backend + static frontend

### Notes for Vercel
- The `data/` directory for JSON storage won't persist across serverless invocations on Vercel's free tier.
- For persistent storage on Vercel, replace JSON files with a free database like Supabase, PlanetScale, or MongoDB Atlas.
- Alternatively, use Vercel KV (Redis) or Vercel Postgres add-ons.

## Project Structure
```
apexwealth/
├── index.html          # Full frontend SPA
├── vercel.json         # Vercel deployment config
├── requirements.txt    # Python dependencies
├── api/
│   └── index.py        # Flask API (all endpoints)
├── static/
│   └── tickers.json    # 2388 NSE tickers with industry
└── data/               # Auto-created, stores JSON data
    ├── users.json
    ├── portfolios.json
    ├── watchlists.json
    └── trades.json
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /api/signup | Create account |
| POST | /api/login | Login |
| POST | /api/change-password | Change password |
| GET | /api/holdings/:uid | Get holdings with live prices |
| POST | /api/holdings/:uid | Add holding |
| PUT | /api/holdings/:uid/:id | Edit holding |
| DELETE | /api/holdings/:uid/:id | Delete holding |
| POST | /api/sell/:uid/:id | Sell holding (records trade) |
| GET | /api/watchlist/:uid | Get watchlist |
| POST | /api/watchlist/:uid | Add to watchlist |
| DELETE | /api/watchlist/:uid/:symbol | Remove from watchlist |
| GET | /api/trades/:uid | Get trade history |
| GET | /api/quote/:symbol | Get live quote |
| GET | /api/market/indices | NIFTY/SENSEX indices |
| GET | /api/market/top-movers | Top gainers/losers |
| GET | /api/chart/:symbol?period=1mo | Historical OHLCV data |

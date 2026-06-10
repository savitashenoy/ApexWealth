# ApexWealth Neon PostgreSQL setup for Vercel persistence

This build stores Login, Portfolio, Watchlist, and Trade History in Neon PostgreSQL when a database URL is configured.

## 1. Create a free Neon database
1. Go to Neon and create a free PostgreSQL project.
2. Copy the pooled connection string. It usually starts with `postgresql://...` and includes `sslmode=require`.

## 2. Add environment variable in Vercel
In your Vercel project:

Settings → Environment Variables → Add:

```text
DATABASE_URL=postgresql://USER:PASSWORD@HOST.neon.tech/DBNAME?sslmode=require
```

Alternative names also supported:

```text
POSTGRES_URL=...
NEON_DATABASE_URL=...
```

Redeploy after adding the variable.

## 3. Test after deployment
Open:

```text
https://YOUR-VERCEL-SITE.vercel.app/api/health/storage
```

Expected output:

```json
{
  "ok": true,
  "storage": "neon",
  "database_configured": true,
  "tables_ready": true
}
```

## Notes
- Tables are auto-created on first request.
- Local development still works using JSON fallback when `DATABASE_URL` is not set.
- Vercel serverless `/tmp` JSON fallback is not persistent, so set `DATABASE_URL` for production.

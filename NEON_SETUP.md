# ApexWealth Neon PostgreSQL setup for Vercel persistence

This build stores Login, Portfolio, Watchlist, and Trade History in Neon PostgreSQL when a valid database URL is configured.

## 1. Create a free Neon database
1. Go to Neon and create a free PostgreSQL project.
2. Open **Dashboard → Connection Details**.
3. Select **Pooled connection**. This is important for Vercel serverless.
4. Copy the full connection string. It should look similar to this shape:

```text
postgresql://USER:PASSWORD@ep-xxxxx-pooler.REGION.aws.neon.tech/DBNAME?sslmode=require
```

Do **not** use the placeholder below in Vercel:

```text
postgresql://USER:PASSWORD@HOST.neon.tech/DBNAME?sslmode=require
```

`HOST.neon.tech` must be replaced by the real host Neon gives you, usually something like `ep-blue-river-123456-pooler.ap-south-1.aws.neon.tech`.

## 2. Add environment variable in Vercel
In your Vercel project:

**Settings → Environment Variables → Add**

Name:

```text
DATABASE_URL
```

Value:

```text
postgresql://actual_user:actual_password@actual-neon-pooler-host.neon.tech/actual_db?sslmode=require
```

Alternative variable names also supported:

```text
POSTGRES_URL
NEON_DATABASE_URL
```

Redeploy after adding or changing environment variables. Vercel does not reliably apply changed env vars to old deployments.

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
  "database_url_valid": true,
  "tables_ready": true
}
```

## Common error

If you see:

```json
"database_error": "failed to resolve host 'HOST.neon.tech'"
```

then the environment variable still contains the example placeholder. Replace it with the exact Neon connection string from the Neon dashboard and redeploy.

## Notes
- Tables are auto-created on first successful request.
- Local development still works using JSON fallback when `DATABASE_URL` is not set.
- Vercel serverless `/tmp` JSON fallback is not persistent, so set a valid `DATABASE_URL` for production.

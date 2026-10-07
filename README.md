# Scaffold API

A production-ready SaaS API scaffold built with FastAPI, PostgreSQL, Redis, Celery, and Nginx.

**Covers:** JWT auth · API key management · subscription plans (Free / Starter / Pro) · rate limiting · usage tracking · billing history · async task processing.

---

## Local Development

### Prerequisites
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) running

### Steps

1. **Clone and set up environment**
   ```bash
   git clone <your-repo-url> scaffold-api
   cd scaffold-api
   cp .env.example .env
   ```
   Edit `.env` if you want different passwords locally (optional — defaults work as-is).

2. **Build and start all services**
   ```bash
   docker compose up --build
   ```
   This starts: **app · celery_worker · celery_beat · db · redis · nginx**

   > ⚠️ The database tables are **created automatically on startup** by `app/db/init_db.py`.
   > Subscription plans (Free / Starter / Pro) are **seeded automatically** too.
   > No manual `init_db` step needed.

3. **Access the API**

   | URL | Description |
   |---|---|
   | `http://localhost/docs` | Swagger UI (via Nginx on port 80) |
   | `http://localhost/redoc` | ReDoc |
   | `http://localhost/health` | Health check |

4. **Quick demo flow** (use Swagger UI or curl)
   ```bash
   # 1. Register — auto-assigns Free plan
   curl -X POST http://localhost/auth/register \
     -H "Content-Type: application/json" \
     -d '{"email":"you@example.com","password":"secret123","full_name":"Your Name"}'

   # → save the access_token and refresh_token from the response

   # 2. Create an API key (paste your access_token)
   curl -X POST http://localhost/api-keys/ \
     -H "Authorization: Bearer <access_token>" \
     -H "Content-Type: application/json" \
     -d '{"name":"my first key"}'

   # → save the raw_key (shown ONCE — store it!)

   # 3. Call a business endpoint with the API key
   curl "http://localhost/v1/analyze?query=hello+world" \
     -H "X-API-Key: <raw_key>"

   # 4. Check usage stats
   curl http://localhost/usage/ \
     -H "Authorization: Bearer <access_token>"

   # 5. Upgrade to Pro
   curl -X POST http://localhost/subscriptions/subscribe \
     -H "Authorization: Bearer <access_token>" \
     -H "Content-Type: application/json" \
     -d '{"plan_name":"pro","billing_cycle":"monthly"}'
   ```

### Running without Docker (host Python, for fast iteration)

If you want hot-reload on code changes, run the app directly while keeping DB + Redis in Docker:

```bash
# Keep infrastructure in Docker
docker compose up -d db redis

# Run app on host (edit POSTGRES_HOST and REDIS_HOST in .env to "localhost" first)
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Mac/Linux
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

> Change `POSTGRES_HOST=localhost` and `REDIS_HOST=localhost` in `.env` when running the app outside Docker.
> Change them back to `db` and `redis` when running fully in Docker.

---

## Production VPS Deployment

For 200–500 users, Docker Compose on a single VPS is the pragmatic choice.

### 1. Provision a VPS
- Minimum: **2 vCPU, 4 GB RAM** (DigitalOcean, Hetzner, AWS EC2, etc.)
- OS: Ubuntu 22.04 or 24.04 LTS

### 2. Install Docker on the VPS
```bash
ssh root@<your-vps-ip>
apt update
apt install -y git curl
curl -fsSL https://get.docker.com | sh
```

### 3. Deploy the code
```bash
mkdir -p /opt/scaffold-api
cd /opt/scaffold-api
git clone <your-repo-url> .

cp .env.example .env
nano .env   # IMPORTANT: change SECRET_KEY, all passwords, ENVIRONMENT=production
```

### 4. Start the production stack
```bash
docker compose -f docker-compose.prod.yml up -d --build
```
`docker-compose.prod.yml` uses 4 Gunicorn workers and 443 SSL — use it instead of the dev compose for production.

### 5. Set up TLS/SSL (HTTPS)
```bash
apt install -y certbot
docker compose stop nginx

# Replace with your domain and email
certbot certonly --standalone -d api.yourdomain.com -m admin@yourdomain.com --agree-tos

# Uncomment the SSL lines in nginx/nginx.conf, then restart
docker compose start nginx
```

---

## Project Structure & Architecture

```
app/
├── main.py                  # App entry point, lifespan (startup/shutdown)
├── core/
│   ├── config.py            # Settings via pydantic-settings (reads .env)
│   ├── security.py          # Password hashing, JWT, API key generation
│   └── dependencies.py      # FastAPI deps: JWT auth, API key auth, Redis
├── db/
│   ├── session.py           # SQLAlchemy async engine + session factory
│   ├── models.py            # ORM models: User, Subscription, APIKey, UsageLog, Billing, Audit
│   └── init_db.py           # Creates tables + seeds plans on startup (idempotent)
├── routers/
│   ├── auth.py              # POST /auth/register|login|logout|refresh
│   ├── subscriptions.py     # GET /subscriptions/plans, POST /subscribe, GET /me, /billing
│   ├── api_keys.py          # GET|POST /api-keys/, DELETE /api-keys/{id}
│   ├── usage.py             # GET /usage/
│   └── users.py             # GET /me  +  GET /v1/analyze  +  GET /v1/process
├── schemas/
│   └── schemas.py           # All Pydantic request/response models
├── middleware/
│   └── rate_limit.py        # Redis sliding-window rate limiter (per-min/day/month)
├── tasks/
│   ├── celery_app.py        # Celery factory + Beat schedule
│   ├── usage_tasks.py       # Async DB write of usage logs (retries on failure)
│   └── subscription_tasks.py # Daily: expire subs → Free downgrade; monthly: counter sync
├── services/                # Put complex business logic here (Stripe, email, etc.)
└── crud/                    # Put pure DB queries here (no business rules)
```

**Request flow for business API endpoints:**
```
X-API-Key → Redis cache lookup → DB lookup (cache miss) → subscription check
         → rate limit (per-min/day/month) → quota check → business logic
         → response → background: Redis counter++ + DB usage log
```

## Updating a Running Stack

After making local changes, use the right command based on what you changed:

| What changed | Command |
|---|---|
| Python logic (router, service, task) | `docker compose up --build -d` |
| Added a package to `requirements.txt` | `docker compose up --build -d` |
| DB model change (add column / new table) | see **Alembic** section below |
| `.env` values only | `docker compose up -d` — no rebuild needed |
| `nginx/nginx.conf` only | `docker compose restart nginx` — no rebuild needed |
| Full reset (⚠️ wipes DB data) | `docker compose down -v && docker compose up --build -d` |

**Check logs after any restart:**
```bash
docker compose logs -f app
docker compose logs -f celery_worker
```

---

## Database Migrations (Alembic)

`init_db.py` uses SQLAlchemy's `create_all()` which only **creates missing tables** on startup.
It will **not** alter existing tables — so any schema change (add a column, rename, add index) requires Alembic.

### One-time setup (already configured via `alembic.ini`)

```bash
# Point Alembic at your async DB — edit alembic/env.py to import your models and engine:
# from app.db.session import engine
# from app.db.models import Base
# target_metadata = Base.metadata
```

### Workflow for every schema change

```bash
# 1. Edit your model in app/db/models.py (add column, new table, etc.)

# 2. Generate a migration from the diff between your models and the current DB schema
docker compose run --rm app alembic revision --autogenerate -m "describe your change"
# e.g. "add phone_number to users"

# 3. Review the generated file in alembic/versions/ — always check before applying

# 4. Apply the migration to the running DB
docker compose run --rm app alembic upgrade head

# 5. Rebuild and restart the app
docker compose up --build -d
```

### Other useful Alembic commands

```bash
# See current migration version
docker compose run --rm app alembic current

# Show full migration history
docker compose run --rm app alembic history

# Roll back one migration
docker compose run --rm app alembic downgrade -1

# Roll back to a specific version
docker compose run --rm app alembic downgrade <revision_id>
```

> **Production rule**: never run `create_all()` against a production DB that already has data.
> Use `alembic upgrade head` in your deploy pipeline instead.

---

## Extending the Scaffold

- **Add Stripe**: implement webhooks in `app/routers/billing.py`, update `billing_history` status `pending` → `paid`
- **Add email**: fill in `send_expiry_reminders()` in `app/tasks/subscription_tasks.py` with SendGrid / Resend / SES
- **Add more business endpoints**: copy the pattern in `app/routers/users.py` — API key auth, rate limiting, and usage tracking are already wired via dependencies
- **Add Prometheus metrics**: mount `prometheus-fastapi-instrumentator` in `app/main.py` and add a Grafana service to `docker-compose.yml`


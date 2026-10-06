# Scaffold API

This repository contains the scaffold for the Commercial SaaS API designed with FastAPI, PostgreSQL, Redis, and Celery.

## Deployment Guide

### Local Development Deployment

1. **Clone and Setup Environment**
   ```bash
   git clone <your-repo-url> scaffold-api
   cd scaffold-api
   cp .env.example .env
   ```
   *Edit `.env` to set your desired local passwords and secrets.*

2. **Start the Infrastructure**
   ```bash
   # Starts PostgreSQL and Redis
   docker compose up -d db redis
   ```

3. **Initialize the Database**
   ```bash
   # Creates the database tables
   docker compose run --rm app python -m app.db.init_db
   ```

4. **Run the Application Locally (Optional: without Docker)**
   *If you prefer running the Python app directly on your host for debugging:*
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```

4. **Run Everything via Docker Compose**
   ```bash
   # Starts App, Celery, Nginx, DB, and Redis
   docker compose up -d --build
   ```
   The API will be available at `http://localhost:8000` (directly) or `http://localhost` (via Nginx).

### Production VPS Deployment

For a small/medium scale (200-500 users), deploying via Docker Compose on a single VPS (e.g., DigitalOcean, Hetzner, AWS EC2) is the most pragmatic approach.

1. **Provision a VPS**
   - Minimum specs: 2 vCPU, 4GB RAM.
   - OS: Ubuntu 22.04 or 24.04 LTS.

2. **Install Dependencies on VPS**
   ```bash
   # SSH into your VPS
   ssh root@<your-vps-ip>
   
   # Install Docker & Git
   apt update
   apt install -y git apt-transport-https ca-certificates curl software-properties-common
   curl -fsSL https://get.docker.com -o get-docker.sh
   sh get-docker.sh
   ```

3. **Deploy the Code**
   ```bash
   mkdir -p /opt/scaffold-api
   cd /opt/scaffold-api
   git clone <your-repo-url> .
   
   # Set up environment variables
   cp .env.example .env
   nano .env  # IMPORTANT: Change SECRET_KEY, DB passwords, and Redis password!
   ```

4. **Start the Production Stack**
   ```bash
   # We use the main docker-compose.yml for this scaffold, but in a real scenario 
   # you might have a docker-compose.prod.yml
   docker compose up -d --build
   ```

5. **Setup TLS/SSL (HTTPS)**
   You should secure Nginx with Let's Encrypt.
   ```bash
   apt install -y certbot
   # Stop Nginx temporarily if it's bound to port 80
   docker compose stop nginx
   
   # Get the certificate (replace with your domain and email)
   certbot certonly --standalone -d api.yourdomain.com -m admin@yourdomain.com --agree-tos
   
   # Then uncomment the SSL sections in nginx/nginx.conf and restart Nginx
   docker compose start nginx
   ```

## Project Structure & Business Logic

To keep this Commercial SaaS API scalable and maintainable, follow the **Service Layer Pattern**. Do not put complex business rules inside FastAPI routers or database CRUD files.

The architecture flows like this:
`Routers (HTTP/Input) -> Services (Business Logic) -> CRUD (Database Ops) -> Models (Schema)`

### Where to organize your files:
- **`app/routers/`**: HTTP endpoints, input validation, and returning JSON. Should be very thin.
- **`app/services/`**: **Put your core business logic here!** (e.g. `subscription_service.py`). This is where you calculate quotas, verify eligibility for upgrades, trigger emails, and compose multiple CRUD operations together.
- **`app/crud/`**: Pure database queries using SQLAlchemy. No business rules here, just simple create/read/update/delete functions.
- **`app/schemas/`**: Pydantic models for request/response validation.

### Example Workflow (Upgrading a Plan)
1. The user hits `POST /v1/subscriptions/upgrade` (`app/routers/subscriptions.py`).
2. The router calls `await subscription_service.upgrade_user_plan(user_id, new_plan_id)`.
3. Inside `app/services/subscription_service.py`, the business logic runs:
   - Check if the user is already on the plan.
   - Check if payment is required.
   - Charge the credit card (via Stripe API).
   - Call `crud.subscription.update_plan()` to save to DB.
   - Dispatch an async Celery task to send a receipt email.

## Next Steps
- Implement Alembic for database migrations (e.g. `alembic init alembic`).
- Wire up the actual ORM models in `app/db/models.py`.
- Implement Stripe webhooks if you plan to automate billing.

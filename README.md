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

## Next Steps
- Implement Alembic for database migrations (e.g. `alembic init alembic`).
- Wire up the actual ORM models in `app/db/models.py`.
- Implement Stripe webhooks if you plan to automate billing.

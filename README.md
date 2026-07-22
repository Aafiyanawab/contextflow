AI-powered enterprise knowledge platform that understands your GitHub repositories, delivers repository-aware answers, and cuts unnecessary LLM calls with a semantic cache.


![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.x-000000?logo=flask&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-RDS-4169E1?logo=postgresql&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Containerized-2496ED?logo=docker&logoColor=white)
![Kubernetes](https://img.shields.io/badge/Kubernetes-k3s-326CE5?logo=kubernetes&logoColor=white)
![Prometheus](https://img.shields.io/badge/Prometheus-Metrics-E6522C?logo=prometheus&logoColor=white)
![Grafana](https://img.shields.io/badge/Grafana-Dashboards-F46800?logo=grafana&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

---
Overview

ContextFlow turns every connected GitHub repository into an intelligent, queryable workspace. It auto-discovers a repo's tech stack, builds reusable contextual knowledge, and answers questions with repository-aware context — while a Semantic Cache avoids redundant model calls and a Repository Inventory stores detected metadata for reuse. It ships with full RBAC, an admin console, and a lightweight Prometheus + Grafana observability stack.

🌐 Live Demo

🚧 Coming Soon

The application is fully deployed and running in production.

The public demo URL will be published in a future update.

---
✨ Key Features

| Feature | Description |
|---------|-------------|
| 🔐 Authentication | GitHub OAuth, email/password authentication, and secure SMTP-based password reset |
| 👥 RBAC | Role-Based Access Control (RBAC) for secure user and administrative access |
| 💬 AI Chat | Repository-aware AI chat grounded in each workspace's context |
| 🧭 Intent Engine | Intent Engine + Context Builder inject only relevant context |
| ⚡ Semantic Cache | Returns cached answers for semantically similar questions |
| 📦 Repository Inventory | Fast, repository-aware AI responses |
| 🔎 AI Diagnostics | Retrieval health, knowledge index, and cache statistics |
| 📊 Monitoring | Prometheus metrics and Grafana dashboards |

---
🏗️ Architecture

ContextFlow consists of the following core components:

- Authentication
- AI Chat
- Intent Engine
- Context Builder
- Semantic Cache
- Repository Inventory

The application integrates with:

- PostgreSQL
- GitHub API
- OpenAI API
---

🧰 Technology Stack

| Layer | Technologies |
|--------|--------------|
| Backend | Python, Flask, SQLAlchemy, PostgreSQL |
| AI | OpenAI API, Intent Engine, Context Builder, Semantic Cache, Repository Inventory, AI Diagnostics |
| Infrastructure | Docker, Kubernetes (k3s), Traefik, GitHub Actions, Amazon ECR, Amazon EC2 |
| Monitoring | Prometheus, Grafana |
| Frontend | HTML, Jinja2, Bootstrap |

---
📁 Project Structure

contextflow/
├── app.py                 # Flask app + routes (chat, workspaces, admin)
├── manage.py              # CLI entrypoint (migrations, set-admin)
├── wsgi.py                # WSGI entrypoint (gunicorn)
├── Dockerfile             # Container image build
├── docker-compose.yml     # Local multi-service stack
├── entrypoint.sh          # flask db upgrade -> gunicorn
├── requirements.txt       # Python dependencies
├── .gitignore
├── .gitattributes
├── app/                   # auth, models, intent_engine, context_builder,
│                          # semantic_cache, github_discovery, metrics, ingest/
├── templates/             # Jinja2 templates
├── static/                # CSS, JavaScript and assets
├── migrations/            # Alembic database migrations
├── monitoring/            # Prometheus + Grafana manifests & dashboards
├── k8s-aws/               # Kubernetes manifests
├── terraform/             # AWS infrastructure (EC2, RDS, ECR, IAM/OIDC)
└── docs/                  # Architecture & decisions

---
🚀 CI/CD Workflow

## 🚀 CI/CD Workflow

Every push to `main` automatically:

1. Triggers GitHub Actions
2. Builds a Docker image
3. Pushes the image to Amazon ECR
4. Deploys the latest image to the Kubernetes (k3s) cluster on Amazon EC2
5. Applies database migrations during application startup
---
📊 Monitoring


The application exposes a token-protected `/metrics` endpoint.

Monitoring stack:

- Prometheus
- Grafana

> **Note**

> Version 1 intentionally uses a lightweight Prometheus + Grafana monitoring stack.
> AWS CloudWatch has not been implemented and is reserved for a future release.
```

---
⚙️ Installation

Prerequisites: Python 3.11+, PostgreSQL (or SQLite for local dev).

```bash
git clone https://github.com/Aafiyanawab/contextflow.git
cd contextflow
pip install -r requirements.txt
```

Create a .env:

```env
SECRET_KEY=your-long-random-secret
OPENAI_API_KEY=sk-...
GITHUB_TOKEN=ghp_...
# DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/contextflow
```

Run locally:

```bash
FLASK_APP=manage.py flask db upgrade
python app.py
FLASK_APP=manage.py flask set-admin you@example.com
```

---
☁️ Deployment

Deployment is automatic on push to main:

```bash
git add .
git commit -m "your changes"
git push origin main
```

GitHub Actions authenticates via OIDC, builds and pushes the image to ECR, and performs a rolling update on k3s. Deploy the monitoring stack once:

kubectl apply -f monitoring/
kubectl -n monitoring port-forward svc/grafana 3000:3000

---
🔮 Future Improvements

- HTTPS with Let's Encrypt
- AWS CloudWatch
- Redis-backed Semantic Cache
- Multi-node Kubernetes
- Helm Charts
- Horizontal Pod Autoscaling

---
📄 License

Licensed under the MIT License — see LICENSE for details.
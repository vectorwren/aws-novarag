# aws-novarag

Agentic RAG using AWS Bedrock (Nova + Titan) and Pydantic AI framework.

## [Read the full story →](https://towardsaws.com/from-failed-local-llms-to-serverless-success-my-agentic-rag-journey-450b64249192)

## What is it?

A smarter RAG that chooses retrieval strategies instead of just semantic search:
1. **Semantic Search** - Vector similarity (default)
2. **Browse Pages** - List all available docs
3. **Full Page View** - Get complete document context

## Stack

- **LLM**: AWS Bedrock Nova Lite
- **Embeddings**: AWS Titan Embeddings v2
- **Framework**: Pydantic AI
- **Database**: PostgreSQL + pgvector (Neon / RDS / Supabase)

## Quick Start (Neon - Free Tier)

```bash
# 1. Clone and install
git clone https://github.com/REDACTED/aws-novarag.git
cd aws-novarag
pip install -r requirements.txt

# 2. Create Neon database (free)
# Go to https://console.neon.tech → Create project → Copy connection string

# 3. Configure
cp .env.example .env
# Add your DATABASE_URL and AWS credentials

# 4. Setup schema & ingest data
python scripts/setup_neon.py
python scripts/step1_generate_embeddings.py
python scripts/step2_upsert_to_rds.py

# 5. Run
python main.py
```

## Database Options

| Option | Cost | Serverless | Setup |
|--------|------|------------|-------|
| **Neon** | Free (0.5GB) | Yes, scales to zero | `DATABASE_URL` |
| **Supabase** | Free (500MB) | No (always-on) | `SUPABASE_URL` |
| **AWS RDS** | ~$15/mo | No | `DB_HOST` + params |

## API Server

```bash
cd deployment && python app.py
```

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/query` | POST | Ask a question |
| `/health` | GET | Health check |
| `/stats` | GET | Query statistics |

## Deploy to AWS (Optional)

```bash
./deploy.sh
```

Deploys: RDS PostgreSQL + ECS Fargate

## Cost

| Component | Price |
|-----------|-------|
| Neon (free tier) | $0 |
| Nova Lite | $0.15/M input, $0.60/M output |
| Titan Embeddings | $0.02/M tokens |

**~$0.0005 per query** (vector DB free with Neon)

## License

MIT

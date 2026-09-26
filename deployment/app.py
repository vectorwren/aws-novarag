"""
FastAPI web server for NovaRAG.
Provides HTTP endpoints for the RAG agent.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Setup FastAPI
try:
    from fastapi import FastAPI, HTTPException, status
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    logging.warning("FastAPI not available. Install with: pip install fastapi uvicorn")

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Request/Response models
class QueryRequest(BaseModel):
    """Request model for query endpoint."""
    question: str = Field(..., description="The user's question", min_length=1)


class QueryResponse(BaseModel):
    """Response model for query endpoint."""
    answer: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    tools_used: List[str] = []


class HealthResponse(BaseModel):
    """Response model for health check."""
    status: str
    timestamp: str
    version: str = "1.0.0"
    database: str


# Global agent instance
rag_agent = None
db = None
query_stats = {
    "total": 0,
    "total_latency": 0,
    "total_tokens": 0,
    "total_cost": 0,
}


def create_app() -> "FastAPI":
    """Create and configure the FastAPI application."""
    if not FASTAPI_AVAILABLE:
        raise RuntimeError("FastAPI is not installed")

    app = FastAPI(
        title="NovaRAG API",
        description="Agentic RAG with AWS Bedrock and RDS PostgreSQL",
        version="1.0.0",
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Startup event
    @app.on_event("startup")
    async def startup_event():
        """Initialize the agent on startup."""
        global rag_agent, db
        try:
            from main import rag_agent as agent, get_or_reconnect_db, is_supabase
            rag_agent = agent
            # Test database connection
            db = get_or_reconnect_db()
            logger.info("NovaRAG agent initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize agent: {e}")
            raise

    # Health check endpoint (no DB ping - just check agent is initialized)
    @app.get("/health", response_model=HealthResponse)
    async def health_check():
        """Health check endpoint - lightweight, no DB ping."""
        db_type = "PostgreSQL (Neon)"
        return HealthResponse(
            status="healthy" if rag_agent else "initializing",
            timestamp=datetime.utcnow().isoformat(),
            database=db_type,
        )

    # Query endpoint
    @app.post("/query", response_model=QueryResponse)
    async def query(request: QueryRequest):
        """Process a user question."""
        if not rag_agent or not db:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Agent not initialized"
            )

        try:
            from main import Deps, get_or_reconnect_db

            start_time = time.time()
            deps = Deps(db=get_or_reconnect_db())
            result = await rag_agent.run(request.question, deps=deps)
            elapsed_ms = int((time.time() - start_time) * 1000)

            usage = result.usage()
            input_tokens = usage.input_tokens if usage else 0
            output_tokens = usage.output_tokens if usage else 0
            total_tokens = input_tokens + output_tokens

            input_cost = input_tokens * 0.15 / 1_000_000
            output_cost = output_tokens * 0.60 / 1_000_000
            total_cost = input_cost + output_cost

            # Extract tool calls from result (unique, in order)
            tools_used = []
            seen_tools = set()
            if hasattr(result, 'all_messages'):
                for message in result.all_messages():
                    if hasattr(message, 'parts'):
                        for part in message.parts:
                            if hasattr(part, 'tool_name') and part.tool_name:
                                if part.tool_name not in seen_tools:
                                    tools_used.append(part.tool_name)
                                    seen_tools.add(part.tool_name)
                    elif hasattr(message, 'tool_calls'):
                        for call in message.tool_calls:
                            if hasattr(call, 'tool_name'):
                                if call.tool_name not in seen_tools:
                                    tools_used.append(call.tool_name)
                                    seen_tools.add(call.tool_name)

            # Update stats
            query_stats["total"] += 1
            query_stats["total_latency"] += elapsed_ms
            query_stats["total_tokens"] += total_tokens
            query_stats["total_cost"] += total_cost

            return QueryResponse(
                answer=result.output,
                latency_ms=elapsed_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                estimated_cost_usd=round(total_cost, 6),
                tools_used=tools_used,
            )

        except Exception as e:
            logger.error(f"Query error: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e)
            )

    # Stats endpoint
    @app.get("/stats")
    async def stats():
        """Get query statistics."""
        total = query_stats["total"]
        if total == 0:
            return {
                "total_queries": 0,
                "average_latency_ms": 0.0,
                "average_tokens": 0.0,
                "total_cost_usd": 0.0,
            }

        return {
            "total_queries": total,
            "average_latency_ms": query_stats["total_latency"] / total,
            "average_tokens": query_stats["total_tokens"] / total,
            "total_cost_usd": round(query_stats["total_cost"], 6),
        }

    # Root endpoint
    @app.get("/")
    async def root():
        """Root endpoint with API information."""
        return {
            "name": "NovaRAG API",
            "version": "1.0.0",
            "description": "Agentic RAG with AWS Bedrock and RDS PostgreSQL",
            "endpoints": {
                "health": "GET /health",
                "query": "POST /query",
                "stats": "GET /stats",
            },
            "models": {
                "chat": os.getenv("BEDROCK_NOVA_MODEL_ID", "amazon.nova-2-lite-v1:0"),
                "embeddings": os.getenv("BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
            }
        }

    return app


if FASTAPI_AVAILABLE:
    app = create_app()

    def main():
        """Run the server."""
        import uvicorn
        port = int(os.getenv("PORT", "8000"))
        uvicorn.run(
            "app:app",
            host="0.0.0.0",
            port=port,
            reload=False,
            access_log=True,
        )

    if __name__ == "__main__":
        main()

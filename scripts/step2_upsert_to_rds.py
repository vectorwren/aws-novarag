"""
Step 2: Load embeddings from cache and upsert to RDS PostgreSQL.
Run this AFTER step1_generate_embeddings.py completes.
"""

import json
import logging
import os
import os
from dataclasses import dataclass
from typing import List, Dict, Any
import psycopg2
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EMBEDDINGS_CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "embeddings_cache.json")


@dataclass
class DocumentChunk:
    """A chunk of documentation with embedding."""
    id: int
    url: str
    chunk_number: int
    title: str
    summary: str
    content: str
    metadata: Dict[str, Any]
    embedding: List[float] = None


class PostgresUpsert:
    """Upsert pre-generated embeddings to PostgreSQL (Neon, RDS, etc.)."""

    def __init__(self, database_url: str = None, db_host: str = None, db_name: str = None, db_user: str = None, db_password: str = None):
        if database_url:
            self.conn = psycopg2.connect(database_url, sslmode="require")
        else:
            self.conn = psycopg2.connect(
                host=db_host,
                database=db_name,
                user=db_user,
                password=db_password,
                sslmode="require"
            )
        self.cursor = self.conn.cursor()

    def load_embeddings(self, cache_path: str) -> List[DocumentChunk]:
        """Load embeddings from JSON cache file."""
        logger.info(f"Loading embeddings from {cache_path}...")

        with open(cache_path, 'r') as f:
            data = json.load(f)

        chunks = []
        # Handle both formats: direct array or wrapped object
        if isinstance(data, list):
            chunks_data = data
        else:
            chunks_data = data.get("chunks", [])

        for c_data in chunks_data:
            chunk = DocumentChunk(
                id=c_data["id"],
                url=c_data["url"],
                chunk_number=c_data.get("chunk_number", 0),
                title=c_data["title"],
                summary=c_data.get("summary", c_data["content"][:200] + "..."),
                content=c_data["content"],
                metadata=c_data.get("metadata", {}),
                embedding=c_data["embedding"]
            )
            chunks.append(chunk)

        logger.info(f"Loaded {len(chunks)} chunks from cache")
        return chunks

    def upsert_batch(self, chunks: List[DocumentChunk], batch_size: int = 100):
        """Upsert chunks to RDS in batches."""
        logger.info(f"Upserting {len(chunks)} chunks to RDS...")

        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (len(chunks) + batch_size - 1) // batch_size

            logger.info(f"Processing batch {batch_num}/{total_batches} ({len(batch)} chunks)")

            for c in batch:
                # Convert embedding list to string format for PostgreSQL vector type
                embedding_str = f"[{','.join(map(str, c.embedding))}]" if c.embedding else "[]"

                self.cursor.execute(
                    "INSERT INTO site_pages (id, url, chunk_number, title, summary, content, metadata, embedding) "
                    "OVERRIDING SYSTEM VALUE "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "url = EXCLUDED.url, chunk_number = EXCLUDED.chunk_number, title = EXCLUDED.title, "
                    "summary = EXCLUDED.summary, content = EXCLUDED.content, "
                    "metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding",
                    (
                        c.id,
                        c.url,
                        c.chunk_number,
                        c.title,
                        c.summary,
                        c.content,
                        json.dumps(c.metadata),
                        embedding_str,
                    )
                )

            # Commit each batch
            self.conn.commit()
            logger.info(f"Batch {batch_num} committed")

        logger.info("Upsert complete!")

    def verify_data(self):
        """Verify data was inserted correctly."""
        self.cursor.execute("SELECT COUNT(*) FROM site_pages")
        count = self.cursor.fetchone()[0]
        logger.info(f"Total rows in site_pages: {count}")

        self.cursor.execute("SELECT COUNT(*) FROM site_pages WHERE embedding IS NOT NULL")
        with_embeddings = self.cursor.fetchone()[0]
        logger.info(f"Rows with embeddings: {with_embeddings}")

    def close(self):
        """Close database connection."""
        self.cursor.close()
        self.conn.close()


def main():
    # Check if embeddings cache exists
    if not os.path.exists(EMBEDDINGS_CACHE):
        logger.error(f"Embeddings cache not found: {EMBEDDINGS_CACHE}")
        logger.error("Run step1_generate_embeddings.py first!")
        return

    # Option 1: DATABASE_URL (Neon, Railway, etc.)
    database_url = os.getenv("DATABASE_URL")

    # Option 2: Individual params (RDS, etc.)
    db_host = os.getenv("DB_HOST")
    db_name = os.getenv("DB_NAME", "novaragdb")
    db_user = os.getenv("DB_USER", "novaragadmin")
    db_password = os.getenv("DB_PASSWORD")

    if not database_url and not db_host:
        logger.error("Set DATABASE_URL or DB_HOST in .env")
        return

    # Connect to PostgreSQL
    if database_url:
        logger.info("Connecting via DATABASE_URL...")
        upserter = PostgresUpsert(database_url=database_url)
    else:
        logger.info(f"Connecting to {db_host}...")
        upserter = PostgresUpsert(db_host=db_host, db_name=db_name, db_user=db_user, db_password=db_password)

    try:
        chunks = upserter.load_embeddings(EMBEDDINGS_CACHE)
        upserter.upsert_batch(chunks)
        upserter.verify_data()
    finally:
        upserter.close()


if __name__ == "__main__":
    main()

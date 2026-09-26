#!/usr/bin/env python3
"""Setup pgvector schema on Neon PostgreSQL."""

import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set in .env")
    print("Get your connection string from: https://console.neon.tech")
    exit(1)

print(f"Connecting to Neon...")
conn = psycopg2.connect(DATABASE_URL, sslmode="require")
conn.autocommit = True
cursor = conn.cursor()

print("Enabling pgvector extension...")
cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")

print("Creating site_pages table...")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS site_pages (
      id BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
      url TEXT NOT NULL,
      chunk_number INT DEFAULT 0,
      title TEXT,
      summary TEXT,
      content TEXT,
      metadata JSONB DEFAULT '{}',
      embedding vector(1024),
      created_at TIMESTAMPTZ DEFAULT NOW(),
      updated_at TIMESTAMPTZ DEFAULT NOW()
    );
""")

print("Creating vector index...")
cursor.execute("""
    CREATE INDEX IF NOT EXISTS site_pages_embedding_idx
    ON site_pages USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
""")

print("Creating helper indexes...")
cursor.execute("""
    CREATE INDEX IF NOT EXISTS site_pages_url_idx ON site_pages(url);
""")
cursor.execute("""
    CREATE INDEX IF NOT EXISTS site_pages_metadata_idx ON site_pages USING gin(metadata);
""")

print("Creating match_site_pages function...")
cursor.execute("""
    CREATE OR REPLACE FUNCTION match_site_pages (
      query_embedding vector(1024),
      match_count int DEFAULT 5,
      filter_config jsonb DEFAULT '{}'
    ) RETURNS TABLE (
      id bigint,
      url text,
      chunk_number int,
      title text,
      summary text,
      content text,
      metadata jsonb,
      embedding vector(1024),
      similarity float
    )
    AS $$
    BEGIN
      RETURN query
      SELECT
        sp.id,
        sp.url,
        sp.chunk_number,
        sp.title,
        sp.summary,
        sp.content,
        sp.metadata,
        sp.embedding,
        1 - (sp.embedding <=> query_embedding) AS similarity
      FROM site_pages sp
      WHERE sp.metadata @> filter_config
      ORDER BY sp.embedding <=> query_embedding
      LIMIT match_count;
    END;
    $$ LANGUAGE plpgsql;
""")

cursor.close()
conn.close()

print("Done! Neon schema ready.")

#!/usr/bin/env python3
"""
Scrape Pydantic AI documentation using sitemap + markdown conversion.
No browser required - uses requests and html2text.

Usage:
    python scripts/scrape_pydantic_docs.py
"""

import asyncio
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import List

import boto3
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-2")

SITEMAP_URL = "https://ai.pydantic.dev/sitemap.xml"


@dataclass
class DocumentChunk:
    """A chunk of documentation ready for storage."""
    url: str
    chunk_number: int
    title: str
    content: str
    metadata: dict
    embedding: List[float] = None


def get_db_connection():
    """Get database connection."""
    import psycopg2
    return psycopg2.connect(DATABASE_URL, sslmode="require")


class BedrockEmbeddingClient:
    """AWS Bedrock embedding client."""

    def __init__(self, region: str = None, model_id: str = None):
        self.client = boto3.client("bedrock-runtime", region_name=region or AWS_REGION)
        self.model_id = model_id or os.getenv("BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")

    async def get_embedding(self, text: str) -> List[float]:
        """Generate embedding for text."""
        import json
        request_body = {"inputText": text}
        try:
            response = self.client.invoke_model(
                modelId=self.model_id,
                body=json.dumps(request_body),
            )
            result = json.loads(response["body"].read())
            return result.get("embedding", [])
        except Exception as e:
            logger.error(f"Embedding error: {e}")
            return [0.0] * 1024


def get_sitemap_urls(sitemap_url: str) -> List[str]:
    """Extract all URLs from the sitemap."""
    logger.info(f"Fetching sitemap from {sitemap_url}")
    response = requests.get(sitemap_url, timeout=30)
    response.raise_for_status()

    root = ET.fromstring(response.content)
    urls = []

    url_tags = root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}url")
    logger.info(f"Found {len(url_tags)} URLs in sitemap")

    for url_tag in url_tags:
        loc = url_tag.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
        if loc is not None:
            urls.append(loc.text)

    return urls


def html_to_markdown(html: str, url: str) -> str:
    """Convert HTML to simple markdown."""
    try:
        import html2text
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = False
        h.body_width = 0  # Don't wrap lines
        return h.handle(html)
    except ImportError:
        # Fallback: simple text extraction
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.decompose()
        return soup.get_text(separator='\n', strip=True)


def fetch_page_content(url: str) -> str:
    """Fetch a page and convert to markdown."""
    try:
        response = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        return html_to_markdown(response.text, url)
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return ""


def chunk_content(content: str, url: str, title: str, max_chunk_size: int = 3000) -> List[DocumentChunk]:
    """Chunk content while preserving code blocks."""
    chunks = []
    lines = content.split('\n')

    current_chunk = []
    current_size = 0
    chunk_number = 0
    in_code_block = False

    # Extract title from content if not provided
    if not title:
        for line in lines[:20]:
            if line.strip().startswith('#'):
                title = line.lstrip('#').strip()
                break

    if not title:
        title = url.split('/')[-1].replace('-', ' ').title()

    for line in lines:
        if line.strip().startswith('```'):
            in_code_block = not in_code_block

        line_size = len(line) + 1

        should_split = (
            current_size + line_size > max_chunk_size and
            not in_code_block and
            line.strip() == ''
        )

        if should_split and current_chunk:
            chunk_content = '\n'.join(current_chunk).strip()
            if chunk_content and len(chunk_content) > 100:  # Skip tiny chunks
                chunks.append(DocumentChunk(
                    url=url,
                    chunk_number=chunk_number,
                    title=title,
                    content=chunk_content,
                    metadata={
                        "source": "pydantic_ai_docs",
                        "scraped_at": datetime.utcnow().isoformat(),
                    },
                ))
                chunk_number += 1
            current_chunk = []
            current_size = 0

        current_chunk.append(line)
        current_size += line_size

    # Last chunk
    if current_chunk:
        chunk_content = '\n'.join(current_chunk).strip()
        if chunk_content and len(chunk_content) > 100:
            chunks.append(DocumentChunk(
                url=url,
                chunk_number=chunk_number,
                title=title,
                content=chunk_content,
                metadata={
                    "source": "pydantic_ai_docs",
                    "scraped_at": datetime.utcnow().isoformat(),
                },
            ))

    return chunks


async def generate_embeddings_for_chunks(
    chunks: List[DocumentChunk],
    embedding_client: BedrockEmbeddingClient,
) -> List[DocumentChunk]:
    """Generate embeddings for all chunks."""
    total = len(chunks)
    logger.info(f"Generating embeddings for {total} chunks")

    for i, chunk in enumerate(chunks):
        if i % 50 == 0:
            logger.info(f"  {i + 1}/{total}")

        text_to_embed = f"{chunk.title}\n\n{chunk.content}"
        chunk.embedding = await embedding_client.get_embedding(text_to_embed)

    logger.info(f"Generated {sum(1 for c in chunks if c.embedding)} embeddings")
    return chunks


def store_chunks_in_db(chunks: List[DocumentChunk]) -> int:
    """Store chunks in database."""
    import json
    logger.info(f"Storing {len(chunks)} chunks in database")

    conn = get_db_connection()
    cursor = conn.cursor()

    # Clear existing data
    logger.info("Clearing existing pydantic_ai_docs data...")
    cursor.execute(
        "DELETE FROM site_pages WHERE metadata->>'source' = 'pydantic_ai_docs'"
    )
    conn.commit()

    # Insert new chunks
    inserted = 0
    for chunk in chunks:
        if not chunk.embedding:
            continue

        embedding_str = f"[{','.join(map(str, chunk.embedding))}]"

        cursor.execute(
            """
            INSERT INTO site_pages (url, chunk_number, title, content, metadata, embedding)
            VALUES (%s, %s, %s, %s, %s, %s::vector)
            """,
            (
                chunk.url,
                chunk.chunk_number,
                chunk.title,
                chunk.content,
                json.dumps(chunk.metadata),
                embedding_str,
            ),
        )
        inserted += 1

        if inserted % 50 == 0:
            conn.commit()
            logger.info(f"  {inserted}/{len(chunks)} inserted")

    conn.commit()
    cursor.close()
    conn.close()

    logger.info(f"Inserted {inserted} chunks total")
    return inserted


async def main():
    """Main scraping pipeline."""
    logger.info("=" * 60)
    logger.info("Pydantic AI Documentation Scraper (Sitemap)")
    logger.info("=" * 60)

    # Step 1: Get all URLs from sitemap
    urls = get_sitemap_urls(SITEMAP_URL)

    # Use ALL URLs from sitemap (no filtering)
    logger.info(f"Scraping all {len(urls)} documentation URLs")

    # Step 2: Fetch all pages
    logger.info(f"Fetching {len(urls)} pages...")
    all_chunks = []
    for url in urls:
        content = fetch_page_content(url)
        if content and len(content) > 500:
            chunks = chunk_content(content, url, "")
            all_chunks.extend(chunks)
            logger.info(f"  ✓ {url}: {len(chunks)} chunks")
        else:
            logger.warning(f"  ✗ {url}: skipped (too short)")

    logger.info(f"Created {len(all_chunks)} total chunks")

    # Step 3: Generate embeddings
    embedding_client = BedrockEmbeddingClient()
    all_chunks = await generate_embeddings_for_chunks(all_chunks, embedding_client)

    # Step 4: Store in database
    inserted = store_chunks_in_db(all_chunks)

    logger.info("=" * 60)
    logger.info(f"Complete! Scraped {len(urls)} pages")
    logger.info(f"Created {len(all_chunks)} chunks")
    logger.info(f"Inserted {inserted} chunks into database")
    logger.info("=" * 60)


if __name__ == "__main__":
    import json
    asyncio.run(main())

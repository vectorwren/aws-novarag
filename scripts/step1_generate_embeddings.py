"""
Step 1: Generate embeddings and save to local JSON file.
Run this first to create embeddings cache.
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any
import boto3
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EMBEDDINGS_CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "embeddings_cache.json")
INPUT_FILE = "data/llms-full.txt"


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


class BedrockEmbeddingClient:
    """AWS Bedrock client for generating embeddings."""

    def __init__(self, region: str = None, model_id: str = None):
        self.region = region or os.getenv("AWS_REGION", "ap-southeast-2")
        self.client = boto3.client("bedrock-runtime", region_name=self.region)
        self.model_id = model_id or os.getenv("BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")

    async def get_embedding(self, text: str) -> List[float]:
        """Generate embedding for text."""
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
            raise


class EmbeddingGenerator:
    """Generate embeddings for documentation chunks."""

    def __init__(self, embedding_client: BedrockEmbeddingClient):
        self.embedding_client = embedding_client
        self.chunks: List[DocumentChunk] = []

    def parse_markdown_sections(self, file_path: str, max_sections: int = None) -> List[Dict]:
        """Parse markdown file into sections."""
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        sections = []
        current_section = {"title": "Introduction", "content": "", "level": 0}
        current_content = []

        for line in content.split("\n"):
            # Match markdown headings (#, ##, ###, etc.)
            heading_match = re.match(r"^(#+)\s+(.+)$", line)
            if heading_match:
                if current_content:
                    current_section["content"] = "\n".join(current_content).strip()
                    if len(current_section["content"]) > 50:
                        sections.append(current_section.copy())

                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()
                current_section = {"title": title, "content": "", "level": level}
                current_content = []
            else:
                current_content.append(line)

            if max_sections and len(sections) >= max_sections:
                break

        if current_content:
            current_section["content"] = "\n".join(current_content).strip()
            if len(current_section["content"]) > 50:
                sections.append(current_section)

        logger.info(f"Parsed {len(sections)} sections")
        return sections

    def chunk_text(self, title: str, content: str) -> List[str]:
        """Chunk text into smaller pieces."""
        chunks = []
        paragraphs = content.split("\n\n")
        current_chunk = ""

        for para in paragraphs:
            if len(current_chunk) + len(para) < 1000:
                current_chunk += "\n\n" + para if current_chunk else para
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = para

        if current_chunk:
            chunks.append(current_chunk.strip())

        return chunks

    def create_chunks(self, sections: List[Dict]) -> List[DocumentChunk]:
        """Create DocumentChunk objects from sections."""
        chunks = []
        chunk_id = 0

        for section in sections:
            title = section["title"]
            content = section["content"]

            if len(content) < 100:
                continue

            text_chunks = self.chunk_text(title, content)

            for i, chunk_text in enumerate(text_chunks):
                if len(chunk_text) < 20:
                    continue

                url = f"doc://{title.lower().replace(' ', '-')}"

                chunk = DocumentChunk(
                    id=chunk_id,
                    url=url,
                    chunk_number=i,
                    title=title,
                    summary=chunk_text[:200] + "..." if len(chunk_text) > 200 else chunk_text,
                    content=chunk_text,
                    metadata={
                        "section_title": title,
                        "chunk_index": i,
                        "source": "pydantic_ai_docs",
                        "created_at": datetime.utcnow().isoformat(),
                    },
                    embedding=None  # Will be filled
                )
                chunks.append(chunk)
                chunk_id += 1

        logger.info(f"Created {len(chunks)} chunks")
        return chunks

    async def generate_embeddings(self, chunks: List[DocumentChunk]) -> List[DocumentChunk]:
        """Generate embeddings for all chunks."""
        total = len(chunks)

        for i, chunk in enumerate(chunks):
            if i % 100 == 0:
                logger.info(f"Generating embedding {i+1}/{total}")

            text_to_embed = f"{chunk.title}\n\n{chunk.content}"

            try:
                chunk.embedding = await self.embedding_client.get_embedding(text_to_embed)
            except Exception as e:
                logger.error(f"Error generating embedding for chunk {i}: {e}")
                chunk.embedding = [0.0] * 1024

        logger.info(f"Generated {sum(1 for c in chunks if c.embedding)} embeddings")
        return chunks

    def save_to_file(self, output_path: str):
        """Save chunks with embeddings to JSON file."""
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Convert to dict for JSON serialization
        data = {
            "metadata": {
                "total_chunks": len(self.chunks),
                "generated_at": datetime.utcnow().isoformat(),
                "embedding_dim": 1024
            },
            "chunks": [asdict(c) for c in self.chunks]
        }

        with open(output_path, 'w') as f:
            json.dump(data, f)

        logger.info(f"Saved {len(self.chunks)} chunks to {output_path}")

    async def run(self, file_path: str, max_sections: int = None):
        """Run the full embedding generation pipeline."""
        logger.info("=" * 60)
        logger.info("Step 1: Generating Embeddings")
        logger.info("=" * 60)

        sections = self.parse_markdown_sections(file_path, max_sections=max_sections)
        self.chunks = self.create_chunks(sections)
        self.chunks = await self.generate_embeddings(self.chunks)
        self.save_to_file(EMBEDDINGS_CACHE)

        logger.info("=" * 60)
        logger.info(f"Complete! {len(self.chunks)} chunks ready for upsert")
        logger.info("=" * 60)


async def main():
    client = BedrockEmbeddingClient(region=os.getenv("AWS_REGION", "ap-southeast-2"))
    generator = EmbeddingGenerator(client)
    await generator.run(INPUT_FILE)


if __name__ == "__main__":
    asyncio.run(main())

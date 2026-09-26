# NovaRAG Dockerfile for AWS Fargate deployment
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install FastAPI and uvicorn for the API server
RUN pip install --no-cache-dir fastapi uvicorn[standard] psycopg2-binary

# Copy application code
COPY main.py .
COPY deployment/ ./deployment/
COPY .env .

# Create a non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 8000

# Run the FastAPI server
CMD ["uvicorn", "deployment.app:app", "--host", "0.0.0.0", "--port", "8000"]

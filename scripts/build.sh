#!/bin/bash
set -e

# Build script for NovaRAG Docker image
# Usage: ./scripts/build.sh [tag]
# Auto-discovers AWS region and ECR repository

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Get AWS account ID and region from AWS config or defaults
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=$(aws configure get region 2>/dev/null || echo "ap-southeast-2")

# Auto-discover ECR repository (look for "nova" or "rag" repos)
REPO_NAME=$(aws ecr describe-repositories --region "$AWS_REGION" --query 'repositories[?contains(repositoryName, `nova`) || contains(repositoryName, `rag`)].repositoryName' --output text 2>/dev/null | head -1)
if [[ -z "$REPO_NAME" ]]; then
    REPO_NAME="nova-rag"
fi

# Image tag (default: latest)
TAG=${1:-latest}
IMAGE_NAME="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$REPO_NAME:$TAG"

echo "Building NovaRAG Docker image..."
echo "Repository: $REPO_NAME"
echo "Tag: $TAG"
echo "Image: $IMAGE_NAME"
echo ""

cd "$PROJECT_DIR"

# Build image
docker build -t "$REPO_NAME:$TAG" .

# Tag for ECR
docker tag "$REPO_NAME:$TAG" "$IMAGE_NAME"

# Login to ECR
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

# Push to ECR
echo "Pushing to ECR..."
docker push "$IMAGE_NAME"

echo ""
echo "Build complete!"
echo "Image: $IMAGE_NAME"
echo ""
echo "To deploy:"
echo "  ./scripts/deploy.sh $TAG"

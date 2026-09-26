#!/bin/bash
set -e

# Deploy script for NovaRAG to ECS Fargate
# Usage: ./scripts/deploy.sh [tag]
# Auto-discovers ECS cluster, service, and IAM roles

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DEPLOYMENT_DIR="$PROJECT_DIR/deployment"

# Image tag (default: latest)
TAG=${1:-latest}

# Get AWS account ID and region from AWS config or defaults
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=$(aws configure get region 2>/dev/null || echo "ap-southeast-2")

# Load .env file for DATABASE_URL and AWS credentials
if [[ -f "$PROJECT_DIR/.env" ]]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

# Required environment variables
if [[ -z "$AWS_ACCESS_KEY_ID" ]]; then
    echo "Error: AWS_ACCESS_KEY_ID not set (check .env or environment)"
    exit 1
fi

if [[ -z "$AWS_SECRET_ACCESS_KEY" ]]; then
    echo "Error: AWS_SECRET_ACCESS_KEY not set (check .env or environment)"
    exit 1
fi

if [[ -z "$DATABASE_URL" ]]; then
    echo "Error: DATABASE_URL not set (check .env or environment)"
    exit 1
fi

# Auto-discover ECS resources
echo "Discovering ECS resources..."

# Find ECS cluster (look for clusters with "nova" or "rag" in name)
CLUSTER_NAME=$(aws ecs list-clusters --region "$AWS_REGION" --query 'clusterArns[?contains(@, `nova`) || contains(@, `rag`)]' --output text 2>/dev/null | head -1)
if [[ -z "$CLUSTER_NAME" ]]; then
    CLUSTER_NAME=$(aws ecs list-clusters --region "$AWS_REGION" --query 'clusterArns[0]' --output text 2>/dev/null)
fi
# Extract just the cluster name from ARN
CLUSTER_NAME=$(basename "$CLUSTER_NAME" 2>/dev/null || echo "nova-rag-cluster")

# Find ECS service in the cluster
SERVICE_NAME=$(aws ecs list-services --cluster "$CLUSTER_NAME" --region "$AWS_REGION" --query 'serviceArns[0]' --output text 2>/dev/null)
SERVICE_NAME=$(basename "$SERVICE_NAME" 2>/dev/null || echo "nova-rag-service")

# Task family name (typically matches service name without "-service")
TASK_FAMILY="${SERVICE_NAME%-service}"

# Find ECR repository (look for "nova" or "rag" repos)
ECR_REPO_NAME=$(aws ecr describe-repositories --region "$AWS_REGION" --query 'repositories[?contains(repositoryName, `nova`) || contains(repositoryName, `rag`)].repositoryName' --output text 2>/dev/null | head -1)
if [[ -z "$ECR_REPO_NAME" ]]; then
    ECR_REPO_NAME="nova-rag"
fi

echo "  Cluster: $CLUSTER_NAME"
echo "  Service: $SERVICE_NAME"
echo "  Task Family: $TASK_FAMILY"
echo "  ECR Repository: $ECR_REPO_NAME"
echo ""

# Export variables for envsubst
export AWS_ACCOUNT_ID
export AWS_REGION
export IMAGE_TAG="$TAG"
export ECR_REPO_NAME
export AWS_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY
export DATABASE_URL

echo "Deploying NovaRAG to ECS..."
echo "Tag: $TAG"
echo "Region: $AWS_REGION"
echo ""

# Generate task definition with substituted variables
# IMPORTANT: Specify only the variables we want to substitute to avoid truncation
# of DATABASE_URL at special characters like '=' and '?'
TASK_DEF_FILE=$(mktemp)
envsubst '$AWS_ACCOUNT_ID $AWS_REGION $IMAGE_TAG $ECR_REPO_NAME $AWS_ACCESS_KEY_ID $AWS_SECRET_ACCESS_KEY $DATABASE_URL' \
    < "$DEPLOYMENT_DIR/task_definition.json" > "$TASK_DEF_FILE"

# Register new task definition
echo "Registering task definition..."
TASK_DEF_ARN=$(aws ecs register-task-definition \
    --cli-input-json "file://$TASK_DEF_FILE" \
    --region "$AWS_REGION" \
    --query 'taskDefinition.taskDefinitionArn' \
    --output text)
echo "Task definition registered: $TASK_DEF_ARN"

# Clean up temp file
rm -f "$TASK_DEF_FILE"

# Update ECS service
echo "Updating ECS service..."
aws ecs update-service \
    --cluster "$CLUSTER_NAME" \
    --service "$SERVICE_NAME" \
    --task-definition "$TASK_FAMILY" \
    --region "$AWS_REGION" \
    --output table

# Wait for deployment to complete
echo ""
echo "Waiting for deployment to stabilize..."
aws ecs wait services-stable \
    --cluster "$CLUSTER_NAME" \
    --services "$SERVICE_NAME" \
    --region "$AWS_REGION"

echo ""
echo "Deployment complete!"
echo "Service: $SERVICE_NAME"
echo "Task Definition: $TASK_DEF_ARN"

# Get the public IP of the running task
echo ""
echo "Getting task IP address..."
TASK_ARN=$(aws ecs list-tasks \
    --cluster "$CLUSTER_NAME" \
    --service-name "$SERVICE_NAME" \
    --region "$AWS_REGION" \
    --query 'taskArns[0]' \
    --output text)

if [[ -n "$TASK_ARN" ]]; then
    ENI_ID=$(aws ecs describe-tasks \
        --cluster "$CLUSTER_NAME" \
        --tasks "$TASK_ARN" \
        --region "$AWS_REGION" \
        --query 'tasks[0].attachments[0].details[?name==`networkInterfaceId`].value' \
        --output text)

    if [[ -n "$ENI_ID" ]]; then
        PUBLIC_IP=$(aws ec2 describe-network-interfaces \
            --network-interface-ids "$ENI_ID" \
            --region "$AWS_REGION" \
            --query 'NetworkInterfaces[0].Association.PublicIp' \
            --output text)

        echo "Public IP: $PUBLIC_IP"
        echo "API URL: http://$PUBLIC_IP:8000"
        echo ""
        echo "Test with:"
        echo "  curl http://$PUBLIC_IP:8000/health"
        echo ""
        echo "Or update your NOVARAG_API_URL:"
        echo "  export NOVARAG_API_URL=http://$PUBLIC_IP:8000"
    fi
fi

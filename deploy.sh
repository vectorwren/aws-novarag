#!/bin/bash
# NovaRAG Complete Deployment Script
# Deploys: RDS (with auto schema setup) -> Data Ingestion -> ECS Fargate

set -e

# Load environment variables from .env if it exists
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

# Configuration - override via environment variables or .env file
REGION="${AWS_REGION:-ap-southeast-2}"
ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
STACK_NAME="${STACK_NAME:-nova-rag-db-v2}"
CLUSTER_NAME="${CLUSTER_NAME:-nova-rag-cluster}"
SERVICE_NAME="${SERVICE_NAME:-nova-rag-service}"
REPOSITORY_NAME="${REPOSITORY_NAME:-nova-rag}"
IMAGE_TAG="${1:-latest}"

echo "========================================="
echo "  NovaRAG Complete Deployment"
echo "  Region: $REGION"
echo "========================================="

# ============================================
# Get VPC configuration
# ============================================
echo ""
echo "Getting VPC configuration..."
echo "-------------------------------------------"

VPC_ID=$(aws ec2 describe-vpcs --region $REGION --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SUBNETS=$(aws ec2 describe-subnets --region $REGION --filters Name=vpc-id,Values=$VPC_ID --query 'Subnets[?DefaultForAz==`true`].[SubnetId]' --output text | tr '\n' ' ')

# Remove trailing spaces and convert to CSV
SUBNETS_CSV=$(echo "$SUBNETS" | sed 's/ \+$//' | tr ' ' ',' | sed 's/,$//')

echo "VPC: $VPC_ID"
echo "Subnets: $SUBNETS_CSV"

# ============================================
# Create or get security group
# ============================================
echo ""
echo "Setting up security group..."
echo "-------------------------------------------"

# Check if nova-rag-sg exists, if not create it
SG_ID=$(aws ec2 describe-security-groups --region $REGION --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=nova-rag-sg" --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "")

if [ -z "$SG_ID" ] || [ "$SG_ID" == "None" ]; then
    echo "Creating nova-rag-sg security group..."
    SG_ID=$(aws ec2 create-security-group \
        --group-name nova-rag-sg \
        --description "NovaRAG security group" \
        --vpc-id $VPC_ID \
        --region $REGION \
        --query 'GroupId' \
        --output text)

    # Allow PostgreSQL from anywhere (RDS)
    aws ec2 authorize-security-group-ingress \
        --group-id $SG_ID \
        --protocol tcp \
        --port 5432 \
        --cidr 0.0.0.0/0 \
        --region $REGION 2>/dev/null || true

    # Allow HTTP from anywhere (app)
    aws ec2 authorize-security-group-ingress \
        --group-id $SG_ID \
        --protocol tcp \
        --port 8000 \
        --cidr 0.0.0.0/0 \
        --region $REGION 2>/dev/null || true

    echo "Created security group: $SG_ID"
else
    echo "Using existing security group: $SG_ID"
fi

# ============================================
# Step 1: Deploy RDS with CloudFormation
# ============================================
echo ""
echo "Step 1: Deploying RDS PostgreSQL with pgvector..."
echo "-------------------------------------------"

# Create CloudFormation parameters file
cat > /tmp/cfn-params.json << EOF
[
  {
    "ParameterKey": "VpcId",
    "ParameterValue": "$VPC_ID"
  },
  {
    "ParameterKey": "SubnetIds",
    "ParameterValue": "$SUBNETS_CSV"
  },
  {
    "ParameterKey": "SecurityGroupId",
    "ParameterValue": "$SG_ID"
  }
]
EOF

# Check if stack exists and create or update
STACK_EXISTS=$(aws cloudformation describe-stacks --stack-name $STACK_NAME --region $REGION 2>/dev/null && echo "true" || echo "false")

if [ "$STACK_EXISTS" = "true" ]; then
  echo "Updating existing stack..."
  aws cloudformation update-stack \
    --stack-name $STACK_NAME \
    --template-body file://deployment/rds-cloudformation.yaml \
    --region $REGION \
    --capabilities CAPABILITY_IAM \
    --parameters file:///tmp/cfn-params.json >/dev/null 2>&1 || echo "Stack update already in progress or no changes"
else
  echo "Creating new stack..."
  aws cloudformation create-stack \
    --stack-name $STACK_NAME \
    --template-body file://deployment/rds-cloudformation.yaml \
    --region $REGION \
    --capabilities CAPABILITY_IAM \
    --parameters file:///tmp/cfn-params.json >/dev/null
fi

echo "Waiting for RDS stack to complete..."
aws cloudformation wait stack-create-complete --stack-name $STACK_NAME --region $REGION 2>/dev/null || \
aws cloudformation wait stack-update-complete --stack-name $STACK_NAME --region $REGION

# Get RDS outputs
DB_ENDPOINT=$(aws cloudformation describe-stacks --stack-name $STACK_NAME --region $REGION --query 'Stacks[0].Outputs[?OutputKey==`DBEndpoint`].OutputValue' --output text)
DB_PORT=$(aws cloudformation describe-stacks --stack-name $STACK_NAME --region $REGION --query 'Stacks[0].Outputs[?OutputKey==`DBPort`].OutputValue' --output text)
DB_NAME=$(aws cloudformation describe-stacks --stack-name $STACK_NAME --region $REGION --query 'Stacks[0].Outputs[?OutputKey==`DBName`].OutputValue' --output text)
DB_USER=$(aws cloudformation describe-stacks --stack-name $STACK_NAME --region $REGION --query 'Stacks[0].Outputs[?OutputKey==`MasterUsername`].OutputValue' --output text)
DB_SECRET_ARN=$(aws cloudformation describe-stacks --stack-name $STACK_NAME --region $REGION --query 'Stacks[0].Outputs[?OutputKey==`DBSecretArn`].OutputValue' --output text)

echo "RDS Endpoint: $DB_ENDPOINT"
echo "Waiting for RDS to be ready..."
sleep 30  # Wait for RDS to be fully ready

# ============================================
# Step 2: Run database schema setup
# ============================================
echo ""
echo "Step 2: Setting up database schema..."
echo "-------------------------------------------"

DB_PASSWORD=$(aws secretsmanager get-secret-value --secret-id $DB_SECRET_ARN --region $REGION --query 'SecretString' --output text | jq -r '.password')

# Wait for RDS to be ready
for i in {1..30}; do
  if PGPASSWORD="$DB_PASSWORD" psql -h "$DB_ENDPOINT" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "SELECT 1" >/dev/null 2>&1; then
    echo "Database is ready!"
    break
  fi
  echo "Waiting for database... ($i/30)"
  sleep 5
done

# Run schema setup
PGPASSWORD="$DB_PASSWORD" psql -h "$DB_ENDPOINT" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" < scripts/rds_schema.sql
echo "Schema setup complete!"

# ============================================
# Step 3: Update .env with RDS credentials
# ============================================
echo ""
echo "Step 3: Configuring database credentials..."
echo "-------------------------------------------"

cat > .env << EOF
# AWS Configuration
AWS_REGION=$REGION
AWS_ACCESS_KEY_ID=$(aws configure get aws_access_key_id)
AWS_SECRET_ACCESS_KEY=$(aws configure get aws_secret_access_key)

# RDS PostgreSQL Configuration
DB_HOST=$DB_ENDPOINT
DB_NAME=$DB_NAME
DB_USER=$DB_USER
DB_PASSWORD=$DB_PASSWORD
EOF

echo "Database configured!"

# ============================================
# Step 4: Data Ingestion (Embeddings + Upsert)
# ============================================
echo ""
echo "Step 4: Ingesting documentation data..."
echo "-------------------------------------------"

# Step 4a: Generate embeddings
echo "4a. Generating embeddings (may take a few minutes)..."
python3 scripts/step1_generate_embeddings.py

# Step 4b: Upsert to RDS
echo "4b. Upserting to RDS..."
python3 scripts/step2_upsert_to_rds.py

echo "Data ingestion complete!"

# ============================================
# Step 5: Deploy to ECS Fargate
# ============================================
echo ""
echo "Step 5: Deploying to ECS Fargate..."
echo "-------------------------------------------"

# Check if Docker is available
if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker not found. Skipping ECS deployment."
    exit 0
fi

# 5a: Create CloudWatch log group
echo "5a. Creating CloudWatch log group..."
aws logs create-log-group --log-group-name /ecs/nova-rag --region $REGION 2>/dev/null || echo "Log group already exists"

# 5b: Login to ECR
echo "5b. Logging in to ECR..."
aws ecr get-login-password --region $REGION | \
  docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com 2>/dev/null || \
  sudo docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com

# 5c: Create ECR repository if not exists
echo "5c. Ensuring ECR repository exists..."
aws ecr describe-repositories --repository-names $REPOSITORY_NAME --region $REGION >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name $REPOSITORY_NAME --region $REGION >/dev/null

# 5d: Build Docker image
echo "5d. Building Docker image..."
sudo docker build -t ${REPOSITORY_NAME}:${IMAGE_TAG} . || \
  docker build -t ${REPOSITORY_NAME}:${IMAGE_TAG} .

# 5e: Push to ECR
echo "5e. Pushing to ECR..."
sudo docker tag ${REPOSITORY_NAME}:${IMAGE_TAG} ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPOSITORY_NAME}:${IMAGE_TAG} || \
  docker tag ${REPOSITORY_NAME}:${IMAGE_TAG} ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPOSITORY_NAME}:${IMAGE_TAG}

sudo docker push ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPOSITORY_NAME}:${IMAGE_TAG} || \
  docker push ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPOSITORY_NAME}:${IMAGE_TAG}

# 5f: Update task definition with current RDS credentials
echo "5f. Updating task definition..."
TASK_DEF_FILE="deployment/task_definition.json"
TEMP_TASK_DEF=$(mktemp)

# Export variables for envsubst
export AWS_ACCOUNT_ID="$ACCOUNT_ID"
export AWS_REGION="$REGION"
export IMAGE_TAG="$IMAGE_TAG"
export DB_HOST="$DB_ENDPOINT"
export DB_NAME="$DB_NAME"
export DB_USER="$DB_USER"
export DB_PASSWORD="$DB_PASSWORD"
export AWS_ACCESS_KEY_ID="$(aws configure get aws_access_key_id)"
export AWS_SECRET_ACCESS_KEY="$(aws configure get aws_secret_access_key)"

# Substitute environment variables in task definition
envsubst < $TASK_DEF_FILE > $TEMP_TASK_DEF

TASK_DEF_ARN=$(aws ecs register-task-definition \
  --cli-input-json file://$TEMP_TASK_DEF \
  --region $REGION \
  --query 'taskDefinition.taskDefinitionArn' \
  --output text)

rm -f $TEMP_TASK_DEF

# 5g: Create cluster if not exists
echo "5g. Ensuring ECS cluster exists..."
aws ecs describe-clusters --clusters $CLUSTER_NAME --region $REGION >/dev/null 2>&1 || \
  aws ecs create-cluster --cluster-name $CLUSTER_NAME --region $REGION >/dev/null

# 5h: Create or update service
echo "5h. Creating/updating ECS service..."

SERVICE_EXISTS=$(aws ecs describe-services \
  --cluster $CLUSTER_NAME \
  --services $SERVICE_NAME \
  --region $REGION \
  --query 'services[0].status' \
  --output text 2>/dev/null || echo "None")

if [ "$SERVICE_EXISTS" = "ACTIVE" ]; then
    echo "Updating existing service..."
    aws ecs update-service \
      --cluster $CLUSTER_NAME \
      --service $SERVICE_NAME \
      --task-definition $TASK_DEF_ARN \
      --force-new-deployment \
      --region $REGION >/dev/null
else
    echo "Creating new service..."
    aws ecs create-service \
      --cluster $CLUSTER_NAME \
      --service-name $SERVICE_NAME \
      --task-definition $TASK_DEF_ARN \
      --desired-count 1 \
      --launch-type FARGATE \
      --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG_ID],assignPublicIp=ENABLED}" \
      --region $REGION >/dev/null
fi

# 5i: Wait for service to stabilize
echo "5i. Waiting for service to stabilize..."
aws ecs wait services-stable \
  --cluster $CLUSTER_NAME \
  --services $SERVICE_NAME \
  --region $REGION

# ============================================
# Step 6: Get Service Endpoint
# ============================================
echo ""
echo "Step 6: Getting service endpoint..."
echo "-------------------------------------------"

# Get task ENI and public IP
TASK_ARN=$(aws ecs list-tasks --cluster $CLUSTER_NAME --service-name $SERVICE_NAME --region $REGION --query 'taskArns[0]' --output text)

# Wait a bit for task to be fully ready
sleep 10

TASK_ENI=$(aws ecs describe-tasks --cluster $CLUSTER_NAME --tasks $TASK_ARN --region $REGION --query 'tasks[0].attachments[0].details[?name==`networkInterfaceId`].value' --output text)
PUBLIC_IP=$(aws ec2 describe-network-interfaces --network-interface-ids $TASK_ENI --region $REGION --query 'NetworkInterfaces[0].Association.PublicIp' --output text)

# ============================================
# Complete!
# ============================================
echo ""
echo "========================================="
echo "  Deployment Complete!"
echo "========================================="
echo ""
echo "Database: $DB_ENDPOINT:$DB_PORT"
echo "ECS Cluster: $CLUSTER_NAME"
echo "ECS Service: $SERVICE_NAME"
echo ""
echo "Service URL: http://$PUBLIC_IP:8000"
echo ""
echo "To test the deployment:"
echo "  curl http://$PUBLIC_IP:8000/health"
echo "  curl -X POST http://$PUBLIC_IP:8000/query -H 'Content-Type: application/json' -d '{\"question\": \"What is Pydantic AI?\"}'"
echo ""
echo "To check service status:"
echo "  aws ecs describe-services --cluster $CLUSTER_NAME --services $SERVICE_NAME --region $REGION"
echo ""
echo "To view logs:"
echo "  aws logs tail /ecs/nova-rag --follow --region $REGION"
echo "========================================="

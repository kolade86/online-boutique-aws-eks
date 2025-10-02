#!/bin/bash
set -e

REGISTRY="851725533056.dkr.ecr.us-east-1.amazonaws.com"
PREFIX="online-boutique-dev"

SERVICES=(
  "adservice"
  "cartservice"
  "checkoutservice"
  "currencyservice"
  "emailservice"
  "frontend"
  "loadgenerator"
  "paymentservice"
  "productcatalogservice"
  "recommendationservice"
  "shippingservice"
)

echo "Starting build process for ${#SERVICES[@]} services..."
echo ""

for service in "${SERVICES[@]}"; do
  echo "========================================="
  echo "Building $service..."
  echo "========================================="
  
  cd "src/$service"
  docker build -t "$REGISTRY/$PREFIX-$service:latest" .
  docker push "$REGISTRY/$PREFIX-$service:latest"
  cd ../..
  
  echo "✓ $service complete"
  echo ""
done

echo "========================================="
echo "All images built and pushed successfully!"
echo "========================================="
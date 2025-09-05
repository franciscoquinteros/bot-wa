#!/bin/bash

# Deploy optimizado para Playwright en Cloud Run
echo "🚀 Deploying bot with optimized infrastructure..."

gcloud run deploy bot-wa \
  --image gcr.io/$(gcloud config get-value project)/bot-wa \
  --platform managed \
  --region us-central1 \
  --cpu=1 \
  --memory=2Gi \
  --concurrency=10 \
  --min-instances=0 \
  --max-instances=5 \
  --timeout=900s \
  --execution-environment=gen2 \
  --set-env-vars="PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright,DISPLAY=:99" \
  --allow-unauthenticated

echo "✅ Deploy completed with optimized resources"
echo "📊 Configuration:"
echo "  - CPU: 1 vCPU"
echo "  - RAM: 2GB" 
echo "  - Concurrency: 10 requests per instance"
echo "  - Min instances: 0 (scales to zero)"
echo "  - Timeout: 15 minutes"

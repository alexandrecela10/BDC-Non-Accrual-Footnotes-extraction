#!/bin/bash
# Automated overnight setup script
# This script waits for upload, sets up VM, and starts the pipeline

set -e
export PATH="/usr/local/share/google-cloud-sdk/bin:$PATH"

BUCKET="gs://bdc-footnotes-upload"
FILE="bdc_project.tar.gz"
ZONE="us-central1-a"
VM="bdc-pipeline-vm"

echo "=== BDC Footnotes Overnight Setup ==="
echo "Started at: $(date)"

# Step 1: Wait for upload to complete
echo ""
echo "Step 1: Waiting for upload to complete..."
while true; do
    # Check if file exists in bucket
    if gsutil ls -l "$BUCKET/$FILE" 2>/dev/null | grep -q "$FILE"; then
        SIZE=$(gsutil ls -l "$BUCKET/$FILE" 2>/dev/null | awk '{print $1}')
        echo "Upload complete! File size: $SIZE bytes"
        break
    fi
    echo "  $(date '+%H:%M:%S') - Upload still in progress..."
    sleep 60
done

# Step 2: SSH into VM and set everything up
echo ""
echo "Step 2: Setting up VM..."

gcloud compute ssh $VM --zone=$ZONE --command='
set -e
echo "=== VM Setup Started ==="

# Install dependencies
echo "Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3-pip python3-venv screen

# Download from bucket
echo "Downloading project from Cloud Storage..."
gsutil cp gs://bdc-footnotes-upload/bdc_project.tar.gz ~/

# Extract
echo "Extracting project..."
mkdir -p ~/bdc_project
cd ~/bdc_project
tar -xzf ~/bdc_project.tar.gz

# Create venv
echo "Creating Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install Python packages
echo "Installing Python dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

# Create .env file with API keys
echo "Creating .env file..."
cat > .env << EOF
GEMINI_API_KEY=AIzaSyCxRhJAogAbX-l0rpg3ujNTZgRXlLN6w7c
LANGFUSE_SECRET_KEY=sk-lf-5044942f-e477-4e25-ad9e-872b75a06def
LANGFUSE_PUBLIC_KEY=pk-lf-f8a58afd-503e-4a89-81c5-b944197a398f
LANGFUSE_BASE_URL=https://cloud.langfuse.com
EOF

echo "=== VM Setup Complete ==="
'

# Step 3: Start the pipeline in a screen session
echo ""
echo "Step 3: Starting pipeline in screen session..."

gcloud compute ssh $VM --zone=$ZONE --command='
cd ~/bdc_project
source venv/bin/activate

# Start pipeline in detached screen session
screen -dmS pipeline bash -c "
    cd ~/bdc_project
    source venv/bin/activate
    echo \"Pipeline started at: \$(date)\" > pipeline_status.log
    python run_full_pipeline.py --checkpoint-interval 50 >> pipeline_status.log 2>&1
    echo \"Pipeline finished at: \$(date)\" >> pipeline_status.log
"

echo "Pipeline started in screen session!"
echo "To check status later:"
echo "  gcloud compute ssh bdc-pipeline-vm --zone=us-central1-a"
echo "  screen -r pipeline"
'

echo ""
echo "=== ALL DONE ==="
echo "Finished at: $(date)"
echo ""
echo "The pipeline is now running on the VM!"
echo "Check progress tomorrow with:"
echo "  gcloud compute ssh bdc-pipeline-vm --zone=us-central1-a --command='tail -50 ~/bdc_project/pipeline_status.log'"

#!/bin/bash
# Deploy BDC Footnotes Pipeline to Google Cloud VM
# This script creates a VM, uploads the project, and starts the pipeline

set -e

# Configuration
PROJECT_ID="bdc-footnotes-pipeline"
ZONE="us-central1-a"
INSTANCE_NAME="bdc-pipeline-vm"
MACHINE_TYPE="e2-medium"  # 2 vCPU, 4GB RAM - good balance of cost/performance

echo "=== BDC Footnotes Pipeline - Google Cloud Deployment ==="
echo ""

# Check if gcloud is installed
if ! command -v gcloud &> /dev/null; then
    echo "ERROR: gcloud CLI not found. Please install it first:"
    echo "  brew install --cask google-cloud-sdk"
    exit 1
fi

# Check if logged in
if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | grep -q "@"; then
    echo "Not logged in to Google Cloud. Running gcloud init..."
    gcloud init
fi

# Create project if it doesn't exist
echo "Setting up project..."
gcloud projects describe $PROJECT_ID 2>/dev/null || gcloud projects create $PROJECT_ID

# Set project
gcloud config set project $PROJECT_ID

# Enable Compute Engine API
echo "Enabling Compute Engine API..."
gcloud services enable compute.googleapis.com

# Create VM instance
echo "Creating VM instance: $INSTANCE_NAME..."
gcloud compute instances create $INSTANCE_NAME \
    --zone=$ZONE \
    --machine-type=$MACHINE_TYPE \
    --image-family=ubuntu-2204-lts \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size=50GB \
    --boot-disk-type=pd-standard \
    --tags=http-server,https-server \
    --metadata=startup-script='#!/bin/bash
apt-get update
apt-get install -y python3-pip python3-venv git screen
' || echo "VM may already exist, continuing..."

# Wait for VM to be ready
echo "Waiting for VM to be ready..."
sleep 30

# Get external IP
EXTERNAL_IP=$(gcloud compute instances describe $INSTANCE_NAME --zone=$ZONE --format='get(networkInterfaces[0].accessConfigs[0].natIP)')
echo "VM External IP: $EXTERNAL_IP"

# Create tarball of project (excluding large files)
echo "Creating project archive..."
cd "$(dirname "$0")"
tar --exclude='venv' --exclude='*.pyc' --exclude='__pycache__' \
    --exclude='.git' --exclude='BDC Footnotes' \
    -czvf /tmp/bdc_project.tar.gz .

# Upload to VM
echo "Uploading project to VM..."
gcloud compute scp /tmp/bdc_project.tar.gz $INSTANCE_NAME:~ --zone=$ZONE

# Also upload the BDC Footnotes data folder (this is large, may take a while)
echo "Uploading BDC Footnotes data (this may take a while)..."
gcloud compute scp --recurse "BDC Footnotes" $INSTANCE_NAME:~ --zone=$ZONE

# Setup and run on VM
echo "Setting up environment on VM..."
gcloud compute ssh $INSTANCE_NAME --zone=$ZONE --command='
    # Extract project
    mkdir -p ~/bdc_project
    cd ~/bdc_project
    tar -xzf ~/bdc_project.tar.gz
    
    # Move data folder
    mv ~/BDC\ Footnotes ~/bdc_project/
    
    # Create virtual environment
    python3 -m venv venv
    source venv/bin/activate
    
    # Install dependencies
    pip install --upgrade pip
    pip install -r requirements.txt
    
    # Create .env file (you need to add your API keys)
    echo "Please add your API keys to ~/bdc_project/.env"
'

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "Next steps:"
echo "1. SSH into the VM:"
echo "   gcloud compute ssh $INSTANCE_NAME --zone=$ZONE"
echo ""
echo "2. Add your API keys to .env:"
echo "   nano ~/bdc_project/.env"
echo ""
echo "3. Start the pipeline in a screen session:"
echo "   cd ~/bdc_project"
echo "   screen -S pipeline"
echo "   source venv/bin/activate"
echo "   python run_full_pipeline.py --checkpoint-interval 50"
echo ""
echo "4. Detach from screen: Ctrl+A, then D"
echo "5. Reattach later: screen -r pipeline"
echo ""
echo "VM IP: $EXTERNAL_IP"

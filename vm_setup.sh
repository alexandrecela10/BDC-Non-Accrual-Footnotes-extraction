#!/bin/bash
# VM Setup Script - Run this after uploading bdc_project.tar.gz
# Usage: bash vm_setup.sh

set -e

echo "=== Setting up BDC Footnotes Pipeline ==="

# Install system dependencies
echo "Installing system packages..."
sudo apt-get update
sudo apt-get install -y python3-pip python3-venv screen

# Extract project
echo "Extracting project files..."
mkdir -p ~/bdc_project
cd ~/bdc_project
tar -xzf ~/bdc_project.tar.gz

# Create virtual environment
echo "Creating Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
echo "Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "1. Add your API keys to .env:"
echo "   nano ~/bdc_project/.env"
echo ""
echo "2. Start the pipeline in a screen session:"
echo "   cd ~/bdc_project"
echo "   screen -S pipeline"
echo "   source venv/bin/activate"
echo "   python run_full_pipeline.py --checkpoint-interval 50"
echo ""
echo "3. Detach from screen: Ctrl+A, then D"
echo "4. Reattach later: screen -r pipeline"

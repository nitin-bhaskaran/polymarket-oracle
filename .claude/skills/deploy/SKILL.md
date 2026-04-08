---
name: deploy
description: Deployment procedures for Polymarket Oracle to GCP. Handles VM setup, service configuration, and deployment. Use when deploying or updating the bot on GCP.
allowed tools: Read, Bash
---

# Deploy Skill — Polymarket Oracle

## GCP Deployment

### First-time Setup
```bash
# Create VM
gcloud compute instances create polymarket-oracle-vm \
  --zone=europe-west2-a \
  --machine-type=e2-micro \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=20GB

# SSH and run setup
gcloud compute ssh polymarket-oracle-vm
curl -sSL https://raw.githubusercontent.com/nitin-bhaskaran/polymarket-oracle/main/scripts/setup_gcp.sh | bash
```

### Update Deployment
```bash
gcloud compute ssh polymarket-oracle-vm
cd ~/polymarket-oracle
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart polymarket-oracle
sudo journalctl -u polymarket-oracle -f
```

### Pre-deploy Checklist
1. All tests pass locally
2. Dry-run completes without errors
3. config.yaml has correct credentials on VM
4. No secrets in committed code (check git diff)
5. Portfolio state backed up if schema changed

### Rollback
```bash
cd ~/polymarket-oracle
git log --oneline -5  # Find the commit to roll back to
git checkout <commit-hash>
sudo systemctl restart polymarket-oracle
```

### Monitoring
```bash
sudo journalctl -u polymarket-oracle -f          # Live logs
sudo systemctl status polymarket-oracle           # Service status
cat ~/polymarket-oracle/data/portfolio_state.json  # Portfolio state
```

# Deploy Command

Run the pre-deploy checklist and deploy to GCP.

## Steps
1. Run all tests: `python -m pytest tests/ -v`
2. Run dry-run scan: `python -m core.main --dry-run --scan-once`
3. Check for secrets in staged files: `bash scripts/sec.sh`
4. If all pass, commit and push to main
5. SSH to GCP VM and pull + restart service

## Usage
```
/deploy
```

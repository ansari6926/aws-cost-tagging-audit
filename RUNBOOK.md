# RUNBOOK — aws-cost-tagging-audit

This runbook describes how to run the audit on a schedule using:

1. [Linux/macOS cron](#option-a-linux--macos-cron)
2. [AWS Lambda + EventBridge (recommended for production)](#option-b-aws-lambda--eventbridge)
3. [GitHub Actions (CI/CD alternative)](#option-c-github-actions)

---

## Option A: Linux / macOS cron

### 1. Create a wrapper shell script

```bash
#!/usr/bin/env bash
# /opt/scripts/run_audit.sh

set -euo pipefail

export AWS_ACCESS_KEY_ID="AKIA..."
export AWS_SECRET_ACCESS_KEY="wJalr..."
export AWS_DEFAULT_REGION="us-east-1"

cd /opt/aws-cost-tagging-audit
source .venv/bin/activate
python audit.py --output-dir /opt/reports/$(date +%Y-%m-%d)
```

Make it executable:

```bash
chmod +x /opt/scripts/run_audit.sh
```

### 2. Add a cron entry

```bash
crontab -e
```

Run every Monday at 08:00 AM:

```
0 8 * * 1  /opt/scripts/run_audit.sh >> /var/log/aws-audit.log 2>&1
```

Run on the 1st of every month at 07:00 AM:

```
0 7 1 * *  /opt/scripts/run_audit.sh >> /var/log/aws-audit.log 2>&1
```

---

## Option B: AWS Lambda + EventBridge

This is the recommended approach for production: no EC2 required, auto-scales,
and credentials are securely managed via IAM roles.

### Architecture

```
EventBridge Rule (cron)
        │
        ▼
  Lambda Function (Python 3.12)
        │
        ├─► Cost Explorer API
        ├─► Resource Groups Tagging API
        │
        ▼
  S3 Bucket  ─► report.csv + report.html
        │
        ▼
  (Optional) SNS → Email notification
```

### Step 1: Package the Lambda function

```bash
# From the project root
pip install -r requirements.txt -t ./package
cp audit.py ./package/
cd package && zip -r ../audit_lambda.zip . && cd ..
```

### Step 2: Create the Lambda function

```bash
aws lambda create-function \
  --function-name aws-cost-tagging-audit \
  --runtime python3.12 \
  --role arn:aws:iam::ACCOUNT_ID:role/LambdaAuditRole \
  --handler audit.main \
  --zip-file fileb://audit_lambda.zip \
  --timeout 300 \
  --memory-size 256
```

> **IAM Role for Lambda** must have:
> - `ce:GetCostAndUsage`
> - `tag:GetResources`
> - `s3:PutObject` (to save reports to S3)
> - Basic Lambda execution role (`AWSLambdaBasicExecutionRole`)

### Step 3: Modify `audit.py` for Lambda handler

The `main()` function works as-is. Add a Lambda handler wrapper:

```python
# In audit.py or a separate lambda_handler.py
import json

def lambda_handler(event, context):
    """AWS Lambda entry point."""
    exit_code = main()
    return {
        "statusCode": 200 if exit_code == 0 else 500,
        "body": json.dumps("Audit complete")
    }
```

Update `--handler` to `audit.lambda_handler` when creating/updating the function.

### Step 4: Create an EventBridge rule

```bash
# Every Monday at 08:00 UTC
aws events put-rule \
  --name "WeeklyAWSAudit" \
  --schedule-expression "cron(0 8 ? * MON *)" \
  --state ENABLED

# Add Lambda as the target
aws events put-targets \
  --rule "WeeklyAWSAudit" \
  --targets "Id=1,Arn=arn:aws:lambda:us-east-1:ACCOUNT_ID:function:aws-cost-tagging-audit"
```

### Step 5: Grant EventBridge permission to invoke Lambda

```bash
aws lambda add-permission \
  --function-name aws-cost-tagging-audit \
  --statement-id EventBridgeInvoke \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:us-east-1:ACCOUNT_ID:rule/WeeklyAWSAudit
```

---

## Option C: GitHub Actions

Schedule the audit as a GitHub Actions workflow, storing reports as artifacts.

Create `.github/workflows/weekly-audit.yml`:

```yaml
name: AWS Cost & Tagging Audit

on:
  schedule:
    - cron: "0 8 * * 1"   # Every Monday at 08:00 UTC
  workflow_dispatch:        # Also allow manual trigger

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run audit
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          AWS_DEFAULT_REGION: ${{ secrets.AWS_DEFAULT_REGION }}
        run: python audit.py --output-dir ./reports

      - name: Upload reports
        uses: actions/upload-artifact@v4
        with:
          name: audit-reports-${{ github.run_id }}
          path: reports/
          retention-days: 30
```

Set `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_DEFAULT_REGION` as
**GitHub repository secrets** (Settings → Secrets and variables → Actions).

---

## Alerting

### SNS Email on completion (Lambda)

```python
import boto3

def notify(subject: str, message: str) -> None:
    sns = boto3.client("sns")
    sns.publish(
        TopicArn="arn:aws:sns:us-east-1:ACCOUNT_ID:AuditAlerts",
        Subject=subject,
        Message=message,
    )
```

Call `notify("Audit Complete", f"{len(untagged_rows)} untagged resources found")` at the end of `main()`.

### Slack Webhook (optional)

```python
import urllib.request, json

def slack_notify(webhook_url: str, message: str) -> None:
    data = json.dumps({"text": message}).encode()
    req = urllib.request.Request(webhook_url, data=data,
                                  headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req)
```

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `AccessDeniedException` on Cost Explorer | Missing `ce:GetCostAndUsage` permission | Add to IAM policy |
| `AccessDeniedException` on Tagging API | Missing `tag:GetResources` permission | Add to IAM policy |
| `No credentials found` | Env vars not set | Export `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` |
| Empty cost report | Cost Explorer has 24-48 h data lag | Normal; wait a day |
| Lambda timeout | Large accounts with many resources | Increase Lambda timeout (max 15 min) |

---

## Cost of Running This Tool

| API Call | Cost |
|----------|------|
| `ce:GetCostAndUsage` | First 1 request/day free; $0.01 per request after |
| `tag:GetResources` | Free (part of AWS free tier) |
| Lambda execution | Free tier covers ~1M invocations/month |
| EventBridge rule | $1.00 per million events |

Running weekly = ~4 Cost Explorer calls/month ≈ **< $0.04/month**.

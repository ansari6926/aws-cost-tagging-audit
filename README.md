# aws-cost-tagging-audit

> **Automated AWS cost visibility + tag compliance auditing in one Python script.**

---

## Problem Statement

In many AWS environments, costs balloon silently across dozens of services, and resources
are created without consistent tagging. This makes it impossible to:

- Attribute spend to teams or projects (no **Owner** tag)
- Distinguish production from dev resources (no **Environment** tag)
- Act on cost spikes quickly

`aws-cost-tagging-audit` solves both problems in a single run:

1. **Cost report** — pulls 30 days of unblended spend from AWS Cost Explorer, grouped by service.
2. **Tag compliance** — scans all EC2 and S3 resources for missing `Owner` and `Environment` tags.
3. **Dual output** — writes a machine-readable `report.csv` *and* a styled `report.html` dashboard.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      audit.py                               │
│                                                             │
│  ┌─────────────────┐        ┌──────────────────────────┐   │
│  │  Cost Explorer  │        │  Resource Groups Tagging │   │
│  │  get_cost_and   │        │  API  get_resources()    │   │
│  │  _usage()       │        │  (paginated)             │   │
│  └────────┬────────┘        └───────────┬──────────────┘   │
│           │  cost_rows                  │  untagged_rows   │
│           └────────────┬────────────────┘                  │
│                        ▼                                    │
│              write_csv()  write_html()                      │
│                        │                                    │
│               report.csv  report.html                       │
└─────────────────────────────────────────────────────────────┘
```

---

## Prerequisites

| Tool | Version |
|------|---------|
| Python | ≥ 3.11 |
| pip | latest |
| AWS credentials | IAM user or role with permissions below |

### Required IAM permissions

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "tag:GetResources"
      ],
      "Resource": "*"
    }
  ]
}
```

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/<YOUR_GH_USERNAME>/aws-cost-tagging-audit.git
cd aws-cost-tagging-audit
```

### 2. Create a virtual environment

```bash
python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Export AWS credentials as environment variables

```bash
export AWS_ACCESS_KEY_ID="AKIA..."
export AWS_SECRET_ACCESS_KEY="wJalr..."
export AWS_DEFAULT_REGION="us-east-1"

# Optional — for temporary session tokens (STS / SSO)
export AWS_SESSION_TOKEN="..."
```
## How to Run

```bash
python audit.py
```

Optional flags:

```
--profile PROFILE   Use a named AWS profile (local dev only)
--region  REGION    AWS region for Tagging API (default: us-east-1)
--output-dir DIR    Directory for output files (default: current dir)
```

Example:

```bash
python audit.py --region eu-west-1 --output-dir ./reports

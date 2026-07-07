#!/usr/bin/env python3
"""
aws-cost-tagging-audit
======================
Pulls the last 30 days of AWS cost data grouped by service (Cost Explorer),
audits EC2 and S3 resources for missing "Owner" and "Environment" tags
(Resource Groups Tagging API), and writes results to report.csv and report.html.

AWS credentials are read from environment variables:
  AWS_ACCESS_KEY_ID
  AWS_SECRET_ACCESS_KEY
  AWS_DEFAULT_REGION   (optional, defaults to us-east-1)
  AWS_SESSION_TOKEN    (optional, for temporary credentials)

Usage:
    python audit.py [--profile PROFILE] [--region REGION]
"""

import argparse
import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# ── Required tags ──────────────────────────────────────────────────────────────
REQUIRED_TAGS = ["Owner", "Environment"]

# ── Resource types audited via Tagging API ─────────────────────────────────────
RESOURCE_TYPE_FILTERS = [
    "ec2:instance",
    "ec2:volume",
    "ec2:snapshot",
    "s3",
]


def get_session(profile: str | None, region: str) -> boto3.Session:
    """Build a boto3 session from env vars (or named profile for local dev)."""
    if profile:
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=os.environ.get("AWS_SESSION_TOKEN"),
        region_name=os.environ.get("AWS_DEFAULT_REGION", region),
    )


# ── Cost Explorer ──────────────────────────────────────────────────────────────

def fetch_cost_by_service(session: boto3.Session) -> list[dict]:
    """
    Return last-30-day costs grouped by AWS service.
    Returns a list of dicts: {service, start, end, amount, unit}
    """
    ce = session.client("ce", region_name="us-east-1")  # CE is global, always us-east-1
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=30)

    print(f"[Cost Explorer] Fetching costs from {start} to {end} …")
    try:
        response = ce.get_cost_and_usage(
            TimePeriod={"Start": str(start), "End": str(end)},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
    except (BotoCoreError, ClientError) as exc:
        print(f"  [ERROR] Cost Explorer call failed: {exc}", file=sys.stderr)
        return []

    rows = []
    for result_by_time in response.get("ResultsByTime", []):
        period_start = result_by_time["TimePeriod"]["Start"]
        period_end = result_by_time["TimePeriod"]["End"]
        for group in result_by_time.get("Groups", []):
            service = group["Keys"][0]
            metrics = group["Metrics"]["UnblendedCost"]
            rows.append(
                {
                    "service": service,
                    "start": period_start,
                    "end": period_end,
                    "amount": float(metrics["Amount"]),
                    "unit": metrics["Unit"],
                }
            )

    rows.sort(key=lambda r: r["amount"], reverse=True)
    print(f"  → {len(rows)} service cost entries retrieved.")
    return rows


# ── Resource Groups Tagging API ────────────────────────────────────────────────

def fetch_untagged_resources(session: boto3.Session) -> list[dict]:
    """
    Page through all EC2/S3 resources and flag those missing required tags.
    Returns a list of dicts: {resource_arn, resource_type, missing_tags, existing_tags}
    """
    tagging = session.client("resourcegroupstaggingapi")
    untagged = []
    paginator = tagging.get_paginator("get_resources")

    print(f"[Tagging API] Scanning resource types: {RESOURCE_TYPE_FILTERS} …")
    try:
        pages = paginator.paginate(
            ResourceTypeFilters=RESOURCE_TYPE_FILTERS,
            ResourcesPerPage=100,
        )
        total = 0
        for page in pages:
            for resource in page.get("ResourceTagMappingList", []):
                total += 1
                arn = resource["ResourceARN"]
                existing_keys = {t["Key"]: t["Value"] for t in resource.get("Tags", [])}
                missing = [t for t in REQUIRED_TAGS if t not in existing_keys]
                if missing:
                    # Derive a short resource type from ARN
                    # arn:aws:ec2:us-east-1:123:instance/i-abc → ec2:instance
                    parts = arn.split(":")
                    res_type = f"{parts[2]}:{parts[5].split('/')[0]}" if len(parts) > 5 else parts[2]
                    untagged.append(
                        {
                            "resource_arn": arn,
                            "resource_type": res_type,
                            "missing_tags": ", ".join(missing),
                            "existing_tags": ", ".join(
                                f"{k}={v}" for k, v in existing_keys.items()
                            ),
                        }
                    )
        print(f"  → {total} resources scanned; {len(untagged)} missing required tags.")
    except (BotoCoreError, ClientError) as exc:
        print(f"  [ERROR] Tagging API call failed: {exc}", file=sys.stderr)

    return untagged


# ── CSV Output ─────────────────────────────────────────────────────────────────

def write_csv(
    cost_rows: list[dict],
    untagged_rows: list[dict],
    output_path: Path = Path("report.csv"),
) -> None:
    print(f"[CSV] Writing {output_path} …")
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        # ── Section 1: Costs ──
        writer.writerow(["=== AWS Cost by Service (Last 30 Days) ==="])
        writer.writerow(["Service", "Period Start", "Period End", "Amount (USD)", "Unit"])
        for r in cost_rows:
            writer.writerow([r["service"], r["start"], r["end"], f"{r['amount']:.4f}", r["unit"]])

        writer.writerow([])  # blank separator

        # ── Section 2: Untagged Resources ──
        writer.writerow(["=== Resources Missing Required Tags ==="])
        writer.writerow(
            ["Resource ARN", "Resource Type", "Missing Tags", "Existing Tags"]
        )
        for r in untagged_rows:
            writer.writerow(
                [r["resource_arn"], r["resource_type"], r["missing_tags"], r["existing_tags"]]
            )

    print(f"  → Saved: {output_path}")


# ── HTML Output ────────────────────────────────────────────────────────────────

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>AWS Cost &amp; Tagging Audit Report</title>
  <style>
    :root {{
      --bg: #0d1117;
      --surface: #161b22;
      --surface2: #21262d;
      --border: #30363d;
      --accent: #58a6ff;
      --green: #3fb950;
      --red: #f85149;
      --yellow: #d29922;
      --text: #c9d1d9;
      --muted: #8b949e;
      --radius: 10px;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 32px 24px;
      min-height: 100vh;
    }}
    header {{
      display: flex;
      align-items: center;
      gap: 16px;
      margin-bottom: 36px;
    }}
    .logo {{
      width: 48px; height: 48px;
      background: linear-gradient(135deg, #ff9900 0%, #ff6b35 100%);
      border-radius: 12px;
      display: flex; align-items: center; justify-content: center;
      font-size: 24px; font-weight: 800; color: #fff;
      flex-shrink: 0;
    }}
    header h1 {{
      font-size: 1.6rem;
      font-weight: 700;
      background: linear-gradient(90deg, #ff9900, #58a6ff);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }}
    header p.subtitle {{ font-size: 0.85rem; color: var(--muted); margin-top: 2px; }}
    .meta {{
      display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 36px;
    }}
    .badge {{
      background: var(--surface2);
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 6px 16px;
      font-size: 0.78rem;
      color: var(--muted);
    }}
    .badge strong {{ color: var(--text); }}
    .section {{ margin-bottom: 48px; }}
    .section-title {{
      font-size: 1.05rem;
      font-weight: 600;
      margin-bottom: 16px;
      display: flex; align-items: center; gap: 10px;
      color: var(--text);
    }}
    .section-title .icon {{
      width: 28px; height: 28px; border-radius: 8px;
      display: flex; align-items: center; justify-content: center;
      font-size: 14px;
    }}
    .icon-cost   {{ background: rgba(255,153,0,.15); }}
    .icon-tag    {{ background: rgba(248,81,73,.15); }}
    .table-wrap {{
      overflow-x: auto;
      border: 1px solid var(--border);
      border-radius: var(--radius);
    }}
    table {{
      width: 100%; border-collapse: collapse; font-size: 0.85rem;
    }}
    thead th {{
      background: var(--surface2);
      padding: 12px 16px;
      text-align: left;
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
    }}
    tbody tr {{
      border-bottom: 1px solid var(--border);
      transition: background 0.15s;
    }}
    tbody tr:last-child {{ border-bottom: none; }}
    tbody tr:hover {{ background: var(--surface2); }}
    tbody td {{
      padding: 11px 16px;
      vertical-align: top;
      word-break: break-all;
    }}
    .amount {{ font-variant-numeric: tabular-nums; font-family: monospace; }}
    .amount-high {{ color: var(--red); font-weight: 600; }}
    .amount-mid  {{ color: var(--yellow); }}
    .amount-low  {{ color: var(--green); }}
    .chip {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 0.72rem;
      font-weight: 600;
    }}
    .chip-missing {{ background: rgba(248,81,73,.15); color: var(--red); }}
    .chip-type    {{ background: rgba(88,166,255,.12); color: var(--accent); }}
    .no-data {{
      text-align: center; padding: 40px; color: var(--muted); font-size: 0.9rem;
    }}
    footer {{
      margin-top: 60px; border-top: 1px solid var(--border);
      padding-top: 20px; font-size: 0.78rem; color: var(--muted);
      text-align: center;
    }}
  </style>
</head>
<body>
<header>
  <div class="logo">☁</div>
  <div>
    <h1>AWS Cost &amp; Tagging Audit Report</h1>
    <p class="subtitle">Automated audit — last 30 days of spend + resource tag compliance</p>
  </div>
</header>

<div class="meta">
  <span class="badge">Generated: <strong>{generated_at}</strong></span>
  <span class="badge">Services tracked: <strong>{num_services}</strong></span>
  <span class="badge">Untagged resources: <strong>{num_untagged}</strong></span>
  <span class="badge">Required tags: <strong>Owner, Environment</strong></span>
</div>

<!-- ── Cost Section ── -->
<div class="section">
  <div class="section-title">
    <span class="icon icon-cost">💰</span>
    AWS Cost by Service &mdash; Last 30 Days
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Service</th>
          <th>Period Start</th>
          <th>Period End</th>
          <th>Amount (USD)</th>
          <th>Unit</th>
        </tr>
      </thead>
      <tbody>
        {cost_rows}
      </tbody>
    </table>
  </div>
</div>

<!-- ── Tagging Section ── -->
<div class="section">
  <div class="section-title">
    <span class="icon icon-tag">🏷️</span>
    Resources Missing Required Tags
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Resource ARN</th>
          <th>Type</th>
          <th>Missing Tags</th>
          <th>Existing Tags</th>
        </tr>
      </thead>
      <tbody>
        {tag_rows}
      </tbody>
    </table>
  </div>
</div>

<footer>
  aws-cost-tagging-audit &bull; Generated {generated_at} &bull;
  Credentials via environment variables &bull; Never hardcoded.
</footer>
</body>
</html>
"""


def _amount_class(amount: float) -> str:
    if amount >= 100:
        return "amount-high"
    if amount >= 10:
        return "amount-mid"
    return "amount-low"


def write_html(
    cost_rows: list[dict],
    untagged_rows: list[dict],
    output_path: Path = Path("report.html"),
) -> None:
    print(f"[HTML] Writing {output_path} …")
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Build cost table rows
    if cost_rows:
        cost_html_rows = []
        for i, r in enumerate(cost_rows, 1):
            cls = _amount_class(r["amount"])
            cost_html_rows.append(
                f"<tr>"
                f"<td>{i}</td>"
                f"<td>{r['service']}</td>"
                f"<td>{r['start']}</td>"
                f"<td>{r['end']}</td>"
                f"<td class='amount {cls}'>${r['amount']:,.4f}</td>"
                f"<td>{r['unit']}</td>"
                f"</tr>"
            )
        cost_rows_html = "\n".join(cost_html_rows)
    else:
        cost_rows_html = "<tr><td colspan='6' class='no-data'>No cost data retrieved.</td></tr>"

    # Build tag table rows
    if untagged_rows:
        tag_html_rows = []
        for i, r in enumerate(untagged_rows, 1):
            missing_chips = "".join(
                f"<span class='chip chip-missing'>{t.strip()}</span> "
                for t in r["missing_tags"].split(",")
                if t.strip()
            )
            tag_html_rows.append(
                f"<tr>"
                f"<td>{i}</td>"
                f"<td style='font-family:monospace;font-size:0.78rem'>{r['resource_arn']}</td>"
                f"<td><span class='chip chip-type'>{r['resource_type']}</span></td>"
                f"<td>{missing_chips}</td>"
                f"<td style='color:var(--muted);font-size:0.80rem'>{r['existing_tags'] or '—'}</td>"
                f"</tr>"
            )
        tag_rows_html = "\n".join(tag_html_rows)
    else:
        tag_rows_html = (
            "<tr><td colspan='5' class='no-data'>✅ All scanned resources have required tags.</td></tr>"
        )

    html = _HTML_TEMPLATE.format(
        generated_at=generated_at,
        num_services=len(cost_rows),
        num_untagged=len(untagged_rows),
        cost_rows=cost_rows_html,
        tag_rows=tag_rows_html,
    )

    output_path.write_text(html, encoding="utf-8")
    print(f"  → Saved: {output_path}")


# ── CLI Entry Point ────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit AWS costs and resource tag compliance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="AWS named profile (for local dev). Ignored in CI/Lambda.",
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region for Tagging API calls (default: us-east-1).",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory to write report.csv and report.html (default: current dir).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  AWS Cost & Tagging Audit")
    print("=" * 60)

    session = get_session(args.profile, args.region)

    cost_rows = fetch_cost_by_service(session)
    untagged_rows = fetch_untagged_resources(session)

    write_csv(cost_rows, untagged_rows, output_dir / "report.csv")
    write_html(cost_rows, untagged_rows, output_dir / "report.html")

    print()
    print("=" * 60)
    print("  Audit complete.")
    print(f"  Cost entries   : {len(cost_rows)}")
    print(f"  Untagged resrcs: {len(untagged_rows)}")
    print(f"  Reports saved to: {output_dir.resolve()}/")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

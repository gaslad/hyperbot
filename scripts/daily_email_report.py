#!/usr/bin/env python3
"""Daily Email Report — sends the Hyperbot daily trade report via Gmail SMTP.

Setup:
    1. Enable 2FA on your Google account
    2. Generate an App Password: https://myaccount.google.com/apppasswords
    3. Set environment variables:
         export HYPERBOT_EMAIL_FROM="youremail@gmail.com"
         export HYPERBOT_EMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
         export HYPERBOT_EMAIL_TO="recipient@gmail.com"
       Or create ~/.hyperbot/email.json:
         {"from": "...", "app_password": "...", "to": "..."}

Usage:
    python3 scripts/daily_email_report.py [workspace_path]
    python3 scripts/daily_email_report.py --dry-run   # print report without sending

The dashboard calls this automatically once per UTC day.
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


def load_email_config() -> dict[str, str]:
    """Load email configuration from env vars or config file."""
    config: dict[str, str] = {}

    # Try env vars first
    config["from"] = os.environ.get("HYPERBOT_EMAIL_FROM", "")
    config["app_password"] = os.environ.get("HYPERBOT_EMAIL_APP_PASSWORD", "")
    config["to"] = os.environ.get("HYPERBOT_EMAIL_TO", "")

    # Fall back to config file
    if not config["from"] or not config["app_password"]:
        config_path = Path.home() / ".hyperbot" / "email.json"
        if config_path.exists():
            try:
                file_config = json.loads(config_path.read_text(encoding="utf-8"))
                config["from"] = config["from"] or file_config.get("from", "")
                config["app_password"] = config["app_password"] or file_config.get("app_password", "")
                config["to"] = config["to"] or file_config.get("to", "")
            except (json.JSONDecodeError, OSError) as e:
                print(f"  [email] Config file error: {e}", flush=True)

    return config


def markdown_to_html(md: str) -> str:
    """Convert simple markdown to HTML email body."""
    lines = md.split("\n")
    html_lines = []
    in_list = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append("<br>")
            continue

        if stripped.startswith("# "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f'<h1 style="font-family:Georgia,serif;color:#1a1917;font-size:24px;margin:16px 0 8px">{stripped[2:]}</h1>')
        elif stripped.startswith("## "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f'<h2 style="font-family:Georgia,serif;color:#2c2a26;font-size:18px;margin:20px 0 8px;border-bottom:1px solid #e8e3d9;padding-bottom:4px">{stripped[3:]}</h2>')
        elif stripped.startswith("- "):
            if not in_list:
                html_lines.append('<ul style="padding-left:20px;margin:4px 0">')
                in_list = True
            content = stripped[2:]
            # Bold markers
            import re
            content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', content)
            html_lines.append(f'<li style="font-size:14px;color:#2c2a26;line-height:1.6;margin:4px 0">{content}</li>')
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            import re
            content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', stripped)
            html_lines.append(f'<p style="font-size:14px;color:#2c2a26;line-height:1.6;margin:4px 0">{content}</p>')

    if in_list:
        html_lines.append("</ul>")

    body_html = "\n".join(html_lines)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#faf8f4;font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif">
<div style="max-width:600px;margin:0 auto;padding:32px 24px">
  <div style="text-align:center;margin-bottom:24px">
    <div style="display:inline-block;width:40px;height:40px;border-radius:10px;background:linear-gradient(135deg,#3d5a3a,#5c7a55);color:#d4cfbf;font-family:Georgia,serif;font-size:20px;line-height:40px;text-align:center">H</div>
    <div style="font-size:12px;color:#9e978d;margin-top:6px;letter-spacing:1px">HYPERBOT DAILY REPORT</div>
  </div>
  <div style="background:#fff;border:1px solid rgba(0,0,0,0.06);border-radius:16px;padding:28px;box-shadow:0 2px 8px rgba(0,0,0,0.03)">
    {body_html}
  </div>
  <div style="text-align:center;margin-top:20px;font-size:11px;color:#c4bfb4">
    Sent by Hyperbot &middot; {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
  </div>
</div>
</body>
</html>"""


def send_report(
    markdown: str,
    subject: str,
    config: dict[str, str],
) -> bool:
    """Send an email via Gmail SMTP with App Password."""
    sender = config["from"]
    password = config["app_password"]
    recipient = config["to"]

    if not sender or not password or not recipient:
        print("  [email] Missing email configuration. Set HYPERBOT_EMAIL_FROM, HYPERBOT_EMAIL_APP_PASSWORD, HYPERBOT_EMAIL_TO", flush=True)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Hyperbot <{sender}>"
    msg["To"] = recipient

    # Plain text version
    msg.attach(MIMEText(markdown, "plain"))

    # HTML version
    html = markdown_to_html(markdown)
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, [recipient], msg.as_string())
        print(f"  [email] Daily report sent to {recipient}", flush=True)
        return True
    except smtplib.SMTPAuthenticationError:
        print("  [email] Gmail authentication failed. Check your App Password.", flush=True)
        print("  [email] Generate one at: https://myaccount.google.com/apppasswords", flush=True)
        return False
    except Exception as e:
        print(f"  [email] Send failed: {e}", flush=True)
        return False


def run(workspace_path: Path, dry_run: bool = False) -> bool:
    """Generate daily report and send via email."""
    sys.path.insert(0, str(workspace_path / "scripts"))

    try:
        from trade_journal import TradeJournal
    except ImportError:
        print("  [email] Could not import trade_journal. Run from workspace directory.", flush=True)
        return False

    manifest_path = workspace_path / "hyperbot.workspace.json"
    journal = TradeJournal(workspace_path, manifest_path=manifest_path)

    # Sync fills first
    try:
        import hl_client
        creds = hl_client.get_credentials()
        address = creds.get("master_address")
        if address:
            count = journal.sync_user_fills(address, hl_client.HL_MAINNET)
            if count > 0:
                print(f"  [email] Synced {count} new fill(s) from Hyperliquid", flush=True)
    except Exception as e:
        print(f"  [email] Fill sync error (continuing with cached data): {e}", flush=True)

    # Build report
    report = journal.build_daily_report()
    if not report:
        print("  [email] No trading activity in the last 24 hours. Skipping email.", flush=True)
        return True

    markdown = report["markdown"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    current = report["current"]
    pnl = current["closed_pnl"]
    pnl_sign = "+" if pnl >= 0 else ""
    subject = f"Hyperbot Daily: {pnl_sign}{pnl:.2f} USDC | {current['wins']}W/{current['losses']}L | {today}"

    if dry_run:
        print(markdown)
        print(f"\n--- Subject: {subject} ---")
        return True

    config = load_email_config()
    return send_report(markdown, subject, config)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hyperbot Daily Email Report")
    parser.add_argument("workspace", nargs="?", default=".", help="Path to workspace")
    parser.add_argument("--dry-run", action="store_true", help="Print report without sending")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    success = run(workspace, dry_run=args.dry_run)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

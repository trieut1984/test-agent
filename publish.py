"""Publish new legal data to the live Agent Base app.

The crawlers (backfill / daily update) write legal_documents.json on this machine, but
the app on Agent Base serves the copy baked into its last deploy. After a crawl that
added documents, this commits *only* that data file, pushes it, and triggers a redeploy
through the Agent Base API, then waits for the deployment's terminal status.

Safety:
  - Only legal_documents.json is ever staged/committed (path-limited commit), so code or
    secrets under edit are never swept into an unattended push (.env is gitignored too).
  - The access token is read from the Agent Base env file and never logged.
  - Every failure is logged and swallowed: a failed publish must not break a crawl, and
    the next crawl simply tries again.

Usage:  python publish.py [--dry-run] [--deploy-only]
"""
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
DATA_FILE = "legal_documents.json"
APP_UUID = os.environ.get("AGENTBASE_APP_UUID", "sbr4jqihz8u8didyypvun21s")
ENV_FILES = [Path.home() / ".claude" / "zlpagentbase.env", Path.home() / ".codex" / "zlpagentbase.env",
             Path.home() / ".cursor" / "zlpagentbase.env"]
DEPLOY_POLL_SECONDS = 15
DEPLOY_TIMEOUT_SECONDS = 600

logger = logging.getLogger("publish")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # keep scheduled pythonw runs windowless


def _git(*args: str) -> subprocess.CompletedProcess:
    exe = shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"
    return subprocess.run([exe, *args], cwd=HERE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=_NO_WINDOW, timeout=180)


def _read_env() -> dict:
    for f in ENV_FILES:
        if f.is_file():
            out = {}
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
            return out
    return {}


def commit_and_push(message: str, dry_run: bool = False) -> bool:
    """True if there was something to publish and it reached GitHub."""
    status = _git("status", "--porcelain", "--", DATA_FILE)
    if not status.stdout.strip():
        logger.info("publish: no change in %s — nothing to publish", DATA_FILE)
        return False
    if dry_run:
        logger.info("publish [dry-run]: would commit and push %s (%s)", DATA_FILE, message)
        return True
    for args in (["add", "--", DATA_FILE], ["commit", "-m", message, "--", DATA_FILE]):
        r = _git(*args)
        if r.returncode != 0:
            logger.error("publish: git %s failed: %s", args[0], (r.stderr or r.stdout).strip()[:300])
            return False
    r = _git("push", "origin", "main")
    if r.returncode != 0:
        logger.error("publish: git push failed (will retry on the next crawl): %s", (r.stderr or r.stdout).strip()[:300])
        return False
    logger.info("publish: pushed %s to GitHub", DATA_FILE)
    return True


def deploy(dry_run: bool = False) -> bool:
    env = _read_env()
    base, token = env.get("AGENTBASE_BASE_URL", "").rstrip("/"), env.get("AGENTBASE_ACCESS_TOKEN", "")
    if not base or not token:
        logger.error("publish: Agent Base env file not found or incomplete — skipping deploy")
        return False
    if dry_run:
        logger.info("publish [dry-run]: would trigger a deploy of %s", APP_UUID)
        return True
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    api = f"{base}/api/v1"
    r = requests.post(f"{api}/deploy", params={"uuid": APP_UUID, "force": "false"}, headers=headers, timeout=60)
    if r.status_code in (404, 405):  # older Coolify versions accept GET only
        r = requests.get(f"{api}/deploy", params={"uuid": APP_UUID, "force": "false"}, headers=headers, timeout=60)
    if r.status_code >= 300:
        logger.error("publish: deploy request failed (HTTP %s): %s", r.status_code, r.text[:200])
        return False
    try:
        deployment = (r.json().get("deployments") or [{}])[0].get("deployment_uuid")
    except ValueError:
        deployment = None
    if not deployment:
        logger.warning("publish: deploy triggered but no deployment id returned — not waiting")
        return True
    deadline = time.time() + DEPLOY_TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(DEPLOY_POLL_SECONDS)
        s = requests.get(f"{api}/deployments/{deployment}", headers=headers, timeout=60)
        status = (s.json().get("status") if s.ok else None) or ""
        if status in ("finished", "failed", "cancelled-by-user"):
            ok = status == "finished"
            (logger.info if ok else logger.error)("publish: deployment %s -> %s", deployment, status)
            return ok
    logger.warning("publish: deployment %s still running after %ss — leaving it", deployment, DEPLOY_TIMEOUT_SECONDS)
    return True


def publish_if_changed(message: str, dry_run: bool = False) -> bool:
    """Entry point for the crawlers. Never raises."""
    try:
        if commit_and_push(message, dry_run=dry_run):
            return deploy(dry_run=dry_run)
    except Exception as e:  # noqa: BLE001 — a failed publish must never break a crawl
        logger.error("publish: unexpected error: %s", e)
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    dry = "--dry-run" in sys.argv
    if "--deploy-only" in sys.argv:
        sys.exit(0 if deploy(dry_run=dry) else 1)
    sys.exit(0 if publish_if_changed("Data update: legal_documents.json (automated)", dry_run=dry) else 1)

Planning mode is default. 
Only execute after approval
When giving commands to run, give me in one by one copy and paste format
Update and add session summaries regularly.
Update memory as you go with important information.
Git pushes: use desktop-commander terminal (`git add`, `git commit`, `git push`), not GitHub Desktop.

---

## ⚠️ CRITICAL: gcloud Run Services Rule

**NEVER use `--set-env-vars` on a live Cloud Run service. ALWAYS use `--update-env-vars`.**

- `--set-env-vars` REPLACES the entire env var list — silently wipes everything not in the command
- `--update-env-vars` MERGES — only changes what you specify, leaves everything else intact

This mistake took down visitgdc.com on July 6, 2026 when a Claude chat added `INTERNAL_SYNC_KEY` using `--set-env-vars` and wiped 20+ other env vars including `CLOUD_SQL_CONNECTION_NAME`, causing the scheduler to fall back to ephemeral SQLite and lose all booking data on cold starts.

**Rule:** When giving any `gcloud run services update` command, always use `--update-env-vars`.

---

## GCP Monitoring (graftondentalcare.com account only)

A read-only service account is set up for API-based GCP monitoring. Use it to check logs, build status, and Cloud Run health without opening the console.

**Credential file:** `/Users/anurag/Documents/Projects/_CREDENTIALS_VAULT/gcp-cowork-monitor.json`
**Service account:** `cowork-monitor@lab-case-manager.iam.gserviceaccount.com`
**Org:** `graftondentalcare.com` (Org ID: `462811596275`)
**Roles:** logging.viewer, monitoring.viewer, run.viewer, cloudbuild.builds.viewer (granted at org level)

**Projects covered (graftondentalcare.com):**
- `lab-case-manager` — Lab Case Manager app (Cloud Run: us-east4)
- `dentastock-prod` — Dentastock app
- `marketing-landing-page-491721` — Marketing landing page
- `mythic-producer-287915` — My First Project

**GitHub-triggered builds** are in `us-east4` region — use `/v1/projects/{PROJECT}/locations/us-east4/builds`
**Manual/storage builds** are in `global` region — use `/v1/projects/{PROJECT}/locations/global/builds`

**NOT covered:** ChartNotesPro, PerioCharting, VoiceCharting — those are on a different Google account.

```python
from google.oauth2 import service_account
import google.auth.transport.requests, requests

KEY_FILE = "/sessions/determined-magical-mayer/mnt/Projects/_CREDENTIALS_VAULT/gcp-cowork-monitor.json"
creds = service_account.Credentials.from_service_account_file(KEY_FILE, scopes=["https://www.googleapis.com/auth/cloud-platform"])
creds.refresh(google.auth.transport.requests.Request())
token = creds.token  # use as: headers={"Authorization": f"Bearer {token}"}
```
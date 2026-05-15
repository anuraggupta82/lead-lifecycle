# Mac Mini Migration Checklist
**Last updated: May 15, 2026**
> This is a living document. Update it as new features are added during development.

---

## Pre-Migration: One-Time Setup on Mac Mini

### 1. Install Prerequisites
- [ ] Install Python 3.11 (`brew install python@3.11`)
- [ ] Install Homebrew if not present
- [ ] Confirm Mac Mini is on the office LAN (same network as `GraftonServer` / OpenDental)

### 2. Clone the Repository
```bash
git clone <repo-url> /Users/<macmini-user>/Documents/Projects/gdc-apps
```

### 3. Build the Python Virtual Environment
```bash
cd /Users/<macmini-user>/Documents/Projects/gdc-apps/marketing/lead-lifecycle/backend
/usr/local/bin/python3.11 -m venv venv
venv/bin/pip install -r requirements.txt
```

---

## Files to Transfer Manually (NOT in git)

### 4. The Database — `pipeline.db` ⚠️ Most Critical
This contains all leads, optimizer memory, rejection patterns, call records, audit history, GA4 cache, and all AI learning to date.

- [ ] Stop the service on dev Mac before copying (avoid partial writes)
- [ ] Copy from: `/Users/anurag/Documents/Projects/gdc-apps/marketing/lead-lifecycle/backend/pipeline.db`
- [ ] Copy to: same relative path on Mac Mini
- [ ] Verify row counts after copy

### 5. The `.env` File
Not in git. Contains all API keys and secrets.

- [ ] Copy `backend/.env` to Mac Mini
- [ ] Update the 3 hardcoded `/Users/anurag/` paths:
  - `DB_PATH=` → new absolute path to `pipeline.db`
  - `GA4_SERVICE_ACCOUNT_JSON=` → new path to GA4 key file
  - `VERTEX_CREDENTIALS_PATH=` → new path to Vertex AI key file

### 6. Credential JSON Files
These live in `_CREDENTIALS_VAULT/` on dev Mac — not in git.

- [ ] `marketing landing page service account key.json` → copy to Mac Mini, update `GA4_SERVICE_ACCOUNT_JSON` in `.env`
- [ ] `vertex-ai-mango-pipeline.json` → copy to Mac Mini, update `VERTEX_CREDENTIALS_PATH` in `.env`
- [ ] GCS service account key → run `bash setup-mac-mini.sh /path/to/key.json` (handles copy + .env update automatically)

---

## Auto-Start Configuration

### 7. Install the launchd plist
The plist auto-starts the service on login and keeps it alive if it crashes.

- [ ] Open `com.grafton.pipeline.plist` and update `/Users/anurag/` → `/Users/<macmini-user>/` (appears 2x)
- [ ] Copy to LaunchAgents:
```bash
cp com.grafton.pipeline.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.grafton.pipeline.plist
```
- [ ] Create the log directory:
```bash
sudo mkdir -p /usr/local/var/log
```
- [ ] Verify it started:
```bash
launchctl list | grep grafton
tail -f /usr/local/var/log/grafton-pipeline.log
```

---

## Network & Connectivity Checks

### 8. OpenDental (Office LAN)
- [ ] Ping `GraftonServer` from Mac Mini — must resolve on office LAN
- [ ] Test OD MySQL: `mysql -h GraftonServer -P 3306 -u root opendental`
- [ ] Test OD REST API: `curl http://GraftonServer:30223/api/v1`
- [ ] Confirm `OD_DB_HOST=GraftonServer` in `.env` resolves (or update to IP if needed)

### 9. External Services
- [ ] Verify Anthropic API key works (run a test optimizer prompt)
- [ ] Verify Google Ads API connection (`GOOGLE_ADS_REFRESH_TOKEN` is long-lived but can expire)
- [ ] Verify Twilio SMS sends correctly
- [ ] Verify Mango Voice API connects (`MANGO_USERNAME` / `MANGO_PASSWORD`)
- [ ] Verify Firestore connection (nXtsmile lead sync)
- [ ] Verify GA4 service account has access to all 3 properties

### 10. Ports & Firewall
- [ ] Port 7070 is open/accessible on Mac Mini from office network
- [ ] If accessing dashboard remotely: set up VPN or SSH tunnel (do NOT expose port 7070 to the public internet — no HTTPS/auth beyond admin password)

---

## Cutover Steps (Day of Migration)

1. [ ] Do a final `git push` from dev Mac so all code is current
2. [ ] `git pull` on Mac Mini
3. [ ] Stop service on dev Mac: `launchctl stop com.grafton.pipeline`
4. [ ] Copy `pipeline.db` to Mac Mini (this is the cutover moment)
5. [ ] Start service on Mac Mini: `launchctl start com.grafton.pipeline`
6. [ ] Confirm dashboard loads at `http://<mac-mini-ip>:7070`
7. [ ] Run a test AI Optimizer pass and verify it reads/writes DB correctly
8. [ ] Update any bookmarks / local DNS from `localhost:7070` to `<mac-mini-ip>:7070`

---

## Things to Add As Development Continues
> Update this section as new features are built that introduce new state, credentials, or services.

- [ ] *(placeholder — add new credential files here)*
- [ ] *(placeholder — add new .env variables here)*
- [ ] *(placeholder — add new DB tables or external service dependencies here)*

---

## Rollback Plan
If something goes wrong after cutover:
1. Stop service on Mac Mini
2. Copy `pipeline.db` back to dev Mac (restore from the copy you made in step 4 above)
3. Start service on dev Mac: `launchctl start com.grafton.pipeline`
4. Investigate before retrying

# Session Summary — May 21, 2026
## AI Optimizer Active-Campaign Filter Regression Fix

### Problem
The AI Optimizer was displaying recommendations for **all campaigns** (including paused ones) instead of only active/ENABLED campaigns. This was a regression — the active-only filter had been intentionally built earlier.

### Root Cause
Traced through git log for the last 48–72 hours. Identified the bug in commit `0429fc2` (May 19, "mcp tool and ai optimizer fix"), which introduced the active-campaign filter correctly in logic but contained a subtle proto enum bug.

**Bad line** (`backend/ai_optimizer.py`, line 1208, `_get_campaign_settings`):
```python
"campaign_status": str(row.campaign.status).replace("CampaignStatus.", ""),
```
The Google Ads proto enum's `str()` returns the **integer value** (e.g. `"2"`), not the name. So `.replace("CampaignStatus.", "")` left the integer `"2"` — which never matched `== "ENABLED"`.

This caused `_enabled_camp_names` (the set of active campaign names) to always be **empty**. The filter downstream:
```python
keyword_perf_active = [k for k in keyword_perf if k.get("campaign", "").strip() in _enabled_camp_names]
```
passed zero rows, and the per-campaign Claude loop fell back to:
```python
all_campaign_names = sorted(active_campaigns_with_data) or list(campaign_spend.keys()) or ...
```
`campaign_spend` is built from unfiltered `keyword_perf` (all campaigns), so ALL campaigns were shown.

### Fix
One line change in `_get_campaign_settings`:
```python
# Before (broken):
"campaign_status": str(row.campaign.status).replace("CampaignStatus.", ""),

# After (fixed):
"campaign_status": str(row.campaign.status.name),  # "ENABLED" or "PAUSED" — use .name, not str() which returns the integer
```
This matches the pattern already used correctly in `google_ads_create.py` line 1735, which even has a comment warning about this exact pitfall.

### Files Changed
- `backend/ai_optimizer.py` — line 1208 only

### Git Push Info
- **Title:** `fix: campaign_status using .name not str() — active-only filter was silently broken`
- **Description:** `_get_campaign_settings stored campaign_status as the proto integer ('2') instead of the name ('ENABLED'). This caused _enabled_camp_names to be always empty, so the ENABLED-only filter fell back to all campaigns including paused ones. Single-line fix: str(row.campaign.status.name). Matches the pattern already used correctly in google_ads_create.py.`

### Status
✅ Fix applied. Ready to push via GitHub Desktop.

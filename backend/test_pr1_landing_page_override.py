"""
PR 1 Test — campaigns.landing_page override in replace_ad
Run from the backend directory:
    python3 test_pr1_landing_page_override.py

Tests the override logic in both places:
  1. Optimizer-side (ai_optimizer.py ~line 3724)
  2. Approval-side (main.py ~line 2005)

No Google Ads calls are made. The DB is temporarily modified and restored.
"""

import sys, json, traceback
sys.path.insert(0, ".")

TEST_LP = "https://visitgdc.com/emergency-dentist-grafton-ma/"
OLD_URL = "https://graftondentalcare.com/emergency/"

PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"

results = []

def check(label, got, expected):
    ok = got == expected
    results.append(ok)
    tag = PASS if ok else FAIL
    print(f"  {tag} {label}")
    if not ok:
        print(f"        got:      {repr(got)}")
        print(f"        expected: {repr(expected)}")

# ── Import DB helpers ──────────────────────────────────────────────────────────
try:
    from database import get_campaign_by_name, _conn
    # Auto-detect: pick first active campaign from DB
    with _conn() as _c:
        _rows = _c.execute(
            "SELECT campaign_name, landing_page FROM campaigns WHERE status='active' ORDER BY campaign_name LIMIT 5"
        ).fetchall()
    print("Active campaigns in DB:")
    for r in _rows:
        print(f"  - {r[0]!r}  (landing_page={repr(r[1])})")
    if not _rows:
        # Fallback: any campaign
        with _conn() as _c:
            _rows = _c.execute("SELECT campaign_name, landing_page FROM campaigns ORDER BY campaign_name LIMIT 5").fetchall()
        print("No active campaigns — using all:")
        for r in _rows:
            print(f"  - {r[0]!r}")
    if not _rows:
        print("ERROR: No campaigns found in DB at all")
        sys.exit(1)
    CAMPAIGN_NAME = _rows[0][0]
    print(f"\nUsing campaign: {repr(CAMPAIGN_NAME)}")
    camp = get_campaign_by_name(CAMPAIGN_NAME)
    original_lp = camp.get("landing_page") or ""
    print(f"Original landing_page: {repr(original_lp)}")
    print()
except Exception as e:
    print(f"ERROR importing database: {e}")
    traceback.print_exc()
    sys.exit(1)

# ── Helper: mirrored override logic from ai_optimizer.py ─────────────────────
def run_optimizer_override(campaign, item_final_url):
    item = {"final_url": item_final_url}
    try:
        from database import get_campaign_by_name as _gcbn_lp
        _camp_row_lp = _gcbn_lp(campaign) if campaign else None
        _lp = ((_camp_row_lp or {}).get("landing_page") or "").strip()
        if _lp and _lp.lower().startswith(("http://", "https://")):
            if item.get("final_url") != _lp:
                item["final_url"] = _lp
    except Exception as _e:
        print(f"  override error: {_e}")
    return item["final_url"]

# ── Helper: mirrored override logic from main.py ─────────────────────────────
def run_approval_override(campaign_name, final_url_from_after):
    final_url = final_url_from_after
    try:
        from database import get_campaign_by_name as _gcbn_lp_approval
        _camp_name_lp = (campaign_name or "").strip()
        if _camp_name_lp:
            _camp_row_lp = _gcbn_lp_approval(_camp_name_lp)
            _lp_override = ((_camp_row_lp or {}).get("landing_page") or "").strip()
            if _lp_override and _lp_override.lower().startswith(("http://", "https://")):
                if _lp_override != final_url:
                    final_url = _lp_override
    except Exception as _lp_err:
        print(f"  approval override error: {_lp_err}")
    return final_url


print("=" * 60)
print("SECTION 1: Optimizer-side override (ai_optimizer.py)")
print("=" * 60)

# Test 1a: landing_page set, Claude has old URL → override fires
with _conn() as c:
    c.execute("UPDATE campaigns SET landing_page=? WHERE campaign_name=?", (TEST_LP, CAMPAIGN_NAME))
result = run_optimizer_override(CAMPAIGN_NAME, OLD_URL)
check("landing_page set + old URL → override fires", result, TEST_LP)

# Test 1b: landing_page set, Claude already has correct URL → no override
result = run_optimizer_override(CAMPAIGN_NAME, TEST_LP)
check("landing_page set + correct URL → no change", result, TEST_LP)

# Test 1c: landing_page empty → preserve Claude suggestion
with _conn() as c:
    c.execute("UPDATE campaigns SET landing_page=? WHERE campaign_name=?", ("", CAMPAIGN_NAME))
result = run_optimizer_override(CAMPAIGN_NAME, OLD_URL)
check("landing_page empty → preserve Claude suggestion", result, OLD_URL)

# Test 1d: landing_page invalid scheme → preserve Claude suggestion
with _conn() as c:
    c.execute("UPDATE campaigns SET landing_page=? WHERE campaign_name=?", ("ftp://bad.com", CAMPAIGN_NAME))
result = run_optimizer_override(CAMPAIGN_NAME, OLD_URL)
check("landing_page invalid scheme → preserve Claude suggestion", result, OLD_URL)

# Test 1e: landing_page NULL → preserve Claude suggestion
with _conn() as c:
    c.execute("UPDATE campaigns SET landing_page=NULL WHERE campaign_name=?", (CAMPAIGN_NAME,))
result = run_optimizer_override(CAMPAIGN_NAME, OLD_URL)
check("landing_page NULL → preserve Claude suggestion", result, OLD_URL)

# Test 1f: empty campaign name → no crash, preserve suggestion
with _conn() as c:
    c.execute("UPDATE campaigns SET landing_page=? WHERE campaign_name=?", (TEST_LP, CAMPAIGN_NAME))
result = run_optimizer_override("", OLD_URL)
check("empty campaign name → no crash", result, OLD_URL)


print()
print("=" * 60)
print("SECTION 2: Approval-side override (main.py)")
print("=" * 60)

# Test 2a: landing_page set, after_state has old URL → override fires
with _conn() as c:
    c.execute("UPDATE campaigns SET landing_page=? WHERE campaign_name=?", (TEST_LP, CAMPAIGN_NAME))
result = run_approval_override(CAMPAIGN_NAME, OLD_URL)
check("landing_page set + old after_state URL → override fires", result, TEST_LP)

# Test 2b: already matching → no change
result = run_approval_override(CAMPAIGN_NAME, TEST_LP)
check("landing_page set + matching after_state URL → no change", result, TEST_LP)

# Test 2c: empty landing_page → preserve after_state URL
with _conn() as c:
    c.execute("UPDATE campaigns SET landing_page=? WHERE campaign_name=?", ("", CAMPAIGN_NAME))
result = run_approval_override(CAMPAIGN_NAME, OLD_URL)
check("landing_page empty → preserve after_state URL", result, OLD_URL)

# Test 2d: unknown campaign name → no crash, preserve after_state URL
result = run_approval_override("NonExistentCampaign", OLD_URL)
check("unknown campaign name → no crash", result, OLD_URL)

# Test 2e: empty campaign_name in row → no crash
result = run_approval_override("", OLD_URL)
check("empty campaign_name in row → no crash", result, OLD_URL)


# ── Restore original landing_page ─────────────────────────────────────────────
with _conn() as c:
    c.execute("UPDATE campaigns SET landing_page=? WHERE campaign_name=?", (original_lp, CAMPAIGN_NAME))
print()
print(f"Restored landing_page to: {repr(original_lp)}")

# ── Summary ───────────────────────────────────────────────────────────────────
print()
print("=" * 60)
passed = sum(results)
total = len(results)
if passed == total:
    print(f"\033[92m✅  All {total}/{total} tests passed.\033[0m")
else:
    print(f"\033[91m❌  {passed}/{total} tests passed.\033[0m")
print("=" * 60)
sys.exit(0 if passed == total else 1)

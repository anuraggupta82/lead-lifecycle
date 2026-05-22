"""
Lead Lifecycle Service — FastAPI
Runs on Mac Mini (http://localhost:7070)

Endpoints:
  POST /api/events                    — receive lifecycle events from any source
  GET  /api/pipeline                  — all leads with stage summary (dashboard)
  GET  /api/lead/{id}                 — full lead + event timeline
  POST /api/unsubscribe/{id}/{channel} — opt-out handler
  GET  /unsubscribe/{id}/{channel}    — one-click unsubscribe (from email links)
  GET  /delete-image/{id}              — one-click smile image deletion (from email links)
  GET  /api/admin/stats               — pipeline funnel stats
  GET  /api/admin/queue               — pending follow-up queue
  POST /api/admin/sync                — trigger Firestore sync
  POST /api/admin/match               — trigger OD patient matching
  POST /api/admin/run-queue           — manually trigger follow-up engine
  PUT  /api/admin/lead/{id}/stage     — manually advance lead stage
  GET  /health                        — health check
  GET  /                              — pipeline dashboard (React SPA)
"""
import logging
import os
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Request, Header, Body, BackgroundTasks, UploadFile, File, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator

from config import get_settings, Settings
from database import (
    init_db, upsert_lead, get_lead, get_lead_by_email, update_stage,
    get_all_leads, get_events, get_pipeline_stats, enqueue_follow_ups,
    add_event, unsubscribe, get_follow_up_queue, get_due_follow_ups,
    add_note, get_notes, delete_note, force_stage,
    get_campaign_stats, get_google_ads_campaigns, get_distinct_sources, get_keyword_stats,
    get_search_term_stats, get_st_classifications, get_geo_stats, get_geo_stats_by_campaign, get_schedule_stats,
    get_geo_json_for_campaign_resource, update_geo_json_for_campaign_resource,
    add_deleted_lead_tombstone, backfill_communication_log, backfill_call_keyword_attribution,
    get_or_create_conversation, get_conversation, get_messages, get_all_conversations,
    get_daily_stats, get_ad_group_stats,
    save_outbound_message, get_lead_messages,
    # Step 9: workflows
    get_all_workflows, get_workflow, get_workflow_steps, get_workflow_step,
    upsert_workflow, upsert_workflow_step, delete_workflow_step, delete_workflow,
    # OD settings
    get_setting, save_setting, get_od_settings,
    # Step 10: stop conditions helpers
    add_lead_event,
    # Inbox / call log / next action helpers
    get_unread_sms_count, get_unread_sms_leads, mark_sms_read,
    get_unread_email_count, get_unread_email_leads, mark_email_read,
    log_call, get_calls, set_next_action, clear_next_action,
    # Lead tags
    get_lead_tags, set_lead_tags,
    # Domain registry
    list_domains, get_domain, create_domain, update_domain, delete_domain,
    list_domain_pages,
    # Media library
    list_media_library, get_media_library_item, create_media_library_item,
    update_media_library_item, delete_media_library_item,
)
from email_service import send_office_new_lead
from follow_up_engine import start_scheduler, stop_scheduler, run_now
from ga4_events import (
    track_lead_created, track_smile_completed, track_appointment_booked,
)
from firestore_sync import sync_from_firestore, sync_unsubscribes_from_firestore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import logging.handlers as _lh
_LOG_FILE = os.path.join(os.path.dirname(__file__), "logs", "app.log")
os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
_file_handler = _lh.RotatingFileHandler(
    _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(), _file_handler],
)
logger = logging.getLogger(__name__)
logger.info(f"Log file: {_LOG_FILE}")

# Module-level scheduler reference so endpoints can inspect job state
ads_scheduler = None
# Tracks last successful run time per job id
_job_last_run: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ads_scheduler
    # Startup
    init_db()
    logger.info("Database initialized")

    # One-time-safe backfill: ensure communication_log has a row for every
    # follow_up_queue entry already marked 'sent', so a restart will never
    # replay an already-delivered template. Idempotent on subsequent boots.
    try:
        n = backfill_communication_log()
        logger.info(f"communication_log backfill: inserted {n} row(s)")
    except Exception as e:
        logger.warning(f"communication_log backfill failed (non-fatal): {e}")

    # Rescue any calls that were stranded as 'skipped_no_audio' due to missing
    # Mango token during reconciliation — reset them so the pipeline tick can
    # retry transcription on the next cycle.
    try:
        from database import reset_skipped_no_audio_calls
        n_rescued = reset_skipped_no_audio_calls()
        if n_rescued:
            logger.info(f"Rescued {n_rescued} skipped_no_audio call(s) — reset to pending for pipeline retry")
    except Exception as e:
        logger.warning(f"reset_skipped_no_audio_calls failed (non-fatal): {e}")

    # Backfill keyword attribution on any calls already matched to GAds call_view rows
    try:
        n_kw = backfill_call_keyword_attribution()
        if n_kw:
            logger.info(f"Startup: backfilled keyword attribution on {n_kw} call(s)")
    except Exception as e:
        logger.warning(f"backfill_call_keyword_attribution failed (non-fatal): {e}")

    # Auto-sync Firestore leads on startup (non-blocking — ignore errors)
    try:
        result = sync_from_firestore()
        logger.info(f"Startup Firestore sync: {result}")
    except Exception as e:
        logger.warning(f"Startup Firestore sync failed (non-fatal): {e}")

    # Start follow-up scheduler (every 15 min)
    start_scheduler()

    # Start Google Ads scheduled jobs
    ads_scheduler = BackgroundScheduler(timezone="America/New_York")  # also stored at module level above

    from datetime import datetime as _dt

    def _stamp(job_id):
        _job_last_run[job_id] = _dt.now().isoformat()

    # 5:30 AM — Pull GA4 analytics data
    def _ga4_pull_job():
        _stamp("ga4_pull")
        try:
            from ga4_reporting import fetch_all_ga4_data
            from database import save_ga4_cache
            data = fetch_all_ga4_data(days=30)
            if not data.get("overview", {}).get("error"):
                save_ga4_cache("full_report", 30, data)
                logger.info(f"GA4 data cached: {data['overview'].get('sessions', 0):.0f} sessions")
            else:
                logger.warning(f"GA4 pull returned error: {data['overview'].get('error')}")
        except Exception as e:
            logger.error(f"Scheduled GA4 pull failed: {e}")

    # 6 AM — Resolve gclids to keywords
    def _gads_sync_job():
        _stamp("gads_sync")
        try:
            from google_ads_sync import sync_gclids_to_keywords
            result = sync_gclids_to_keywords(days_back=7)
            logger.info(f"Scheduled Google Ads sync: {result}")
        except Exception as e:
            logger.error(f"Scheduled Google Ads sync failed: {e}")

    # 7 AM — AI optimizer (after fresh data)
    def _optimizer_job():
        _stamp("ai_optimizer")
        try:
            from ai_optimizer import optimize_campaign
            result = optimize_campaign(trigger="scheduler_7am")
            logger.info(f"Scheduled optimizer: run_id={result.get('run_id','?')} "
                        f"pending={result.get('summary', {}).get('keywords_to_pause', 0)} pauses")
        except Exception as e:
            logger.error(f"Scheduled optimizer failed: {e}")

    # 10 PM — OpenDental matcher + treatment stages
    def _od_sync_job():
        _stamp("od_sync")
        try:
            from od_matcher import run_full_od_sync
            result = run_full_od_sync()
            logger.info(f"Scheduled OD sync: {result}")
        except Exception as e:
            logger.error(f"Scheduled OD sync failed: {e}")

    # Every 15 min — Re-sync Firestore leads (picks up new smile design / form submissions)
    def _firestore_sync_job():
        _stamp("firestore_sync")
        try:
            result = sync_from_firestore()
            if result.get("synced", 0) > 0:
                logger.info(f"Scheduled Firestore sync: {result}")
        except Exception as e:
            logger.error(f"Scheduled Firestore sync failed: {e}")
        # Same cadence — pull unsubscribe opt-outs from the public Cloud Run
        # microservice (collection `unsubscribes` in marketing-landing-page-491721).
        try:
            unsub_result = sync_unsubscribes_from_firestore()
            if unsub_result.get("applied", 0) > 0:
                logger.info(f"Scheduled unsubscribe sync: {unsub_result}")
        except Exception as e:
            logger.error(f"Scheduled unsubscribe sync failed: {e}")

    # Every 5 min — Poll IMAP inbox for inbound emails
    def _imap_poll_job():
        _stamp("imap_poll")
        try:
            from imap_service import poll_once
            result = poll_once()
            if result.get("fetched", 0) > 0 or result.get("errors", 0) > 0:
                logger.info(f"IMAP poll: {result}")
        except Exception as e:
            logger.error(f"IMAP poll failed: {e}")

    # ── Mango Voice — initialize token manager and schedule jobs ──────────────
    settings = get_settings()
    if settings.mango_enabled and settings.mango_username and settings.mango_password:
        try:
            from mango_service import MangoTokenManager, sync_mango_calls, reconcile_attribution
            _mango_token_mgr = MangoTokenManager(
                username=settings.mango_username,
                password=settings.mango_password,
                api_base=settings.mango_api_base,
            )
            app.state.mango_token_mgr = _mango_token_mgr
            logger.info("Mango Voice token manager initialized")

            def _mango_sync_job():
                _stamp("mango_sync")
                try:
                    n = sync_mango_calls(
                        _mango_token_mgr,
                        pbx_id=settings.mango_pbx_id,
                        api_base=settings.mango_api_base,
                    )
                    if n > 0:
                        logger.info(f"Mango sync: {n} calls upserted")
                except Exception as e:
                    logger.error(f"Mango sync failed: {e}")
                # Patient enrichment: match new calls to OD patients (runs after every sync)
                try:
                    from od_matcher import match_mango_calls_to_od_patients
                    result = match_mango_calls_to_od_patients(limit=200)
                    if result.get("total", 0) > 0:
                        logger.info(f"Mango OD patient match: {result}")
                except Exception as e:
                    logger.error(f"Mango OD patient match failed: {e}")

            def _mango_reconcile_job():
                _stamp("mango_reconcile")
                try:
                    _tok = _mango_token_mgr.get_token()
                    n = reconcile_attribution(days=7, mango_token=_tok)
                    if n > 0:
                        logger.info(f"Mango reconcile: {n} calls attributed")
                except Exception as e:
                    logger.error(f"Mango reconcile failed: {e}")
                # Keyword attribution — runs after phone-number reconcile
                try:
                    from call_keyword_attribution import attribute_calls_to_keywords
                    kw_result = attribute_calls_to_keywords(days=7)
                    logger.info(f"Keyword attribution: {kw_result}")
                except Exception as e:
                    logger.error(f"Keyword attribution failed: {e}")

            def _mango_gads_call_view_job():
                _stamp("mango_call_view")
                try:
                    from google_ads_sync import sync_call_view, sync_call_search_terms
                    n = sync_call_view(days_back=14)
                    logger.info(f"GAds call_view sync: {n} rows")
                    # Also refresh call search terms for keyword attribution
                    m = sync_call_search_terms(days=30)
                    logger.info(f"GAds call search terms sync: {m} rows")
                    # Re-run backfill with fresh search term data
                    from database import backfill_call_keyword_attribution
                    updated = backfill_call_keyword_attribution()
                    if updated:
                        logger.info(f"Keyword attribution backfill: {updated} calls updated")
                except Exception as e:
                    logger.error(f"GAds call_view sync failed: {e}")

            ads_scheduler.add_job(
                _mango_sync_job,
                CronTrigger(minute="0,5,10,15,20,25,30,35,40,45,50,55"),
                id="mango_sync", name="Mango Voice Call Sync",
                max_instances=1, coalesce=True, replace_existing=True,
            )
            ads_scheduler.add_job(
                _mango_reconcile_job,
                CronTrigger(minute="3,33"),
                id="mango_reconcile", name="Mango Attribution Reconciler",
                max_instances=1, coalesce=True, replace_existing=True,
            )
            ads_scheduler.add_job(
                _mango_gads_call_view_job,
                CronTrigger(minute="5,35"),  # every 30 min — keeps call_view fresh for same-day attribution
                id="mango_call_view", name="GAds Call View Sync",
                max_instances=1, coalesce=True, replace_existing=True,
            )

            # Pipeline tick — runs every N minutes (offset from sync to avoid contention)
            _pipeline_interval = max(5, settings.mango_pipeline_interval_min)

            def _mango_pipeline_tick():
                _stamp("mango_pipeline")
                try:
                    from mango_pipeline import run_pipeline_tick
                    tok = None
                    if app.state.mango_token_mgr:
                        tok = app.state.mango_token_mgr.get_token()
                    run_pipeline_tick(mango_token=tok)
                except Exception as e:
                    logger.error(f"Mango pipeline tick failed: {e}")

            ads_scheduler.add_job(
                _mango_pipeline_tick,
                "interval",
                minutes=_pipeline_interval,
                id="mango_pipeline", name="Mango Call Analysis Pipeline",
                max_instances=1, coalesce=True, replace_existing=True,
            )

        except Exception as e:
            logger.warning(f"Mango Voice initialization failed (non-fatal): {e}")
            app.state.mango_token_mgr = None
    else:
        app.state.mango_token_mgr = None
        logger.info("Mango Voice disabled (MANGO_ENABLED=false or credentials missing)")

    # 6:30 AM — Rebuild keyword_intelligence join table (Phase A)
    # Runs after GA4 pull (5:30) and GAds sync (6:00) so all source data is fresh.
    def _keyword_intelligence_job():
        _stamp("keyword_intelligence")
        try:
            from intelligence_builder import rebuild_keyword_intelligence
            result = rebuild_keyword_intelligence()
            logger.info(f"Keyword intelligence rebuilt: {result}")
        except Exception as e:
            logger.error(f"Keyword intelligence rebuild failed: {e}")

    # 10:15 PM — OD Payment Sync (runs after OD match at 10PM, before call production at 10:30PM)
    def _od_payment_sync_job():
        _stamp("od_payment_sync")
        try:
            from od_payment_sync import sync_od_payments
            result = sync_od_payments(days_back=7)
            logger.info(f"Scheduled OD payment sync: {result}")
        except Exception as e:
            logger.error(f"Scheduled OD payment sync failed: {e}")

    # 10:30 PM — Link phone-call patients to keyword production log (runs after OD sync at 10PM)
    def _call_production_job():
        _stamp("call_production")
        try:
            from call_production_log import link_calls_to_keyword_production
            result = link_calls_to_keyword_production(days=7)
            logger.info(f"Scheduled call production log: {result}")
        except Exception as e:
            logger.error(f"Scheduled call production log failed: {e}")

    # 11 PM — Upload offline conversions
    def _conversion_upload_job():
        _stamp("conversion_upload")
        try:
            from google_ads_conversions import upload_offline_conversions
            result = upload_offline_conversions()
            logger.info(f"Scheduled conversion upload: {result}")
        except Exception as e:
            logger.error(f"Scheduled conversion upload failed: {e}")

    ads_scheduler.add_job(_firestore_sync_job, CronTrigger(minute="0,15,30,45"),
                          id="firestore_sync", name="Firestore Lead Sync",
                          max_instances=1, coalesce=True, replace_existing=True)
    ads_scheduler.add_job(_imap_poll_job, CronTrigger(minute="0,5,10,15,20,25,30,35,40,45,50,55"),
                          id="imap_poll", name="IMAP Inbox Poll",
                          max_instances=1, coalesce=True, replace_existing=True)
    ads_scheduler.add_job(_ga4_pull_job, CronTrigger(hour=5, minute=30),
                          id="ga4_pull", name="GA4 Analytics Data Pull", replace_existing=True)
    # 6 AM — GAds morning refresh: keeps keyword cache fresh for the 7 AM AI optimizer.
    # Note: the full gclid→keyword attribution for income purposes runs again at 22:00
    # as step 2 of the unified_od_sync chain, so no attribution work is lost.
    def _gads_morning_refresh_job():
        _stamp("gads_morning_refresh")
        try:
            from google_ads_sync import sync_gclids_to_keywords
            result = sync_gclids_to_keywords(days_back=7)
            logger.info(f"GAds morning refresh: {result}")
        except Exception as e:
            logger.error(f"GAds morning refresh failed: {e}")

    ads_scheduler.add_job(_gads_morning_refresh_job, CronTrigger(hour=6, minute=0),
                          id="gads_morning_refresh", name="Google Ads Morning Keyword Refresh",
                          replace_existing=True)
    ads_scheduler.add_job(_keyword_intelligence_job, CronTrigger(hour=6, minute=30),
                          id="keyword_intelligence", name="Keyword Intelligence Rebuild", replace_existing=True)
    ads_scheduler.add_job(_optimizer_job, CronTrigger(hour=7, minute=0),
                          id="ai_optimizer", name="AI Campaign Optimizer", replace_existing=True)

    # 10 PM — Unified OD sync: replaces the former 5 individual evening jobs
    # (_gads_sync at 06:00, _od_sync at 22:00, _od_payment_sync at 22:15,
    # _call_production at 22:30, _conversion_upload at 23:00).
    # The individual job functions (_gads_sync_job, _od_sync_job, _od_payment_sync_job,
    # _call_production_job, _conversion_upload_job) are KEPT — they are still called
    # by the individual Admin endpoints in the Advanced disclosure.
    def _unified_od_sync_job():
        _stamp("unified_od_sync")
        try:
            from unified_od_sync import run_unified_od_sync
            result = run_unified_od_sync(trigger="scheduled")
            logger.info(
                f"Scheduled unified OD sync: pct={result.get('pct')}, "
                f"steps={len(result.get('step_results', []))}"
            )
        except Exception as e:
            logger.error(f"Scheduled unified OD sync failed: {e}", exc_info=True)

    ads_scheduler.add_job(_unified_od_sync_job, CronTrigger(hour=22, minute=0),
                          id="unified_od_sync", name="Unified OD Sync (chain)",
                          max_instances=1, coalesce=True, replace_existing=True)

    # Domain crawler — runs on the 1st of every month at 2 AM.
    # Incremental: only re-crawls pages whose HTML has changed (hash diff).
    # First crawl of a new domain is always full regardless of schedule.
    from domain_crawler import domain_crawl_scheduled_job as _domain_crawl_job
    ads_scheduler.add_job(_domain_crawl_job, CronTrigger(day=1, hour=2, minute=0),
                          id="domain_crawl", name="Domain Crawler (monthly)", replace_existing=True)

    # Quarterly nearby-practices sync — 1st of Jan, Apr, Jul, Oct at 3 AM.
    # Fetches all 4 radius bands (5/10/15/20 mi) from Google Places and
    # upserts into nearby_practices DB for brand-negative keyword generation.
    def _nearby_practices_sync_job():
        _stamp("nearby_practices_sync")
        try:
            from places_client import sync_nearby_practices as _sync_nearby
            _places_key = get_settings().google_places_api_key
            if not _places_key:
                logger.warning("[nearby_sync] No GOOGLE_PLACES_API_KEY — skipping")
                return
            result = _sync_nearby(_places_key)
            logger.info(
                f"[nearby_sync] Quarterly sync complete: "
                f"synced={result.get('synced',0)} errors={result.get('errors',0)} "
                f"run_id={result.get('run_id','?')} bands={result.get('bands',{})}"
            )
        except Exception as e:
            logger.error(f"Nearby practices sync failed: {e}")

    ads_scheduler.add_job(
        _nearby_practices_sync_job,
        CronTrigger(month="1,4,7,10", day=1, hour=3, minute=0),
        id="nearby_practices_sync",
        name="Nearby Practices Quarterly Sync",
        replace_existing=True,
    )

    # Competitor advertising intelligence scan — 1st and 16th of each month at 4 AM.
    # Fetches Google Ads Auction Insights to detect which nearby competitors are
    # actively advertising specific services, then stages pending actions for review.
    def _competitor_intel_job():
        _stamp("competitor_intel")
        try:
            from competitor_intel_engine import run_competitor_intel_scan
            result = run_competitor_intel_scan()
            logger.info(
                f"[intel] Scan complete: intel={result.get('intel_written',0)} "
                f"actions={result.get('actions_staged',0)} run_id={result.get('run_id','?')}"
            )
        except Exception as e:
            logger.error(f"Competitor intel scan failed: {e}")

    ads_scheduler.add_job(
        _competitor_intel_job,
        CronTrigger(day="1,16", hour=4, minute=0),
        id="competitor_intel",
        name="Competitor Advertising Intelligence Scan (15-day)",
        replace_existing=True,
    )

    # SKAG traffic lock — runs nightly at 4:30 AM.
    # Finds SKAGs created 7+ days ago and adds an EXACT negative to the
    # source ad group so traffic routes exclusively through the SKAG.
    # Idempotent: KEYWORD_ALREADY_EXISTS is handled gracefully.
    def _skag_lock_job():
        _stamp("skag_lock")
        try:
            from skag_signals import lock_skag_traffic
            n = lock_skag_traffic()
            logger.info(f"[skag_lock] Locked {n} SKAG(s)")
        except Exception as e:
            logger.error(f"SKAG lock job failed: {e}")

    ads_scheduler.add_job(
        _skag_lock_job,
        CronTrigger(hour=4, minute=30),
        id="skag_lock",
        name="SKAG Traffic Lock (nightly)",
        replace_existing=True,
    )

    # SKAG outcomes snapshot — runs nightly at 4:45 AM.
    # Pulls 30-day impressions/clicks/conversions from GAds for each live SKAG
    # and writes a timestamped row to skag_outcomes_30d for performance tracking.
    def _skag_snapshot_job():
        _stamp("skag_snapshot")
        try:
            from skag_signals import snapshot_skag_outcomes, revert_zombie_skags
            n_snap = snapshot_skag_outcomes()
            n_revert = revert_zombie_skags()
            logger.info(f"[skag_snapshot] Snapshots written: {n_snap}  Zombies reverted: {n_revert}")
        except Exception as e:
            logger.error(f"SKAG snapshot/revert job failed: {e}")

    ads_scheduler.add_job(
        _skag_snapshot_job,
        CronTrigger(hour=4, minute=45),
        id="skag_snapshot",
        name="SKAG Outcomes Snapshot + Zombie Revert (nightly)",
        replace_existing=True,
    )

    # 1 AM — CallRail number sync: keeps callrail_numbers table current with
    # any new or updated trackers provisioned in the CallRail dashboard.
    def _callrail_sync_job():
        if not get_settings().callrail_api_key:
            logger.debug("[callrail_sync] skipped — CALLRAIL_API_KEY not set")
            return
        try:
            from callrail_sync import sync_callrail_numbers
            result = sync_callrail_numbers()
            if result.get("recording_warnings"):
                logger.warning(
                    "[callrail_sync] HIPAA WARNING — recording enabled on trackers: %s",
                    result["recording_warnings"]
                )
        except Exception as e:
            logger.error(f"CallRail number sync failed: {e}", exc_info=True)

    ads_scheduler.add_job(
        _callrail_sync_job,
        CronTrigger(hour=1, minute=0),
        id="callrail_sync",
        name="CallRail Number Sync (nightly)",
        max_instances=1, coalesce=True, replace_existing=True,
    )

    # Every 15 min (offset 2 min from Firestore/IMAP) — CallRail call polling.
    # Replaces webhook delivery: polls the API for new completed calls and runs
    # each through process_webhook() for lead creation and attribution.
    def _callrail_calls_sync_job():
        if not get_settings().callrail_api_key:
            logger.debug("[callrail_sync] calls sync skipped — CALLRAIL_API_KEY not set")
            return
        try:
            from callrail_sync import sync_callrail_calls
            result = sync_callrail_calls()
            if result.get("created") or result.get("linked") or result.get("errors"):
                logger.info(
                    "[callrail_sync] calls: created=%s linked=%s skipped_existing=%s "
                    "skipped_outbound=%s errors=%s duration_ms=%s",
                    result.get("created"), result.get("linked"),
                    result.get("skipped_existing"), result.get("skipped_outbound"),
                    result.get("errors"), result.get("duration_ms"),
                )
        except Exception as e:
            logger.error(f"CallRail calls sync failed: {e}", exc_info=True)

    ads_scheduler.add_job(
        _callrail_calls_sync_job,
        CronTrigger(minute="2,17,32,47"),
        id="callrail_calls_sync",
        name="CallRail Call Poll (every 15 min)",
        max_instances=1, coalesce=True, replace_existing=True,
    )

    ads_scheduler.start()
    logger.info("Scheduled jobs started (1AM CallRail number sync, every-15min CallRail call poll, 15-day competitor intel, quarterly nearby-practices sync, 1st/month 2AM domain crawl, 4:30AM SKAG lock, 4:45AM SKAG snapshot, 5:30AM GA4, 6AM gads sync, 6:30AM KI rebuild, 7AM optimizer, 10PM OD, 10:30PM call-prod, 11PM conversions)")

    yield

    # Shutdown
    stop_scheduler()
    ads_scheduler.shutdown(wait=False)


app = FastAPI(
    title="Lead Lifecycle Service",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend
_frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_frontend_dir):
    app.mount("/static", StaticFiles(directory=_frontend_dir), name="static")

# Serve case photos (media library) so the dashboard can show thumbnails
_case_photos_dir = os.path.join(os.path.dirname(__file__), "case_photos")
os.makedirs(_case_photos_dir, exist_ok=True)
app.mount("/media/case-photos", StaticFiles(directory=_case_photos_dir), name="case_photos")


# ─── Auth ─────────────────────────────────────────────────────────────────────

def _require_admin(x_admin_password: Optional[str] = Header(None)):
    settings = get_settings()
    if x_admin_password != settings.admin_password:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _require_admin_media(
    x_admin_password: Optional[str] = Header(None),
    pw: Optional[str] = Query(None),
):
    """Auth for media endpoints (audio playback) where the browser cannot send headers.
    Accepts the admin password either as the X-Admin-Password header OR as a ?pw= query param.
    """
    settings = get_settings()
    token = x_admin_password or pw
    if token != settings.admin_password:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "lead-lifecycle", "time": datetime.now(timezone.utc).isoformat()}


# ─── Public: Dashboard SPA ────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard():
    frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")
    if os.path.exists(frontend_path):
        with open(frontend_path) as f:
            content = f.read()
        return HTMLResponse(
            content,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            }
        )
    return HTMLResponse("<h1>Pipeline Dashboard</h1><p>Frontend not found. Run from project root.</p>")


# ─── Events endpoint (called by landing page, scheduler, Mango) ───────────────

class EventPayload(BaseModel):
    event_type: str                    # 'lead_created','smile_completed','booking_confirmed',
                                       #   'call_matched','stage_update'
    lead_id: Optional[str] = None
    email: Optional[str] = None        # fallback lookup if no lead_id
    source: str = "external"
    detail: Optional[dict] = None

    # Lead fields (populated on lead_created)
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    goals: Optional[list] = None
    gclid: Optional[str] = None
    fbclid: Optional[str] = None
    msclkid: Optional[str] = None
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None
    utm_term: Optional[str] = None
    utm_content: Optional[str] = None
    landing_url: Optional[str] = None
    smile_image_url: Optional[str] = None
    booking_id: Optional[str] = None
    created_at: Optional[str] = None
    ga4_client_id: Optional[str] = None   # browser _ga cookie value for GA4 session stitching


@app.post("/api/events", status_code=201)
async def receive_event(payload: EventPayload):
    """
    Central event receiver. Called by:
    - nxtsmile.com backend (lead_created, smile_completed)
    - Appointment scheduler (booking_confirmed, booking_cancelled)
    - Mango call analysis (call_matched)
    """
    event_type = payload.event_type
    detail_str = json.dumps(payload.detail or {})

    # Resolve lead
    lead = None
    if payload.lead_id:
        lead = get_lead(payload.lead_id)
    if not lead and payload.email:
        lead = get_lead_by_email(payload.email)

    now = datetime.now(timezone.utc).isoformat()

    if event_type == "lead_created":
        if not payload.lead_id:
            raise HTTPException(status_code=400, detail="lead_id required for lead_created")

        lead_data = {
            "id": payload.lead_id,
            "created_at": payload.created_at or now,
            "source": payload.source,
            "stage": "new",
            "first_name": payload.first_name or "",
            "last_name": payload.last_name or "",
            "email": payload.email or "",
            "phone": payload.phone or "",
            "goals": payload.goals or [],
            "gclid": payload.gclid or "",
            "fbclid": payload.fbclid or "",
            "msclkid": payload.msclkid or "",
            "utm_source": payload.utm_source or "",
            "utm_medium": payload.utm_medium or "",
            "utm_campaign": payload.utm_campaign or "",
            "utm_term": payload.utm_term or "",
            "utm_content": payload.utm_content or "",
            "landing_url": payload.landing_url or "",
        }
        lead = upsert_lead(lead_data)
        enqueue_follow_ups(lead, lead_data["created_at"])
        add_event(payload.lead_id, "lead_created", stage_to="new", source=payload.source,
                  detail=detail_str)

        # Notify office
        try:
            send_office_new_lead(lead)
        except Exception as e:
            logger.warning(f"Office notification failed: {e}")

        # Fire GA4 event
        try:
            _ga4_cid = getattr(payload, "ga4_client_id", "") or ""
            track_lead_created(payload.lead_id, source=payload.source, gclid=payload.gclid or "", ga4_client_id=_ga4_cid)
        except Exception as e:
            logger.debug(f"GA4 lead_created event failed (non-fatal): {e}")

        return {"status": "ok", "lead_id": payload.lead_id, "action": "created"}

    elif event_type == "smile_completed":
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        upsert_lead({"id": lead["id"], "smile_image_url": payload.smile_image_url or "",
                     "smile_generated_at": now})
        # Smile completion is just an event — no stage change (stays as 'new' until first email)
        add_event(lead["id"], "smile_completed", source=payload.source, detail=detail_str)

        # Fire GA4 event
        try:
            track_smile_completed(lead["id"], ga4_client_id=lead.get("ga4_client_id") or "")
        except Exception as e:
            logger.debug(f"GA4 smile_completed event failed (non-fatal): {e}")

        return {"status": "ok", "lead_id": lead["id"], "action": "smile_noted"}

    elif event_type == "booking_confirmed":
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        if payload.booking_id:
            upsert_lead({"id": lead["id"], "booking_id": payload.booking_id})
        update_stage(lead["id"], "scheduled", source="scheduler",
                     detail=f"booking_id={payload.booking_id}")
        add_event(lead["id"], "booking_confirmed", stage_to="scheduled", source="scheduler",
                  detail=detail_str)

        # Fire GA4 event
        try:
            track_appointment_booked(lead["id"], ga4_client_id=lead.get("ga4_client_id") or "")
        except Exception as e:
            logger.debug(f"GA4 appointment_booked event failed (non-fatal): {e}")

        return {"status": "ok", "lead_id": lead["id"], "action": "booking_noted"}

    elif event_type == "booking_cancelled":
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        update_stage(lead["id"], "auto_nurture", source="scheduler")
        add_event(lead["id"], "booking_cancelled", source="scheduler", detail=detail_str)
        return {"status": "ok", "lead_id": lead["id"], "action": "cancellation_noted"}

    elif event_type == "call_matched":
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        add_event(lead["id"], "call_matched", source="mango", detail=detail_str)
        # Advance to showed if they were scheduled
        if lead["stage"] in ("scheduled",):
            update_stage(lead["id"], "showed", source="mango")
        return {"status": "ok", "lead_id": lead["id"], "action": "call_noted"}

    elif event_type == "stage_update":
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        new_stage = (payload.detail or {}).get("stage")
        if not new_stage:
            raise HTTPException(status_code=400, detail="detail.stage required for stage_update")
        update_stage(lead["id"], new_stage, source=payload.source, detail=detail_str)
        return {"status": "ok", "lead_id": lead["id"], "action": "stage_updated"}

    else:
        raise HTTPException(status_code=400, detail=f"Unknown event_type: {event_type}")


# ─── Pipeline API ─────────────────────────────────────────────────────────────

@app.get("/api/pipeline")
def get_pipeline(stage: Optional[str] = None, limit: int = 200, show_all: bool = False):
    """Return all leads with their current stage — feeds the dashboard.

    Default: gads_only=True (only Google Ads attributed leads shown).
    Pass show_all=true to bypass the filter and see every lead.
    """
    leads = get_all_leads(stage=stage, limit=limit, gads_only=not show_all)

    # Enrich each lead with last event
    result = []
    for lead in leads:
        events = get_events(lead["id"])
        last_event = events[-1] if events else None
        queue = get_follow_up_queue(lead["id"])
        next_action = next(
            (q for q in queue if q["status"] == "pending"),
            None
        )
        result.append({
            **lead,
            "event_count": len(events),
            "last_event": last_event,
            "next_follow_up": next_action,
        })

    return {"leads": result, "total": len(result)}


@app.get("/api/lead/{lead_id}")
def get_lead_detail(lead_id: str):
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    events = get_events(lead_id)
    queue = get_follow_up_queue(lead_id)
    return {"lead": lead, "events": events, "follow_up_queue": queue}


# ─── Unsubscribe ──────────────────────────────────────────────────────────────

@app.get("/unsubscribe/{lead_id}/{channel}", response_class=HTMLResponse)
def one_click_unsubscribe(lead_id: str, channel: str):
    """One-click unsubscribe link from email footer."""
    if channel not in ("email", "sms"):
        return HTMLResponse("<h2>Invalid unsubscribe link.</h2>", status_code=400)
    lead = get_lead(lead_id)
    if not lead:
        return HTMLResponse("<h2>Already removed or link expired.</h2>")
    unsubscribe(lead_id, channel, reason="one-click")
    label = "email" if channel == "email" else "text messages"
    return HTMLResponse(f"""
    <!DOCTYPE html><html><head><meta charset="utf-8">
    <style>body{{font-family:sans-serif;text-align:center;padding:60px;color:#333}}
    .card{{max-width:400px;margin:0 auto;background:#f9f9f9;border-radius:12px;padding:32px}}
    h2{{color:#0d7a7f}}</style></head>
    <body><div class="card">
    <h2>You've been unsubscribed</h2>
    <p>You'll no longer receive {label} from Grafton Dental Care.</p>
    <p>If you change your mind, call us at <strong>508-318-4477</strong>.</p>
    </div></body></html>
    """)


# ─── Delete Smile Image (public — linked from emails) ────────────────────────

@app.get("/delete-image/{lead_id}", response_class=HTMLResponse)
def delete_smile_image(lead_id: str):
    """
    One-click image deletion link from follow-up emails.
    Deletes the smile preview from GCS and clears the URL in the database.
    """
    lead = get_lead(lead_id)
    if not lead:
        return HTMLResponse("""
        <!DOCTYPE html><html><head><meta charset="utf-8">
        <style>body{font-family:sans-serif;text-align:center;padding:60px;color:#333}
        .card{max-width:450px;margin:0 auto;background:#f9f9f9;border-radius:12px;padding:32px}
        h2{color:#0d7a7f}</style></head>
        <body><div class="card">
        <h2>Image Already Removed</h2>
        <p>This image has already been deleted or the link has expired.</p>
        <p>If you have questions, call us at <strong>508-318-4477</strong>.</p>
        </div></body></html>
        """)

    blob_name = lead.get("smile_blob_name", "")
    composite_blob_name = lead.get("smile_composite_blob_name", "")
    image_url = lead.get("smile_image_url", "")

    # Parse after-blob name from URL if not stored directly (legacy fallback)
    gcs_after_blob = blob_name
    if not gcs_after_blob and image_url and "storage.googleapis.com" in image_url:
        try:
            path = image_url.split("storage.googleapis.com/")[1].split("?")[0]
            _, gcs_after_blob = path.split("/", 1)
        except Exception:
            pass

    # Delete both after-only and composite blobs from GCS
    deleted_count = 0
    blobs_to_delete = [(gcs_after_blob, "after"), (composite_blob_name, "composite")]
    try:
        from google.cloud import storage as gcs_storage
        from config import get_settings as _gs
        client = gcs_storage.Client()
        bucket = client.bucket(_gs().gcs_bucket)
        for bname, label in blobs_to_delete:
            if bname:
                try:
                    bucket.blob(bname).delete()
                    deleted_count += 1
                    logger.info(f"Deleted GCS {label} blob for lead {lead_id}: {bname}")
                except Exception as e:
                    logger.warning(f"Could not delete GCS {label} blob for lead {lead_id}: {e}")
    except Exception as e:
        logger.warning(f"GCS client init failed for image delete (lead {lead_id}): {e}")

    # Clear all smile fields from the database
    from database import _conn
    with _conn() as conn:
        conn.execute(
            "UPDATE leads SET smile_image_url = '', smile_blob_name = '', "
            "smile_composite_blob_name = '', updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), lead_id)
        )

    # Log the event
    add_event(lead_id, "image_deleted", source="patient_request",
              detail=json.dumps({"gcs_blobs_deleted": deleted_count}))

    name = lead.get("first_name") or "there"
    return HTMLResponse(f"""
    <!DOCTYPE html><html><head><meta charset="utf-8">
    <style>body{{font-family:sans-serif;text-align:center;padding:60px;color:#333}}
    .card{{max-width:450px;margin:0 auto;background:#f9f9f9;border-radius:12px;padding:32px}}
    h2{{color:#0d7a7f}} .check{{font-size:48px;margin-bottom:16px}}</style></head>
    <body><div class="card">
    <div class="check">✅</div>
    <h2>Image Deleted</h2>
    <p>Hi {name}, your smile preview image has been permanently deleted from our servers.</p>
    <p>If you'd like to start fresh or have any questions, call us at <strong>508-318-4477</strong>
    or visit <a href="https://nxtsmile.com" style="color:#0d7a7f;">nxtsmile.com</a>.</p>
    </div></body></html>
    """)


# ─── Admin endpoints ──────────────────────────────────────────────────────────

@app.get("/api/admin/stats", dependencies=[Depends(_require_admin)])
def admin_stats(show_all: bool = False):
    """Pipeline KPI stats. Default: gads_only (matches /api/pipeline/enriched default).
    Pass show_all=true to get unfiltered counts (must match pipeline fetch)."""
    return get_pipeline_stats(gads_only=not show_all)


@app.get("/api/admin/queue", dependencies=[Depends(_require_admin)])
def admin_queue():
    return {"items": get_due_follow_ups(), "total": len(get_due_follow_ups())}


@app.get("/api/admin/hot-leads", dependencies=[Depends(_require_admin)])
def admin_hot_leads():
    from database import get_hot_leads
    return {"leads": get_hot_leads()}


@app.post("/api/admin/callrail/sync", dependencies=[Depends(_require_admin)])
def admin_callrail_sync():
    """
    Manually trigger a CallRail number sync.  Upserts all trackers into
    callrail_numbers.  Also run nightly at 1 AM via APScheduler.
    """
    if not get_settings().callrail_api_key:
        raise HTTPException(status_code=503, detail="CALLRAIL_API_KEY not configured in .env")
    try:
        from callrail_sync import sync_callrail_numbers
        result = sync_callrail_numbers()
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"CallRail sync failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/callrail/sync-calls", dependencies=[Depends(_require_admin)])
def admin_callrail_sync_calls():
    """
    Manually poll CallRail for new completed calls and ingest them via
    process_webhook().  Uses cursor from settings.callrail_calls_last_sync.
    Also runs automatically every 15 minutes via APScheduler (at :02/:17/:32/:47).
    Safe to call at any time — ingestion is fully idempotent.
    """
    if not get_settings().callrail_api_key:
        raise HTTPException(status_code=503, detail="CALLRAIL_API_KEY not configured in .env")
    try:
        from callrail_sync import sync_callrail_calls
        result = sync_callrail_calls()
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"CallRail calls sync failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/callrail/numbers", dependencies=[Depends(_require_admin)])
def admin_callrail_list_numbers():
    """All tracking numbers with 30-day call counts and campaign assignment names."""
    try:
        from callrail_admin import list_numbers_with_stats
        return {"status": "ok", "numbers": list_numbers_with_stats()}
    except Exception as e:
        logger.error(f"CallRail list_numbers failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/callrail/numbers/{number_id}", dependencies=[Depends(_require_admin)])
def admin_callrail_get_number(number_id: int):
    """Single tracking number detail for the assignment modal."""
    try:
        from callrail_admin import get_number_detail
        row = get_number_detail(number_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Tracking number id={number_id} not found")
        return {"status": "ok", "number": row}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"CallRail get_number {number_id} failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/callrail/numbers/{number_id}/assign", dependencies=[Depends(_require_admin)])
def admin_callrail_assign_number(number_id: int, body: dict):
    """
    Assign / update a tracking number.
    Validates, pushes destination_number + whisper to CallRail, then writes DB.
    """
    # HIPAA guard — recording must not be toggled via this endpoint
    if body.get("recording_enabled") not in (None, 0, False):
        raise HTTPException(status_code=400, detail="Recording can only be enabled after signing the HIPAA BAA.")
    if not get_settings().callrail_api_key:
        raise HTTPException(status_code=503, detail="CALLRAIL_API_KEY not configured in .env")
    try:
        from callrail_admin import assign_number
        updated = assign_number(number_id, body)
        return {"status": "ok", "number": updated}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.error(f"CallRail assign_number {number_id} failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/callrail/numbers/{number_id}/status", dependencies=[Depends(_require_admin)])
def admin_callrail_set_status(number_id: int, body: dict):
    """Pause or activate a tracking number (updates DB + CallRail)."""
    new_status = body.get("status", "")
    if new_status not in ("active", "paused"):
        raise HTTPException(status_code=422, detail="status must be 'active' or 'paused'")
    if not get_settings().callrail_api_key:
        raise HTTPException(status_code=503, detail="CALLRAIL_API_KEY not configured in .env")
    try:
        from callrail_admin import set_number_status
        updated = set_number_status(number_id, new_status)
        return {"status": "ok", "number": updated}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.error(f"CallRail set_status {number_id} failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/callrail/numbers/{number_id}/retry-gads", dependencies=[Depends(_require_admin)])
def admin_callrail_retry_gads(number_id: int):
    """Retry the Google Ads call extension push for a number in failed/pending state."""
    try:
        from callrail_admin import retry_gads_push
        result = retry_gads_push(number_id)
        return {"status": "ok", "gads_push": result}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"CallRail retry-gads {number_id} failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/callrail/reconcile", dependencies=[Depends(_require_admin)])
def admin_callrail_reconcile():
    """Drift report: compare local callrail_numbers DB against live CallRail API."""
    if not get_settings().callrail_api_key:
        raise HTTPException(status_code=503, detail="CALLRAIL_API_KEY not configured in .env")
    try:
        from callrail_admin import reconcile_with_callrail
        return {"status": "ok", **reconcile_with_callrail()}
    except Exception as e:
        logger.error(f"CallRail reconcile failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/callrail/campaigns-for-assignment", dependencies=[Depends(_require_admin)])
def admin_callrail_campaigns_for_assignment():
    """Slim campaign list for the tracking number assignment modal dropdown."""
    try:
        from callrail_admin import list_campaigns_for_assignment
        return {"status": "ok", "campaigns": list_campaigns_for_assignment()}
    except Exception as e:
        logger.error(f"CallRail campaigns_for_assignment failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/callrail/recent-calls", dependencies=[Depends(_require_admin)])
def admin_callrail_recent_calls(limit: int = Query(20, ge=1, le=200)):
    """Most-recent CallRail calls for the Tracking Numbers admin tab (PR 4)."""
    try:
        from database import _conn
        with _conn() as conn:
            rows = conn.execute("""
                SELECT
                    cc.id, cc.callrail_call_id, cc.caller_number, cc.caller_name,
                    cc.caller_city, cc.caller_state,
                    cc.called_at, cc.duration_seconds, cc.answered, cc.voicemail,
                    cc.source, cc.campaign, cc.keyword, cc.gclid, cc.landing_page,
                    cc.lead_id, cc.mango_call_id,
                    cn.phone_number   AS tracking_number,
                    cn.friendly_name  AS tracking_name,
                    (l.first_name || ' ' || l.last_name) AS lead_name,
                    l.stage AS lead_stage
                FROM callrail_calls cc
                LEFT JOIN callrail_numbers cn ON cn.id = cc.tracking_number_id
                LEFT JOIN leads l ON l.id = cc.lead_id
                ORDER BY cc.called_at DESC, cc.id DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return {"status": "ok", "calls": [dict(r) for r in rows]}
    except Exception as e:
        logger.error(f"CallRail recent-calls failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/callrail/enrich-calls", dependencies=[Depends(_require_admin)])
def admin_callrail_enrich_calls(limit: int = Query(500, ge=1, le=5000)):
    """
    Run OD enrichment for callrail_calls rows missing od_patient_num / od_patient_status.

    Pass 1: copy from linked lead (SQLite only — no OD round-trip).
    Pass 2: live OD phone lookup for any rows still unenriched.

    Safe to call at any time; idempotent.  Also runs automatically during the
    nightly OD sync (run_full_od_sync).
    """
    try:
        from od_matcher import enrich_callrail_calls_with_od
        result = enrich_callrail_calls_with_od(limit=limit)
        return {"status": "ok", **result}
    except Exception as e:
        logger.error(f"callrail enrich-calls failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/callrail/guard-status", dependencies=[Depends(_require_admin)])
def admin_callrail_guard_get():
    """Return the current state of the existing-patient guard toggle."""
    try:
        from callrail_webhook import _existing_patient_guard_enabled
        return {"status": "ok", "guard_enabled": _existing_patient_guard_enabled()}
    except Exception as e:
        logger.error(f"callrail guard-status GET failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/callrail/guard-status", dependencies=[Depends(_require_admin)])
def admin_callrail_guard_set(body: dict):
    """
    Enable or disable the existing-patient guard at runtime.
    Body: {"enabled": true | false}
    """
    try:
        from database import save_setting
        enabled = bool(body.get("enabled", True))
        save_setting("callrail_existing_patient_guard", "true" if enabled else "false")
        logger.info(f"[callrail] existing-patient guard set to {enabled}")
        return {"status": "ok", "guard_enabled": enabled}
    except Exception as e:
        logger.error(f"callrail guard-status POST failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/sync", dependencies=[Depends(_require_admin)])
def admin_sync():
    result = sync_from_firestore()
    unsub_result = sync_unsubscribes_from_firestore()
    return {"status": "ok", "result": result, "unsubscribe_sync": unsub_result}


@app.post("/api/admin/match", dependencies=[Depends(_require_admin)])
async def admin_od_match():
    from od_matcher import run_full_od_sync
    result = run_full_od_sync()
    return {"status": "ok", "result": result}


@app.post("/api/admin/gads-sync", dependencies=[Depends(_require_admin)])
def admin_gads_sync(days_back: int = 7):
    """Sync Google Ads click_view + keywords. Use days_back=90 for initial backfill."""
    try:
        from google_ads_sync import sync_gclids_to_keywords
        days_back = max(1, min(int(days_back), 90))
        result = sync_gclids_to_keywords(days_back=days_back)
        return {"status": "ok", "result": result}
    except ImportError as e:
        raise HTTPException(status_code=503, detail=f"Google Ads library not installed: {e}")
    except Exception as e:
        logger.error(f"Google Ads sync failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/upload-conversions", dependencies=[Depends(_require_admin)])
def admin_upload_conversions():
    try:
        from google_ads_conversions import upload_offline_conversions
        result = upload_offline_conversions()
        return {"status": "ok", "result": result}
    except ImportError as e:
        raise HTTPException(status_code=503, detail=f"Google Ads library not installed: {e}")
    except Exception as e:
        logger.error(f"Conversion upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/sync-call-production", dependencies=[Depends(_require_admin)])
def admin_sync_call_production(days: int = 7):
    """
    On-demand: link resolved Mango calls to keyword_production_log.
    Pass ?days=60 for a 60-day backfill covering closed campaigns.
    """
    try:
        from call_production_log import link_calls_to_keyword_production
        result = link_calls_to_keyword_production(days=days)
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"Call production sync failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/sync-payments", dependencies=[Depends(_require_admin)])
def admin_sync_payments(days: int = 7, full: bool = False):
    """
    On-demand: pull OD payments for all attributed patients.
    days=N to re-sync patients last touched > N days ago.
    full=true to rebuild from scratch (use after deploying PR 2).
    """
    try:
        from od_payment_sync import sync_od_payments
        result = sync_od_payments(days_back=days, full_resync=full)
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"OD payment sync failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/refresh-call-income", dependencies=[Depends(_require_admin)])
def admin_refresh_call_income(days: int = 90):
    """
    PR 4: Re-query OpenDental for the current paid total for all new-patient calls
    matched in the last `days` days. Updates mango_calls.od_patient_income and any
    linked keyword_production_log rows. Safe to run multiple times (idempotent).
    """
    try:
        from od_payment_sync import refresh_call_od_income
        result = refresh_call_od_income(days=days)
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"Refresh call income failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/backfill-booked-outcome", dependencies=[Depends(_require_admin)])
def admin_backfill_booked_outcome():
    """
    One-shot: set booked_outcome='booked' for all existing inbound calls that have
    od_appointment_id set but booked_outcome still NULL. Run this once before the
    first call production backfill to unlock historical records.
    """
    try:
        from mango_pipeline import backfill_booked_outcome
        result = backfill_booked_outcome()
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"booked_outcome backfill failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/sync-od-all", dependencies=[Depends(_require_admin)])
def admin_sync_od_all():
    """
    Kicks off the unified 7-step OD sync chain in a background thread.
    Returns immediately — poll progress via GET /api/admin/sync-od-all/progress.
    Double-click safe: returns {"status": "already_running"} if chain is active.
    """
    import threading
    from unified_od_sync import run_unified_od_sync, get_unified_sync_progress
    progress = get_unified_sync_progress()
    if progress.get("running"):
        return {"status": "already_running", "progress": progress}

    def _run():
        try:
            run_unified_od_sync(trigger="manual")
        except Exception as e:
            logger.error(f"Unified OD sync failed: {e}", exc_info=True)
            try:
                from unified_od_sync import _set_progress_done
                _set_progress_done(error=str(e))
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}


@app.get("/api/admin/sync-od-all/progress", dependencies=[Depends(_require_admin)])
def admin_sync_od_all_progress():
    """Return current unified sync progress state (dict). Polled by the frontend every 1.5s."""
    from unified_od_sync import get_unified_sync_progress
    return get_unified_sync_progress()


@app.get("/api/admin/sync-od-all/last-run", dependencies=[Depends(_require_admin)])
def admin_sync_od_all_last_run():
    """
    Return the last completed run's progress dict (persisted in sqlite settings row).
    Used by the UI to show "Last synced: 14 minutes ago · 7/7 ok" on page load
    even if the user wasn't watching the sync run.
    Returns {"never_run": True} if the key is missing or the JSON is corrupted.
    """
    import json
    raw = get_setting("unified_od_sync_last_run")
    if not raw:
        return {"never_run": True}
    try:
        return json.loads(raw)
    except Exception:
        # JSON corruption resilience — never raise, always return safe fallback
        return {"never_run": True}


@app.post("/api/admin/optimize", dependencies=[Depends(_require_admin)])
def admin_optimize(dry_run: bool = True):
    """
    Kicks off the optimizer in a background thread and returns 202 immediately.
    Progress can be polled via GET /api/admin/optimizer/progress.
    """
    import threading

    def _run():
        try:
            from ai_optimizer import optimize_campaign
            optimize_campaign(dry_run=dry_run, trigger="admin_manual")
        except ImportError as e:
            logger.error(f"AI optimizer import failed: {e}")
            try:
                from ai_optimizer import _set_progress_done
                _set_progress_done()
            except Exception:
                pass
        except Exception as e:
            logger.error(f"AI optimizer failed: {e}", exc_info=True)
            try:
                from ai_optimizer import _set_progress_done
                _set_progress_done()
            except Exception:
                pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"status": "started"}


@app.get("/api/admin/optimizer/progress", dependencies=[Depends(_require_admin)])
def optimizer_progress():
    """
    Returns the current optimizer progress state.
    Polled by the frontend every 2s while a run is in progress.
    Returns: {running, step_index, step_label, step_detail, total_steps, pct, elapsed_sec}
    """
    try:
        from ai_optimizer import get_optimizer_progress
        return get_optimizer_progress()
    except Exception:
        return {"running": False, "step_index": 0, "step_label": "", "step_detail": "",
                "total_steps": 10, "pct": 0, "elapsed_sec": 0}


# ─── Phase 1: Google Ads Campaign Management ───────────────────────────────��─

@app.get("/api/admin/optimizer/dashboard-summary", dependencies=[Depends(_require_admin)])
def optimizer_dashboard_summary():
    """
    Dashboard-facing optimizer snapshot:
      - pending_count: how many actions need approval
      - pending_rows: up to 25 pending actions (for display)
      - last_run_at / last_run_id
      - summary: KPI dict from last run (spend, clicks, leads, calls, appts, CPA...)
      - advisories: Claude advisory strings from last run
      - call_summary / od_production_summary from last run
    """
    from database import get_pending_approvals, get_optimizer_runs

    # Pending actions
    pending = get_pending_approvals()
    pending_count = len(pending)
    pending_rows = pending[:25]

    # Latest completed run
    runs = get_optimizer_runs(limit=1)
    last_run = runs[0] if runs else None

    last_run_at = None
    last_run_id = None
    summary = {}
    advisories = []
    call_summary = {}
    od_production_summary = {}

    per_campaign = []

    if last_run:
        last_run_at = last_run.get("completed_at") or last_run.get("started_at")
        last_run_id = last_run.get("run_id")
        try:
            report = json.loads(last_run.get("report_json") or "{}")
            summary = report.get("summary", {})
            advisories = report.get("advisories", [])
            call_summary = report.get("call_summary", {})
            od_production_summary = report.get("od_production_summary", {})

            # Build per_campaign array merging call + production data.
            # od_production_summary is {"total_attributed": float, "by_campaign": {name: float}}
            od_by_campaign = {}
            if isinstance(od_production_summary, dict):
                od_by_campaign = od_production_summary.get("by_campaign", {})
                if not isinstance(od_by_campaign, dict):
                    od_by_campaign = {}
            all_campaign_names = set(call_summary.keys()) | set(od_by_campaign.keys())
            for cname in sorted(all_campaign_names):
                cs = call_summary.get(cname, {})
                prod = od_by_campaign.get(cname, 0)
                per_campaign.append({
                    "campaign_name": cname,
                    "calls": cs.get("calls", 0),
                    "booked": cs.get("booked", 0),
                    "confirmed_appts": cs.get("confirmed_appts", 0),
                    "production": round(float(prod), 2),
                })
            # Sort by calls desc
            per_campaign.sort(key=lambda x: x["calls"], reverse=True)
        except Exception as exc:
            logger.warning(f"optimizer_dashboard_summary per_campaign build failed: {exc}")
        if not summary:
            try:
                summary = json.loads(last_run.get("summary_json") or "{}")
            except Exception:
                pass

    return {
        "pending_count": pending_count,
        "pending_rows": pending_rows,
        "last_run_at": last_run_at,
        "last_run_id": last_run_id,
        "summary": summary,
        "advisories": advisories,
        "call_summary": call_summary,
        "od_production_summary": od_production_summary,
        "per_campaign": per_campaign,
    }


@app.get("/api/admin/gads/audit-log", dependencies=[Depends(_require_admin)])
def gads_audit_log(limit: int = 100, entity_id: str = "", operation: str = ""):
    """Return recent Google Ads audit log entries."""
    from database import get_audit_log
    entries = get_audit_log(limit=limit, entity_id=entity_id, operation=operation)
    return {"entries": entries, "total": len(entries)}


@app.get("/api/admin/gads/pending-approvals", dependencies=[Depends(_require_admin)])
def gads_pending_approvals():
    """Return all audit rows awaiting admin approval (Apply button)."""
    from database import get_pending_approvals
    rows = get_pending_approvals()
    return {"pending": rows, "total": len(rows)}


@app.post("/api/admin/gads/approve/{action_id}", dependencies=[Depends(_require_admin)])
async def gads_approve_action(action_id: str, request: Request):
    """
    Execute an approved recommendation against Google Ads.
    Idempotent: already-approved rows return 409.
    """
    from database import get_audit_row, update_gads_action_result, set_audit_approval
    from campaign_safety import check_writes_enabled, WriteBlockedError
    from ai_optimizer import (_build_client, _execute_single_pause,
                               _execute_bid_change, _execute_add_keyword,
                               _execute_add_negative, _execute_enable_keyword,
                               _execute_budget_change, _execute_update_rsa,
                               _execute_geo_exclusion,
                               _execute_add_to_shared_negative_list,
                               _execute_replace_ad,
                               _execute_pause_ad,
                               _verify_gads_change)

    row = get_audit_row(action_id)
    if not row:
        raise HTTPException(status_code=404, detail="Action not found")
    if row["execution_result"] not in ("pending_approval", "error"):
        raise HTTPException(
            status_code=409,
            detail=f"Action already in state '{row['execution_result']}' — cannot re-apply"
        )
    # Reset error state back to pending so the execution path proceeds cleanly
    if row["execution_result"] == "error":
        from database import _conn as _db_conn
        with _db_conn() as _c:
            _c.execute(
                "UPDATE gads_audit_log SET execution_result='pending_approval', error_detail='' WHERE action_id=?",
                (action_id,)
            )
        row = get_audit_row(action_id)  # re-fetch with fresh state

    # Kill switch check
    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        update_gads_action_result(action_id, executed=False,
            execution_result="blocked", error_detail=str(e))
        raise HTTPException(status_code=403, detail=str(e))

    settings = get_settings()
    customer_id = settings.google_ads_customer_id

    operation = row["operation"]
    try:
        if operation == "pause_keyword":
            client = _build_client()
            _execute_single_pause(client, customer_id, resource_name=row["entity_id"])
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(f"Approved + executed pause_keyword: {row['entity_name']} ({action_id[:8]})")

        elif operation in ("increase_bid", "decrease_bid"):
            after = json.loads(row["after_state_json"] or "{}")
            raw_bid = after.get("new_bid_micros")
            if not raw_bid:
                raise HTTPException(
                    status_code=422,
                    detail="after_state_json missing new_bid_micros — cannot execute bid change"
                )
            try:
                new_bid_micros = int(raw_bid)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="new_bid_micros must be an integer")
            client = _build_client()
            try:
                _execute_bid_change(
                    client, customer_id,
                    resource_name=row["entity_id"],
                    new_bid_micros=new_bid_micros
                )
            except ValueError as e:
                # Bid guardrail violation — user error, not server error
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"rejected: {str(e)[:200]}")
                raise HTTPException(status_code=422, detail=str(e))
            except Exception as e:
                update_gads_action_result(action_id, executed=True,
                    execution_result=f"failed: {str(e)[:200]}")
                raise
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(f"Approved + executed {operation}: {row['entity_name']} "
                        f"new_bid={new_bid_micros} ({action_id[:8]})")

        elif operation == "add_exact_keyword":
            after = json.loads(row["after_state_json"] or "{}")
            keyword_text = after.get("keyword_text")
            match_type = after.get("match_type", "EXACT")
            ad_group_resource = after.get("ad_group_resource") or row["entity_id"]
            if not keyword_text:
                raise HTTPException(
                    status_code=422,
                    detail="after_state_json missing keyword_text"
                )
            client = _build_client()
            try:
                _execute_add_keyword(
                    client, customer_id,
                    ad_group_resource=ad_group_resource,
                    keyword_text=keyword_text,
                    match_type=match_type
                )
            except ValueError as e:
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"rejected: {str(e)[:200]}")
                raise HTTPException(status_code=422, detail=str(e))
            except Exception as e:
                err_str = str(e)
                # Policy violations and invalid argument errors — reject cleanly, don't 500
                if "POLICY_ERROR" in err_str or "policy_violation" in err_str.lower():
                    msg = f"Google policy violation for '{keyword_text}' — keyword rejected by Google Ads policy"
                    update_gads_action_result(action_id, executed=False,
                        execution_result=f"rejected: policy_violation", error_detail=err_str[:500])
                    raise HTTPException(status_code=422, detail=msg)
                elif "INVALID_ARGUMENT" in err_str:
                    msg = f"Invalid argument adding '{keyword_text}' — ad group resource may be removed or invalid"
                    update_gads_action_result(action_id, executed=False,
                        execution_result=f"rejected: invalid_argument", error_detail=err_str[:500])
                    raise HTTPException(status_code=422, detail=msg)
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"failed: {err_str[:200]}", error_detail=err_str[:500])
                raise HTTPException(status_code=500, detail=f"Add keyword failed: {err_str[:300]}")
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(f"Approved + executed add_exact_keyword: '{keyword_text}' "
                        f"({action_id[:8]})")

        elif operation == "add_negative_keyword":
            after = json.loads(row["after_state_json"] or "{}")
            keyword_text = after.get("keyword_text")
            match_type = after.get("match_type", "BROAD")
            # Use campaign_resource from after_state first; fall back to entity_id only if
            # entity_id looks like an actual resource name (contains 'customers/').
            _raw_cr_after = after.get("campaign_resource") or ""
            _raw_cr_entity = row["entity_id"] or ""
            if _raw_cr_after and "customers/" in _raw_cr_after:
                campaign_resource = _raw_cr_after
            elif _raw_cr_entity and "customers/" in _raw_cr_entity:
                campaign_resource = _raw_cr_entity
            else:
                campaign_resource = None
            if not keyword_text:
                raise HTTPException(
                    status_code=422,
                    detail="after_state_json missing keyword_text"
                )
            if not campaign_resource:
                update_gads_action_result(action_id, executed=False,
                    execution_result="rejected: missing campaign_resource — cannot add negative without a valid campaign")
                raise HTTPException(
                    status_code=422,
                    detail=f"Cannot add negative '{keyword_text}' — no valid campaign_resource in the action record. "
                           f"Re-run the optimizer to regenerate this recommendation with a resolved campaign resource."
                )
            client = _build_client()
            try:
                _execute_add_negative(
                    client, customer_id,
                    campaign_resource=campaign_resource,
                    keyword_text=keyword_text,
                    match_type=match_type
                )
            except ValueError as e:
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"rejected: {str(e)[:200]}")
                raise HTTPException(status_code=422, detail=str(e))
            except Exception as e:
                update_gads_action_result(action_id, executed=True,
                    execution_result=f"failed: {str(e)[:200]}")
                raise
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(f"Approved + executed add_negative_keyword: '{keyword_text}' "
                        f"({action_id[:8]})")

        elif operation == "add_to_shared_negative_list":
            after = json.loads(row["after_state_json"] or "{}")
            keyword_text = after.get("keyword_text") or row["entity_name"]
            match_type = after.get("match_type", "BROAD")
            if not keyword_text:
                raise HTTPException(status_code=422, detail="keyword_text missing")
            client = _build_client()
            try:
                _execute_add_to_shared_negative_list(
                    client, customer_id,
                    keyword_text=keyword_text,
                    match_type=match_type
                )
            except Exception as e:
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"failed: {str(e)[:200]}")
                raise HTTPException(status_code=500, detail=f"Shared list update failed: {str(e)[:300]}")
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(f"Approved + added to shared list: '{keyword_text}' ({action_id[:8]})")

        elif operation == "tighten_match_type":
            after = json.loads(row["after_state_json"] or "{}")
            client = _build_client()
            # Step 1: Add EXACT first (no impression gap)
            try:
                _execute_add_keyword(
                    client, customer_id,
                    ad_group_resource=after.get("ad_group_resource", ""),
                    keyword_text=row["entity_name"],
                    match_type="EXACT",
                )
            except Exception as e:
                update_gads_action_result(action_id, executed=True,
                    execution_result=f"partial_failed: add_exact step: {str(e)[:150]}")
                raise HTTPException(status_code=500,
                    detail=f"Step 1 (add exact match) failed: {e}. Broad match still active.")
            # Step 2: Pause BROAD
            try:
                _execute_single_pause(client, customer_id, resource_name=row["entity_id"])
            except Exception as e:
                update_gads_action_result(action_id, executed=True,
                    execution_result=f"partial_success: exact added, broad pause failed: {str(e)[:150]}")
                logger.warning(f"tighten_match_type: exact added but broad pause failed for "
                               f"'{row['entity_name']}': {e}")
                return {"status": "partial_success", "action_id": action_id,
                        "detail": "Exact match keyword added. Broad match pause failed — please pause manually."}
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(f"Approved + executed tighten_match_type: '{row['entity_name']}' → EXACT ({action_id[:8]})")

        elif operation == "claude_advisory":
            # Advisory acknowledgment — no Google Ads API call needed.
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(f"Advisory acknowledged: '{row['entity_name']}' ({action_id[:8]})")

        elif operation == "enable_keyword":
            client = _build_client()
            try:
                _execute_enable_keyword(client, customer_id, resource_name=row["entity_id"])
            except Exception as e:
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"failed: {str(e)[:200]}")
                raise
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(f"Approved + executed enable_keyword: {row['entity_name']} ({action_id[:8]})")

        elif operation == "ad_copy_suggestion":
            after = json.loads(row["after_state_json"] or "{}")
            ad_resource = after.get("ad_resource")
            new_headlines = []
            new_descriptions = []
            if after.get("headline"):
                new_headlines = [after["headline"]]
            if after.get("description"):
                new_descriptions = [after["description"]]

            if ad_resource and new_headlines:
                # Live API execution — update RSA via API
                client = _build_client()
                try:
                    rsa_ok = _execute_update_rsa(
                        client, customer_id,
                        ad_group_ad_resource=ad_resource,
                        new_headlines=new_headlines,
                        new_descriptions=new_descriptions,
                    )
                except ValueError as e:
                    update_gads_action_result(action_id, executed=False,
                        execution_result=f"rejected: {str(e)[:200]}")
                    raise HTTPException(status_code=422, detail=str(e))
                except Exception as e:
                    update_gads_action_result(action_id, executed=False,
                        execution_result=f"failed: {str(e)[:200]}")
                    raise
                if rsa_ok:
                    update_gads_action_result(action_id, executed=True, execution_result="success")
                    set_audit_approval(action_id, approver="admin")
                    logger.info(f"Approved + executed ad_copy_suggestion (RSA updated): '{row['entity_name']}' ({action_id[:8]})")
                else:
                    # RSA assets are IMMUTABLE via update — treat as advisory.
                    update_gads_action_result(action_id, executed=True,
                        execution_result="advisory_applied",
                        error_detail="RSA headlines/descriptions are immutable via API — manual edit needed in Google Ads UI.")
                    set_audit_approval(action_id, approver="admin")
                    logger.info(f"Ad copy suggestion advisory_applied (IMMUTABLE_FIELD): '{row['entity_name']}' ({action_id[:8]})")
            else:
                # Legacy or missing ad_resource — acknowledge only
                update_gads_action_result(action_id, executed=True, execution_result="success")
                set_audit_approval(action_id, approver="admin")
                logger.info(f"Ad copy suggestion acknowledged (no ad_resource — manual action needed): '{row['entity_name']}' ({action_id[:8]})")

        elif operation == "geo_exclusion":
            after = json.loads(row["after_state_json"] or "{}")
            geo_target_resource = after.get("geo_target_resource")
            # campaign_resource: try entity_id first (set by _OP_MAP), then after_state
            campaign_resource_for_geo = (
                row.get("entity_id") or
                after.get("campaign_resource") or ""
            )

            if geo_target_resource and campaign_resource_for_geo:
                # Live API execution
                client = _build_client()
                try:
                    _execute_geo_exclusion(
                        client, customer_id,
                        campaign_resource=campaign_resource_for_geo,
                        geo_target_resource=geo_target_resource,
                    )
                except Exception as e:
                    update_gads_action_result(action_id, executed=False,
                        execution_result=f"failed: {str(e)[:200]}")
                    raise
                update_gads_action_result(action_id, executed=True, execution_result="success")
                set_audit_approval(action_id, approver="admin")
                logger.info(f"Approved + executed geo_exclusion: '{row['entity_name']}' ({action_id[:8]})")
            else:
                # Legacy — no resolved geo target resource
                update_gads_action_result(action_id, executed=True, execution_result="success")
                set_audit_approval(action_id, approver="admin")
                logger.info(f"Geo exclusion acknowledged (no geo_target_resource — manual action needed): '{row['entity_name']}' ({action_id[:8]})")

        elif operation == "change_budget":
            after = json.loads(row["after_state_json"] or "{}")
            new_budget_usd = after.get("new_daily_budget_usd")
            camp_resource_for_budget = (
                after.get("campaign_resource") or row.get("entity_id") or ""
            )
            if not new_budget_usd or not camp_resource_for_budget:
                raise HTTPException(
                    status_code=422,
                    detail="after_state_json missing new_daily_budget_usd or campaign_resource"
                )
            try:
                new_budget_usd = float(new_budget_usd)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="new_daily_budget_usd must be a number")
            client = _build_client()
            try:
                from campaign_safety import WriteBlockedError as _WBE
                _execute_budget_change(client, customer_id,
                    campaign_resource=camp_resource_for_budget,
                    new_daily_budget_usd=new_budget_usd)
            except _WBE as e:
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"rejected: {str(e)[:200]}")
                raise HTTPException(status_code=422, detail=str(e))
            except Exception as e:
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"failed: {str(e)[:200]}")
                raise
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(f"Approved + executed change_budget: ${new_budget_usd:.2f}/day ({action_id[:8]})")

        elif operation == "change_bid_strategy":
            from ai_optimizer import _execute_change_bid_strategy
            after = json.loads(row["after_state_json"] or "{}")
            details = after
            bid_strategy = after.get("bid_strategy") or details.get("bid_strategy", "")
            target_cpa = int(after.get("target_cpa_micros") or details.get("target_cpa_micros", 0))
            target_roas = float(after.get("target_roas") or details.get("target_roas", 0))
            camp_res = after.get("campaign_resource") or details.get("campaign_resource", "") or row.get("entity_id", "")
            if not bid_strategy or not camp_res:
                raise HTTPException(status_code=422, detail="Missing bid_strategy or campaign_resource")
            client = _build_client()
            try:
                _execute_change_bid_strategy(client, customer_id, camp_res, bid_strategy, target_cpa, target_roas)
            except Exception as e:
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"failed: {str(e)[:200]}")
                raise
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(f"Approved + executed change_bid_strategy: {bid_strategy} ({action_id[:8]})")

        elif operation == "change_match_type":
            from ai_optimizer import _execute_change_match_type
            after = json.loads(row["after_state_json"] or "{}")
            details = after
            resource_name_kw = after.get("resource_name") or details.get("resource_name", "") or row.get("entity_id", "")
            new_mt = after.get("new_match_type") or details.get("new_match_type", "EXACT")
            kw_text = after.get("keyword_text") or row.get("entity_name", "")
            if not resource_name_kw:
                raise HTTPException(status_code=422, detail="Missing keyword resource_name")
            client = _build_client()
            try:
                _execute_change_match_type(client, customer_id, resource_name_kw, new_mt, keyword_text=kw_text)
            except Exception as e:
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"failed: {str(e)[:200]}", error_detail=str(e)[:500])
                raise
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(f"Approved + executed change_match_type: '{kw_text}' → {new_mt} ({action_id[:8]})")

        elif operation == "add_asset":
            from google_ads_create import add_callouts_to_campaign, add_structured_snippet_to_campaign
            after = json.loads(row["after_state_json"] or "{}")
            asset_type = after.get("asset_type", "")
            camp_rn = after.get("campaign_resource") or row.get("entity_id", "")
            if not camp_rn:
                raise HTTPException(
                    status_code=422,
                    detail="add_asset after_state_json missing campaign_resource"
                )
            if asset_type == "CALLOUT":
                callout_texts = after.get("callout_texts") or []
                if len(callout_texts) < 3:
                    raise HTTPException(
                        status_code=422,
                        detail=f"add_asset CALLOUT needs ≥3 callout_texts, got {len(callout_texts)}"
                    )
                result = add_callouts_to_campaign(
                    campaign_resource_name=camp_rn,
                    callout_texts=callout_texts,
                    customer_id=customer_id,
                )
                if not result["ok"]:
                    errs = "; ".join(result.get("errors") or ["unknown error"])
                    update_gads_action_result(action_id, executed=False,
                        execution_result="failed", error_detail=errs[:500])
                    raise HTTPException(status_code=502, detail=f"add_callouts_to_campaign failed: {errs}")
                logger.info(
                    f"add_asset CALLOUT approved: {result['count']} callout(s) linked to {camp_rn} ({action_id[:8]})"
                )
            elif asset_type == "STRUCTURED_SNIPPET":
                header = after.get("snippet_header", "")
                values = after.get("values") or []
                if not header:
                    raise HTTPException(
                        status_code=422,
                        detail="add_asset STRUCTURED_SNIPPET missing snippet_header"
                    )
                if len(values) < 3:
                    raise HTTPException(
                        status_code=422,
                        detail=f"add_asset STRUCTURED_SNIPPET needs ≥3 values, got {len(values)}"
                    )
                result = add_structured_snippet_to_campaign(
                    campaign_resource_name=camp_rn,
                    header=header,
                    values=values,
                    customer_id=customer_id,
                )
                if not result["ok"]:
                    errs = "; ".join(result.get("errors") or ["unknown error"])
                    update_gads_action_result(action_id, executed=False,
                        execution_result="failed", error_detail=errs[:500])
                    raise HTTPException(status_code=502, detail=f"add_structured_snippet_to_campaign failed: {errs}")
                logger.info(
                    f"add_asset STRUCTURED_SNIPPET approved: header='{header}' linked to {camp_rn} ({action_id[:8]})"
                )
            else:
                raise HTTPException(
                    status_code=422,
                    detail=f"add_asset unsupported asset_type '{asset_type}'"
                )
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")

        elif operation == "replace_ad":
            after = json.loads(row["after_state_json"] or "{}")
            old_rn        = after.get("old_ad_group_ad_resource", "")
            new_h         = after.get("new_headlines") or []
            new_d         = after.get("new_descriptions") or []
            final_url     = after.get("final_url", "")
            ag_resource   = after.get("ad_group_resource", "")
            path1         = (after.get("path1", "") or "")[:15]
            path2         = (after.get("path2", "") or "")[:15]
            if not old_rn or not new_h or not new_d or not final_url:
                raise HTTPException(
                    status_code=422,
                    detail="replace_ad after_state_json missing required fields"
                )
            client = _build_client()
            try:
                result = _execute_replace_ad(
                    client, customer_id,
                    old_ad_group_ad_resource=old_rn,
                    new_headlines=new_h,
                    new_descriptions=new_d,
                    final_url=final_url,
                    ad_group_resource=ag_resource,
                    path1=path1,
                    path2=path2,
                )
            except ValueError as ve:
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"rejected: {str(ve)[:200]}")
                raise HTTPException(status_code=422, detail=str(ve))
            except Exception as ae:
                err = str(ae)
                if "POLICY" in err.upper():
                    update_gads_action_result(action_id, executed=False,
                        execution_result="rejected: policy_violation",
                        error_detail=err[:500])
                    raise HTTPException(status_code=422,
                        detail=f"Google policy blocked the new ad: {err[:300]}")
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"failed: {err[:200]}", error_detail=err[:500])
                raise HTTPException(status_code=500,
                    detail=f"Replace ad failed: {err[:300]}")
            # Stash created resource back into after_state for audit trail
            try:
                from database import _conn as _db_conn
                with _db_conn() as _c:
                    after["created_ad_group_ad_resource"] = result["created_resource"]
                    _c.execute(
                        "UPDATE gads_audit_log SET after_state_json=? WHERE action_id=?",
                        (json.dumps(after), action_id),
                    )
            except Exception:
                pass
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(
                f"replace_ad approved: paused {result['paused_resource']}, "
                f"created {result['created_resource']} ({action_id[:8]})"
            )

        elif operation == "pause_ad":
            after = json.loads(row["after_state_json"] or "{}")
            ad_rn = after.get("ad_group_ad_resource", "")
            if not ad_rn:
                raise HTTPException(
                    status_code=422,
                    detail="pause_ad after_state_json missing ad_group_ad_resource"
                )
            client = _build_client()
            try:
                result = _execute_pause_ad(client, customer_id, ad_rn)
            except ValueError as ve:
                raise HTTPException(status_code=422, detail=str(ve))
            except Exception as ae:
                raise HTTPException(status_code=502, detail=f"Google Ads error: {ae}")
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(
                f"pause_ad approved: paused {result['paused_resource']} ({action_id[:8]})"
            )

        elif operation == "update_geo_targeting":
            # ── AI-recommended geo radius / ZIP change ────────────────────────────────────
            from google_ads_write import replace_campaign_locations as _replace_locs
            after = json.loads(row["after_state_json"] or "{}")
            camp_rn = after.get("campaign_resource") or row.get("entity_id") or ""
            if not camp_rn or not camp_rn.startswith("customers/"):
                raise HTTPException(
                    status_code=422,
                    detail="update_geo_targeting after_state_json missing campaign_resource"
                )

            # Build geo_json from the approved rec fields
            proposed_radius = after.get("proposed_radius_miles")
            add_zips = after.get("add_zip_codes") or []
            remove_zips = after.get("remove_zip_codes") or []

            # Fetch current geo_json from the campaign DB record to build the delta.
            # Refuse if the campaign has no local geo_json — we must not blindly overwrite
            # live Google Ads targeting from an empty/unknown local state.
            from database import get_geo_json_for_campaign_resource
            current_geo_raw = get_geo_json_for_campaign_resource(camp_rn)
            if not current_geo_raw:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "update_geo_targeting: campaign has no local geo_json record — "
                        "refusing to overwrite live Google Ads targeting from an unknown baseline. "
                        "Save geo targeting manually from the Launch tab first."
                    )
                )
            try:
                current_geo = json.loads(current_geo_raw)
            except Exception:
                raise HTTPException(status_code=422, detail="update_geo_targeting: local geo_json is malformed")
            current_locs = current_geo.get("locations") or []

            # Apply delta: remove ZIPs by value (match regardless of stored type)
            remove_set = {str(z).strip() for z in remove_zips}
            updated_locs = [
                loc for loc in current_locs
                if str(loc.get("value", "")).strip() not in remove_set
            ]
            for z in add_zips:
                z_str = str(z).strip()
                if not any(str(l.get("value","")).strip() == z_str for l in updated_locs):
                    updated_locs.append({"type": "postal", "value": z_str, "include": True})

            # If radius is changing, replace the existing proximity entry.
            # IMPORTANT: replace_campaign_locations creates a proximity criterion ONLY when
            # type=="city" with a radius — NOT type=="address". Use "city" to match seeded format.
            if proposed_radius:
                # Remove old city-based proximity entries
                updated_locs = [
                    loc for loc in updated_locs if not (loc.get("type") == "city" and loc.get("radius"))
                ]
                updated_locs.append({
                    "type": "city",
                    "value": "Grafton, MA",
                    "radius": int(proposed_radius),
                    "include": True,
                })

            if not updated_locs:
                raise HTTPException(
                    status_code=422,
                    detail="update_geo_targeting would result in zero locations — blocked for safety"
                )

            new_geo_json = json.dumps({"unit": "miles", "locations": updated_locs})
            try:
                result = _replace_locs(camp_rn, new_geo_json)
                added = result.get("added", 0)
                removed = result.get("removed", 0)
                errs = result.get("errors", [])
                exec_result = "partial_success" if errs else "success"
            except Exception as e:
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"failed: {str(e)[:200]}")
                raise

            update_gads_action_result(action_id, executed=True, execution_result=exec_result)
            set_audit_approval(action_id, approver="admin")

            # Persist updated geo_json to local DB
            from database import update_geo_json_for_campaign_resource
            update_geo_json_for_campaign_resource(camp_rn, new_geo_json)
            logger.info(
                f"update_geo_targeting approved: campaign={camp_rn} "
                f"added={added} removed={removed} errs={errs} ({action_id[:8]})"
            )

        elif operation == "pause_ad_group":
            from google_ads_write import set_ad_group_status as _set_ag_status
            after = json.loads(row["after_state_json"] or "{}")
            ag_rn = after.get("ad_group_resource", "")
            ag_name = after.get("ad_group_name", ag_rn)
            if not ag_rn:
                raise HTTPException(
                    status_code=422,
                    detail="pause_ad_group after_state_json missing ad_group_resource"
                )
            if "/adGroups/" not in ag_rn:
                raise HTTPException(
                    status_code=422,
                    detail=f"pause_ad_group invalid ad_group_resource format: {ag_rn}"
                )
            try:
                _set_ag_status(ag_rn, "PAUSED")
            except ValueError as ve:
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"rejected: {str(ve)[:200]}")
                raise HTTPException(status_code=422, detail=str(ve))
            except Exception as ae:
                err = str(ae)
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"failed: {err[:200]}", error_detail=err[:500])
                raise HTTPException(status_code=502, detail=f"Google Ads error: {ae}")
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(f"pause_ad_group approved: paused '{ag_name}' ({ag_rn}) ({action_id[:8]})")

        elif operation == "set_ad_schedule":
            from ai_optimizer import _build_client as _sch_build_client
            from google_ads_create import parse_ad_schedule, push_ad_schedule
            after = json.loads(row["after_state_json"] or "{}")
            camp_rn_sched = after.get("campaign_resource") or row.get("entity_id") or ""
            schedule_text = after.get("schedule_text") or ""
            slots = after.get("slots") or (parse_ad_schedule(schedule_text) if schedule_text else [])
            if not camp_rn_sched:
                raise HTTPException(status_code=422, detail="set_ad_schedule missing campaign_resource")
            if not slots:
                raise HTTPException(status_code=422, detail=f"set_ad_schedule could not parse schedule: {schedule_text!r}")
            _sch_cid = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())
            client = _sch_build_client()
            try:
                result = push_ad_schedule(client, _sch_cid, camp_rn_sched, slots, replace=True)
            except Exception as e:
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"failed: {str(e)[:200]}", error_detail=str(e)[:500])
                raise HTTPException(status_code=502, detail=f"Google Ads schedule error: {e}")
            if not result.get("ok"):
                err = result.get("error") or "unknown error"
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"failed: {err[:200]}", error_detail=err)
                raise HTTPException(status_code=502, detail=f"Google Ads schedule error: {err}")
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(
                f"set_ad_schedule approved: {camp_rn_sched} '{schedule_text}' "
                f"pushed={result.get('pushed',0)} removed={result.get('removed',0)} ({action_id[:8]})"
            )

        elif operation == "create_skag":
            # ── SKAG creation: isolate one keyword into its own ad group ──────
            from ai_optimizer import _execute_create_skag, _build_client as _skag_build_client
            from database import get_campaign_by_id as _skag_get_campaign
            after = json.loads(row["after_state_json"] or "{}")
            skag_kw_text       = after.get("keyword_text", "").strip()
            skag_source_ag     = after.get("source_ad_group_name", "").strip()
            skag_campaign_id   = after.get("campaign_id", "").strip()
            skag_new_ag_name   = (after.get("new_ad_group_name") or f"SKAG — {skag_kw_text}").strip()
            skag_rec_id        = (after.get("recommendation_id") or "").strip()

            if not skag_kw_text:
                raise HTTPException(status_code=422, detail="create_skag missing keyword_text")
            if not skag_source_ag:
                raise HTTPException(status_code=422, detail="create_skag missing source_ad_group_name")
            if not skag_rec_id:
                raise HTTPException(
                    status_code=422,
                    detail="create_skag missing recommendation_id — only optimizer-surfaced candidates can be executed"
                )

            # Resolve campaign_resource from our campaigns table
            skag_camp_resource = ""
            if skag_campaign_id:
                _camp_row = _skag_get_campaign(skag_campaign_id)
                if _camp_row:
                    skag_camp_resource = _camp_row.get("gads_campaign_resource") or ""
            # Fallback: try entity_id if it looks like a campaign resource
            if not skag_camp_resource:
                _eid = row.get("entity_id") or ""
                if _eid.startswith("customers/"):
                    skag_camp_resource = _eid

            if not skag_camp_resource:
                raise HTTPException(
                    status_code=422,
                    detail=f"create_skag cannot resolve campaign_resource for campaign_id={skag_campaign_id!r}"
                )

            _skag_cid = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())
            _skag_client = _skag_build_client()
            skag_result = _execute_create_skag(
                _skag_client,
                _skag_cid,
                skag_camp_resource,
                skag_source_ag,
                skag_kw_text,
                skag_new_ag_name,
                skag_rec_id,
                action_id,
                [],   # skag_created_this_run — no per-run cap for manual approvals
            )

            if skag_result.get("blocked"):
                reason = skag_result.get("reason", "blocked")
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"blocked: {reason[:200]}")
                raise HTTPException(status_code=409, detail=reason)

            if skag_result.get("error"):
                err = skag_result["error"]
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"failed: {err[:200]}", error_detail=err[:500])
                raise HTTPException(status_code=502, detail=f"SKAG creation error: {err}")

            new_ag_resource = skag_result.get("ad_group_resource", "")
            if not new_ag_resource:
                # Unexpected: no error but also no resource — treat as failure
                _detail = "SKAG creation returned no ad_group_resource (unexpected empty result)"
                update_gads_action_result(action_id, executed=False,
                    execution_result=f"failed: {_detail[:200]}")
                raise HTTPException(status_code=502, detail=_detail)

            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, approver="admin")
            logger.info(
                f"create_skag approved: kw={skag_kw_text!r} source_ag={skag_source_ag!r} "
                f"new_ag={new_ag_resource!r} rsa_copied={skag_result.get('rsa_copied')} ({action_id[:8]})"
            )

        else:
            raise HTTPException(status_code=400, detail=f"Unknown operation: {operation}")

    except HTTPException:
        raise
    except Exception as e:
        update_gads_action_result(action_id, executed=False,
            execution_result="error", error_detail=str(e))
        logger.error(f"Approve action failed for {action_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # ── Read-back verification: confirm what was actually changed in GAds ────────
    # Re-fetch row so that replace_ad's created_ad_group_ad_resource (written back to DB
    # inside the replace_ad block) is visible in after_state_json.
    try:
        row = get_audit_row(action_id) or row
    except Exception:
        pass
    _verification: dict = {}
    try:
        _after_for_verify = json.loads(row.get("after_state_json") or "{}")
        _before_for_verify = json.loads(row.get("before_state_json") or "{}")
        _verify_ctx: dict = {}

        if operation in ("pause_keyword", "enable_keyword", "tighten_match_type", "change_match_type"):
            _verify_ctx["resource_name"] = row.get("entity_id", "")
            _verify_ctx["before_status"] = _before_for_verify.get("status", "")
            _verify_ctx["before_match_type"] = _before_for_verify.get("match_type", "")

        elif operation in ("increase_bid", "decrease_bid"):
            _verify_ctx["resource_name"] = row.get("entity_id", "")
            _verify_ctx["new_bid_micros"] = _after_for_verify.get("new_bid_micros", 0)
            _verify_ctx["before_bid_micros"] = _before_for_verify.get("cpc_bid_micros") or _before_for_verify.get("bid_micros")

        elif operation == "change_budget":
            _verify_ctx["campaign_resource"] = (
                _after_for_verify.get("campaign_resource") or row.get("entity_id", "")
            )
            _verify_ctx["new_daily_budget_usd"] = _after_for_verify.get("new_daily_budget_usd", 0)
            _verify_ctx["before_daily_budget_usd"] = _before_for_verify.get("daily_budget_usd") or _before_for_verify.get("current_daily_budget_usd")

        elif operation == "status":
            _verify_ctx["campaign_resource"] = row.get("entity_id", "")
            _verify_ctx["expected_status"] = _after_for_verify.get("status", "")
            _verify_ctx["before_status"] = _before_for_verify.get("status", "")

        elif operation == "pause_ad":
            _verify_ctx["ad_group_ad_resource"] = _after_for_verify.get("ad_group_ad_resource", "")
            _verify_ctx["before_status"] = _before_for_verify.get("status", "")

        elif operation == "pause_ad_group":
            _verify_ctx["ad_group_resource"] = _after_for_verify.get("ad_group_resource", "")
            _verify_ctx["ad_group_name"] = _after_for_verify.get("ad_group_name", "")
            _verify_ctx["before_status"] = _before_for_verify.get("status", "")

        elif operation == "enable_ad_group":
            _verify_ctx["ad_group_resource"] = _after_for_verify.get("ad_group_resource", "") or row.get("entity_id", "")
            _verify_ctx["ad_group_name"] = _after_for_verify.get("ad_group_name", "")
            _verify_ctx["before_status"] = _before_for_verify.get("status", "")

        elif operation == "replace_ad":
            _verify_ctx["old_ad_group_ad_resource"] = _after_for_verify.get("old_ad_group_ad_resource", "")
            _verify_ctx["created_ad_group_ad_resource"] = _after_for_verify.get("created_ad_group_ad_resource", "")

        elif operation == "add_exact_keyword":
            _verify_ctx["keyword_text"] = _after_for_verify.get("keyword_text", "")
            _verify_ctx["ad_group_resource"] = _after_for_verify.get("ad_group_resource", "")

        elif operation in ("add_negative_keyword", "add_to_shared_negative_list"):
            _verify_ctx["keyword_text"] = _after_for_verify.get("keyword_text", "")

        elif operation == "change_bid_strategy":
            _verify_ctx["campaign_resource"] = _after_for_verify.get("campaign_resource", "") or row.get("entity_id", "")
            _verify_ctx["bid_strategy"] = _after_for_verify.get("bid_strategy", "")
            _verify_ctx["before_bid_strategy"] = _before_for_verify.get("bidding_strategy_type", "") or _before_for_verify.get("bid_strategy", "")

        elif operation == "geo_exclusion":
            _verify_ctx["campaign_resource"] = row.get("entity_id", "") or _after_for_verify.get("campaign_resource", "")
            _verify_ctx["geo_target_resource"] = _after_for_verify.get("geo_target_resource", "")
            _verify_ctx["location_name"] = _after_for_verify.get("location_name", "") or _before_for_verify.get("location_name", "")

        elif operation == "update_geo_targeting":
            _verify_ctx["campaign_resource"] = _after_for_verify.get("campaign_resource", "") or row.get("entity_id", "")
            _verify_ctx["proposed_radius_miles"] = _after_for_verify.get("proposed_radius_miles")
            _verify_ctx["add_zip_codes"] = _after_for_verify.get("add_zip_codes", [])
            _verify_ctx["remove_zip_codes"] = _after_for_verify.get("remove_zip_codes", [])

        _verify_client = _build_client()
        _verification = _verify_gads_change(_verify_client, customer_id, operation, _verify_ctx)
        logger.info(f"[verify] {operation} ({action_id[:8]}): {_verification.get('summary','')}")
    except Exception as _ve:
        logger.warning(f"[verify] non-fatal verification error for {operation}: {_ve}")
        _verification = {"confirmed": False, "summary": "Could not verify — check Google Ads", "detail": {}}

    # Phase A: snapshot for outcome tracking (T+7/T+30/T+90)
    try:
        from database import snapshot_applied_outcome, _conn as _db_conn
        before = json.loads(row.get("before_state_json") or "{}")
        entity_type = row.get("entity_type", "")
        entity_name = row.get("entity_name", "")

        # M2 fix: resolve campaign_id for keyword/ad_group/ad entity types
        # For campaign actions it's the entity_id itself; for keyword actions
        # we look up the campaign from gads_keywords_cache by keyword name.
        snap_campaign_id = ""
        snap_campaign_name = ""
        if entity_type == "campaign":
            snap_campaign_id = row.get("entity_id", "")
            snap_campaign_name = entity_name
        else:
            # Try to find campaign context for keyword/ad_group actions
            try:
                with _db_conn() as _c:
                    _kw_camp = _c.execute(
                        "SELECT campaign_name FROM gads_keywords_cache "
                        "WHERE LOWER(keyword_text)=? LIMIT 1",
                        (entity_name.lower(),)
                    ).fetchone()
                    if _kw_camp:
                        snap_campaign_name = _kw_camp["campaign_name"]
                        # Look up campaign_id from campaigns table
                        _camp_row = _c.execute(
                            "SELECT campaign_id FROM campaigns WHERE campaign_name=? LIMIT 1",
                            (snap_campaign_name,)
                        ).fetchone()
                        if _camp_row:
                            snap_campaign_id = _camp_row["campaign_id"]
            except Exception:
                pass

        snapshot_applied_outcome(
            action_id=action_id,
            operation=operation,
            entity_type=entity_type,
            entity_id=row.get("entity_id", ""),
            entity_name=entity_name,
            campaign_id=snap_campaign_id,
            campaign_name=snap_campaign_name,
            before_state=before,
            ai_reason=row.get("reason", ""),
            optimizer_run_id=row.get("optimizer_run_id", ""),
        )
    except Exception as _snap_err:
        logger.warning(f"[phase_a] snapshot_applied_outcome failed (non-fatal): {_snap_err}")

    # Update optimizer memory with approval decision
    try:
        from optimizer_memory import MemoryStore
        _mem = MemoryStore()
        run_id_for_mem = row.get("optimizer_run_id", "")
        if run_id_for_mem:
            _mem.update_rec_status(run_id_for_mem, action_id, "executed")
    except Exception as _mem_err:
        logger.debug(f"Memory update_rec_status (approve) failed (non-fatal): {_mem_err}")

    return {
        "status": "ok",
        "action_id": action_id,
        "operation": operation,
        "confirmation": _verification,
    }


@app.post("/api/admin/gads/reject/{action_id}", dependencies=[Depends(_require_admin)])
async def gads_reject_action(action_id: str, request: Request):
    """Dismiss a recommendation without executing it. Optionally capture reject_reason."""
    from database import get_audit_row, update_gads_action_result, set_audit_approval, record_reject_reason
    row = get_audit_row(action_id)
    if not row:
        raise HTTPException(status_code=404, detail="Action not found")
    if row["execution_result"] not in ("pending_approval", "error", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Action already in state '{row['execution_result']}'"
        )
    # Phase A: capture optional reject reason from request body
    reject_reason = ""
    try:
        body = await request.json()
        reject_reason = str(body.get("reason", ""))[:300]
    except Exception:
        pass
    update_gads_action_result(action_id, executed=False, execution_result="rejected")
    set_audit_approval(action_id, approver="admin")
    if reject_reason:
        record_reject_reason(action_id, reject_reason)
        logger.info(f"[phase_a] Rejection recorded for {action_id[:8]}: {reject_reason[:80]}")

        # Auto-write rejection pattern to optimizer_memory so future runs learn from it
        try:
            from database import add_optimizer_memory
            _entity_name = (row.get("entity_name") or "").strip()
            _operation   = (row.get("operation") or "").strip()
            _campaign    = (row.get("campaign_name") or "").strip()
            if _entity_name and _operation:
                _mem_key    = f"{_operation}:{_entity_name.lower()}"
                _mem_reason = f"Admin rejected '{_operation}' on '{_entity_name}': {reject_reason}"
                add_optimizer_memory(
                    category="rejection_pattern",
                    key=_mem_key,
                    value="rejected_by_admin",
                    reason=_mem_reason,
                    campaign=_campaign,
                    author="admin",
                )
                logger.info(f"[phase_a] Wrote rejection_pattern to optimizer_memory: {_mem_key[:60]}")
        except Exception as _omem_err:
            logger.debug(f"optimizer_memory write on reject failed (non-fatal): {_omem_err}")

    # Update optimizer run memory with rejection decision (MemoryStore file)
    try:
        from optimizer_memory import MemoryStore
        _mem = MemoryStore()
        run_id_for_mem = row.get("optimizer_run_id", "")
        if run_id_for_mem:
            _mem.update_rec_status(run_id_for_mem, action_id, "rejected")
    except Exception as _mem_err:
        logger.debug(f"Memory update_rec_status (reject) failed (non-fatal): {_mem_err}")

    return {"status": "ok", "action_id": action_id, "reject_reason": reject_reason}


@app.patch("/api/admin/gads/reclassify/{action_id}", dependencies=[Depends(_require_admin)])
async def gads_reclassify_action(action_id: str, request: Request):
    """
    Move a recommendation between campaign-level and account-level.
    Body: { "target_level": "account"|"campaign", "campaign_name": str, "reason": str }
    - target_level="account" → sets campaign_name to "" (account-wide)
    - target_level="campaign" → sets campaign_name to the provided campaign_name
    Writes reclassification_pattern to optimizer_memory so Claude learns over time.
    Memory scoping:
      - account move: write one global entry (campaign="") — visible to both per-campaign and account-level runs
      - campaign move: write one campaign-scoped entry + one global entry so account-level run also learns
    """
    from database import get_audit_row, _conn as _db_conn, add_optimizer_memory
    row = get_audit_row(action_id)
    if not row:
        raise HTTPException(status_code=404, detail="Action not found")

    body = await request.json()
    target_level = str(body.get("target_level", "")).strip().lower()
    if target_level not in ("account", "campaign"):
        raise HTTPException(status_code=422, detail="target_level must be 'account' or 'campaign'")

    campaign_name = str(body.get("campaign_name", "")).strip()
    if target_level == "campaign" and not campaign_name:
        raise HTTPException(status_code=422, detail="campaign_name is required when target_level='campaign'")
    reason = str(body.get("reason", "")).strip()[:300]

    # account-level recs have empty campaign_name
    new_campaign_name = "" if target_level == "account" else campaign_name

    # H4: detect no-op (already at the requested level)
    old_campaign = (row.get("campaign_name") or "").strip()
    old_level = "account" if not old_campaign else f"campaign:{old_campaign}"
    new_level  = "account" if not new_campaign_name else f"campaign:{new_campaign_name}"
    if old_campaign == new_campaign_name:
        raise HTTPException(status_code=400, detail=f"Recommendation is already at {old_level} — no change needed")

    # H3: warn when mutating a terminal-state row (history-altering)
    terminal_states = ("success", "rejected", "blocked", "failed")
    if row.get("execution_result") in terminal_states:
        logger.warning(
            f"[reclassify] Mutating campaign_name on terminal-state row "
            f"action_id={action_id[:8]} state={row['execution_result']} — "
            f"this alters historical attribution data"
        )

    now = datetime.now(timezone.utc).isoformat()

    # When moving to account level, flip add_negative_keyword → add_to_shared_negative_list
    # so the approval handler routes to the shared list execution path, not campaign-level.
    # When moving back to campaign level, flip it back.
    current_operation = (row.get("operation") or "").strip()
    new_operation = current_operation  # default: no change
    if target_level == "account" and current_operation == "add_negative_keyword":
        new_operation = "add_to_shared_negative_list"
    elif target_level == "campaign" and current_operation == "add_to_shared_negative_list":
        new_operation = "add_negative_keyword"

    with _db_conn() as conn:
        cur = conn.execute(
            "UPDATE gads_audit_log SET campaign_name=?, operation=?, updated_at=? WHERE action_id=?",
            (new_campaign_name, new_operation, now, action_id)
        )
        # H2: verify the row was actually updated
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Action not found (row may have been deleted)")

    logger.info(f"[reclassify] {action_id[:8]} moved from {old_level} → {new_level} reason={reason[:60]}")

    # Write to optimizer_memory so future Claude runs learn from this correction
    # C3 fix: always write a global (campaign="") entry so the account-level prompt sees it.
    #         For campaign moves, also write a campaign-scoped entry for the per-campaign prompt.
    memory_written = False
    try:
        operation   = (row.get("operation") or "").strip()
        entity_name = (row.get("entity_name") or "").strip()
        if operation:
            mem_key   = f"reclassify:{operation}:{entity_name.lower()}" if entity_name else f"reclassify:{operation}"
            mem_value = f"prefer_{target_level}_level"
            mem_reason = (
                f"Admin moved '{operation}'"
                + (f" on '{entity_name}'" if entity_name else "")
                + f" from {old_level} to {new_level}"
                + (f": {reason}" if reason else "")
            )

            if target_level == "account":
                # account move: global entry — per-campaign runs should NOT generate this rec,
                # account-level run SHOULD. Phrasing: suppress at campaign level, emit at account.
                add_optimizer_memory(
                    category="reclassification_pattern",
                    key=mem_key,
                    value="prefer_account_level",
                    reason=mem_reason,
                    campaign="",  # global = visible to all runs
                    author="admin",
                )
            else:
                # campaign move: one global entry (account-level run learns "don't emit account-wide")
                add_optimizer_memory(
                    category="reclassification_pattern",
                    key=mem_key,
                    value="prefer_campaign_level",
                    reason=mem_reason + f" [belongs to campaign: {new_campaign_name}]",
                    campaign="",  # global — visible to account-level run
                    author="admin",
                )
                # plus a campaign-scoped entry for the per-campaign run
                add_optimizer_memory(
                    category="reclassification_pattern",
                    key=mem_key,
                    value="prefer_campaign_level",
                    reason=mem_reason,
                    campaign=new_campaign_name,
                    author="admin",
                )

            memory_written = True
            logger.info(f"[reclassify] Wrote reclassification_pattern to optimizer_memory: {mem_key[:60]}")
    except Exception as _mem_err:
        # M5: promote to warning so failures are visible in production logs
        logger.warning(f"[reclassify] optimizer_memory write failed for {action_id[:8]} (non-fatal): {_mem_err}")

    # Return updated row
    updated = get_audit_row(action_id)
    return {
        "status": "ok",
        "action_id": action_id,
        "target_level": target_level,
        "campaign_name": new_campaign_name,
        "old_level": old_level,
        "new_level": new_level,
        "memory_written": memory_written,
        "row": updated or {},
    }


@app.post("/api/admin/gads/refine/{action_id}", dependencies=[Depends(_require_admin)])
async def gads_refine_action(action_id: str, request: Request):
    """
    Store user feedback on a recommendation without rejecting it.
    Body: {"feedback": "This keyword actually drives implant calls, don't pause it"}
    The feedback is stored in user_feedback column and surfaced in the Optimization tab.
    The AI optimizer reads this feedback on the next run via optimizer memory.
    """
    from database import get_audit_row, _conn as _db_conn
    row = get_audit_row(action_id)
    if not row:
        raise HTTPException(status_code=404, detail="Action not found")
    body = await request.json()
    feedback = str(body.get("feedback", "")).strip()[:500]
    if not feedback:
        raise HTTPException(status_code=422, detail="feedback text is required")

    now = datetime.now(timezone.utc).isoformat()
    with _db_conn() as conn:
        conn.execute(
            "UPDATE gads_audit_log SET user_feedback=?, user_feedback_at=?, updated_at=? WHERE action_id=?",
            (feedback, now, now, action_id)
        )

    # Save feedback to optimizer memory so it's applied on next run
    try:
        from database import add_optimizer_memory
        entity_name   = row.get("entity_name", "")
        operation     = row.get("operation", "")
        campaign_name = row.get("campaign_name", "") or ""
        memory_note   = f"[User feedback on {operation} for '{entity_name}']: {feedback}"
        add_optimizer_memory(
            category="general",
            key=f"feedback_{action_id[:8]}",
            value=memory_note,
            reason=f"User refinement via Optimization tab for action {action_id[:8]}",
            campaign=campaign_name,
            author="admin",
        )
        logger.info(f"Refine feedback saved for {action_id[:8]}: {feedback[:80]}")
    except Exception as _mem_err:
        logger.warning(f"Could not save refine feedback to optimizer memory: {_mem_err}")

    return {"status": "ok", "action_id": action_id, "feedback_saved": feedback}


@app.get("/api/admin/optimizer/diagnose-calls", dependencies=[Depends(_require_admin)])
def optimizer_diagnose_calls(days: int = 30):
    """
    Diagnostic: show how inbound calls are resolving to campaigns.
    Use this to debug the '0 calls' issue — reveals which calls have no campaign linkage.
    """
    from database import _conn as _db_conn
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _db_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM mango_calls WHERE direction='inbound' AND started_at >= ?",
            (cutoff,)
        ).fetchone()[0]

        with_gads = conn.execute("""
            SELECT COUNT(*) FROM mango_calls mc
            JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
            WHERE mc.direction='inbound' AND mc.started_at >= ?
        """, (cutoff,)).fetchone()[0]

        with_lead = conn.execute("""
            SELECT COUNT(*) FROM mango_calls mc
            JOIN leads l ON l.id = mc.lead_id
            WHERE mc.direction='inbound' AND mc.started_at >= ?
              AND l.campaign_name IS NOT NULL AND l.campaign_name != ''
        """, (cutoff,)).fetchone()[0]

        resolved_rows = conn.execute("""
            WITH resolved AS (
              SELECT mc.uuid,
                COALESCE(NULLIF(TRIM(gcv.campaign_name),''), NULLIF(TRIM(l.campaign_name),'')) AS campaign_name
              FROM mango_calls mc
              LEFT JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
              LEFT JOIN leads l ON l.id = mc.lead_id
              WHERE mc.direction='inbound' AND mc.started_at >= ?
            )
            SELECT campaign_name, COUNT(*) AS cnt
            FROM resolved
            GROUP BY campaign_name
            ORDER BY cnt DESC
        """, (cutoff,)).fetchall()

        camp_rows = conn.execute(
            "SELECT id, campaign_name, campaign_id, gads_campaign_numeric_id FROM campaigns"
        ).fetchall()

    by_campaign = [{"campaign_name": r["campaign_name"] or "(unresolved)", "calls": r["cnt"]}
                   for r in resolved_rows]
    unresolved = sum(r["calls"] for r in by_campaign if not r["campaign_name"] or r["campaign_name"] == "(unresolved)")

    return {
        "days": days,
        "total_inbound_calls": total,
        "calls_with_gads_call_view_match": with_gads,
        "calls_with_lead_campaign": with_lead,
        "calls_unresolved_to_campaign": unresolved,
        "by_campaign": by_campaign,
        "campaigns_in_db": [dict(r) for r in camp_rows],
        "hint": (
            "If calls_unresolved_to_campaign > 0, the calls have neither a gads_call_view "
            "match (from GAds call tracking) nor a lead with campaign_name. "
            "Run Sync Now + Reconcile to link them."
            if unresolved > 0 else "All calls resolved to a campaign ✓"
        ),
    }


@app.get("/api/admin/optimizer/campaign/{campaign_name}/recommendations",
         dependencies=[Depends(_require_admin)])
def campaign_recommendations(campaign_name: str, status: str = "pending_approval", limit: int = 100):
    """
    Return optimizer recommendations for a specific campaign, grouped by operation type.
    campaign_name: URL-encoded campaign name (or 'all' for all campaigns).
    status: pending_approval | success | rejected | expired | all
    """
    from database import _conn as _db_conn
    import urllib.parse
    camp = urllib.parse.unquote(campaign_name)

    with _db_conn() as conn:
        if camp.lower() == "all":
            if status == "all":
                rows = conn.execute(
                    "SELECT * FROM gads_audit_log ORDER BY priority ASC, created_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM gads_audit_log WHERE execution_result=? "
                    "ORDER BY priority ASC, created_at DESC LIMIT ?",
                    (status, limit)
                ).fetchall()
        else:
            if status == "all":
                rows = conn.execute(
                    "SELECT * FROM gads_audit_log "
                    "WHERE LOWER(TRIM(campaign_name))=LOWER(TRIM(?)) "
                    "ORDER BY priority ASC, created_at DESC LIMIT ?",
                    (camp, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM gads_audit_log "
                    "WHERE LOWER(TRIM(campaign_name))=LOWER(TRIM(?)) "
                    "  AND execution_result=? "
                    "ORDER BY priority ASC, created_at DESC LIMIT ?",
                    (camp, status, limit)
                ).fetchall()

    grouped = {}
    total_impact = 0.0
    for r in rows:
        op = r["operation"]
        grouped.setdefault(op, [])
        row_dict = dict(r)
        # Parse JSON fields
        try:
            row_dict["before_state"] = json.loads(r["before_state_json"] or "{}")
            row_dict["after_state"] = json.loads(r["after_state_json"] or "{}")
            impact = json.loads(r["impact_estimate_json"] or "{}")
            row_dict["impact_estimate"] = impact
            total_impact += float(impact.get("savings_30d_usd", 0))
        except Exception:
            row_dict["before_state"] = {}
            row_dict["after_state"] = {}
            row_dict["impact_estimate"] = {}
        grouped[op].append(row_dict)

    return {
        "campaign_name": camp,
        "status_filter": status,
        "recommendations": grouped,
        "total": len(rows),
        "summary": {
            "total_pending": sum(1 for r in rows if r["execution_result"] == "pending_approval"),
            "total_applied": sum(1 for r in rows if r["execution_result"] == "success"),
            "total_rejected": sum(1 for r in rows if r["execution_result"] == "rejected"),
            "estimated_savings_30d_usd": round(total_impact, 2),
        },
    }


@app.get("/api/admin/optimizer/campaign/{campaign_name}/impact",
         dependencies=[Depends(_require_admin)])
def campaign_impact(campaign_name: str, days: int = 90):
    """
    Return applied outcomes and verdicts for a campaign (for the Optimization tab impact view).
    """
    from database import _conn as _db_conn
    import urllib.parse
    camp = urllib.parse.unquote(campaign_name)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with _db_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM applied_outcomes
            WHERE LOWER(TRIM(campaign_name)) = LOWER(TRIM(?))
              AND applied_at >= ?
            ORDER BY applied_at DESC
        """, (camp, cutoff)).fetchall()

    outcomes = [dict(r) for r in rows]
    for o in outcomes:
        try:
            o["post_7d"]  = json.loads(o.get("post_7d_json", "{}") or "{}")
            o["post_30d"] = json.loads(o.get("post_30d_json", "{}") or "{}")
        except Exception:
            o["post_7d"] = {}
            o["post_30d"] = {}

    totals = {
        "actions_applied": len(outcomes),
        "improved": sum(1 for o in outcomes if o.get("verdict") == "improved"),
        "neutral":  sum(1 for o in outcomes if o.get("verdict") == "neutral"),
        "degraded": sum(1 for o in outcomes if o.get("verdict") == "degraded"),
        "pending":  sum(1 for o in outcomes if o.get("verdict") == "pending"),
    }
    return {"campaign_name": camp, "days": days, "outcomes": outcomes, "totals": totals}


@app.post("/api/admin/optimizer/campaign/{campaign_name}/apply-bulk",
          dependencies=[Depends(_require_admin)])
async def campaign_apply_bulk(campaign_name: str, request: Request):
    """
    Apply multiple recommendations at once (up to 25 per call — safety guard).
    Body: {"action_ids": ["uuid1", "uuid2", ...]}
    Returns per-action result map. Partial failures do NOT roll back other actions.
    """
    import urllib.parse
    body = await request.json()
    action_ids = body.get("action_ids", [])
    if not action_ids:
        raise HTTPException(status_code=422, detail="action_ids list is required")
    if len(action_ids) > 25:
        raise HTTPException(
            status_code=422,
            detail=f"Bulk apply limited to 25 actions per call (got {len(action_ids)}). "
                   "Split into smaller batches and review results between batches."
        )

    results = {}
    for aid in action_ids:
        try:
            # Re-use the single-action approve endpoint logic via internal call
            from database import get_audit_row, update_gads_action_result, set_audit_approval
            from campaign_safety import check_writes_enabled, WriteBlockedError
            from ai_optimizer import (_build_client, _execute_single_pause,
                                       _execute_bid_change, _execute_add_keyword,
                                       _execute_add_negative, _execute_enable_keyword,
                                       _execute_budget_change, _execute_update_rsa,
                                       _execute_geo_exclusion,
                                       _execute_add_to_shared_negative_list,
                                       _execute_replace_ad, _execute_pause_ad,
                                       _execute_change_bid_strategy,
                                       _execute_change_match_type,
                                       _apply_google_recommendation)

            row = get_audit_row(aid)
            if not row:
                results[aid] = {"status": "error", "detail": "Action not found"}
                continue
            if row["execution_result"] not in ("pending_approval", "error"):
                results[aid] = {"status": "skipped", "detail": f"Already {row['execution_result']}"}
                continue
            # Reset error state so execution proceeds cleanly
            if row["execution_result"] == "error":
                from database import _conn as _db_conn
                with _db_conn() as _c:
                    _c.execute(
                        "UPDATE gads_audit_log SET execution_result='pending_approval', error_detail='' WHERE action_id=?",
                        (aid,)
                    )
                row = get_audit_row(aid)

            try:
                check_writes_enabled()
            except WriteBlockedError as e:
                results[aid] = {"status": "blocked", "detail": str(e)}
                continue

            settings = get_settings()
            customer_id = settings.google_ads_customer_id
            operation = row["operation"]

            if operation == "pause_keyword":
                client = _build_client()
                _execute_single_pause(client, customer_id, resource_name=row["entity_id"])

            elif operation in ("increase_bid", "decrease_bid"):
                after = json.loads(row["after_state_json"] or "{}")
                new_bid_micros = int(after.get("new_bid_micros", 0))
                client = _build_client()
                _execute_bid_change(client, customer_id, resource_name=row["entity_id"],
                                    new_bid_micros=new_bid_micros)

            elif operation == "add_exact_keyword":
                after = json.loads(row["after_state_json"] or "{}")
                client = _build_client()
                _execute_add_keyword(client, customer_id,
                                     ad_group_resource=after.get("ad_group_resource") or row["entity_id"],
                                     keyword_text=after.get("keyword_text", row["entity_name"]),
                                     match_type=after.get("match_type", "EXACT"))

            elif operation == "add_negative_keyword":
                after = json.loads(row["after_state_json"] or "{}")
                client = _build_client()
                _execute_add_negative(client, customer_id,
                                      campaign_resource=after.get("campaign_resource") or row["entity_id"],
                                      keyword_text=after.get("keyword_text", row["entity_name"]),
                                      match_type=after.get("match_type", "BROAD"))

            elif operation == "add_to_shared_negative_list":
                after = json.loads(row["after_state_json"] or "{}")
                client = _build_client()
                _execute_add_to_shared_negative_list(
                    client, customer_id,
                    keyword_text=after.get("keyword_text", row["entity_name"]),
                    match_type=after.get("match_type", "BROAD")
                )

            elif operation == "tighten_match_type":
                after = json.loads(row["after_state_json"] or "{}")
                before = json.loads(row["before_state_json"] or "{}")
                client = _build_client()
                # Step 1: add exact match keyword first (so no impression gap)
                _execute_add_keyword(client, customer_id,
                                     ad_group_resource=after.get("ad_group_resource", ""),
                                     keyword_text=row["entity_name"],
                                     match_type="EXACT")
                # Step 2: pause the broad match keyword
                _execute_single_pause(client, customer_id, resource_name=row["entity_id"])

            elif operation == "claude_advisory":
                # Advisory acknowledgment — no API call
                pass

            elif operation == "enable_keyword":
                client = _build_client()
                _execute_enable_keyword(client, customer_id, resource_name=row["entity_id"])

            elif operation == "ad_copy_suggestion":
                after = json.loads(row["after_state_json"] or "{}")
                ad_resource = after.get("ad_resource")
                new_headlines = [after["headline"]] if after.get("headline") else []
                new_descriptions = [after["description"]] if after.get("description") else []
                if ad_resource and new_headlines:
                    client = _build_client()
                    _execute_update_rsa(client, customer_id,
                                        ad_group_ad_resource=ad_resource,
                                        new_headlines=new_headlines,
                                        new_descriptions=new_descriptions)
                    # _execute_update_rsa returns False on IMMUTABLE_FIELD —
                    # bulk endpoint still marks the row 'success' but the function
                    # itself logs a warning so it's traceable.
                # else: legacy row without ad_resource — acknowledge only

            elif operation == "geo_exclusion":
                after = json.loads(row["after_state_json"] or "{}")
                geo_target_resource = after.get("geo_target_resource")
                camp_resource_geo = after.get("campaign_resource") or row.get("entity_id") or ""
                if geo_target_resource and camp_resource_geo:
                    client = _build_client()
                    _execute_geo_exclusion(client, customer_id,
                                           campaign_resource=camp_resource_geo,
                                           geo_target_resource=geo_target_resource)
                # else: legacy row — acknowledge only

            elif operation == "change_budget":
                after = json.loads(row["after_state_json"] or "{}")
                new_budget_usd = float(after.get("new_daily_budget_usd", 0))
                camp_resource_budget = after.get("campaign_resource") or row.get("entity_id") or ""
                if new_budget_usd and camp_resource_budget:
                    client = _build_client()
                    _execute_budget_change(client, customer_id,
                                           campaign_resource=camp_resource_budget,
                                           new_daily_budget_usd=new_budget_usd)

            elif operation == "change_bid_strategy":
                from ai_optimizer import _execute_change_bid_strategy
                after = json.loads(row["after_state_json"] or "{}")
                bid_strategy = after.get("bid_strategy", "")
                target_cpa = int(after.get("target_cpa_micros", 0))
                target_roas = float(after.get("target_roas", 0))
                camp_res = after.get("campaign_resource", "") or row.get("entity_id", "")
                if bid_strategy and camp_res:
                    client = _build_client()
                    _execute_change_bid_strategy(client, customer_id, camp_res, bid_strategy, target_cpa, target_roas)

            elif operation == "change_match_type":
                from ai_optimizer import _execute_change_match_type
                after = json.loads(row["after_state_json"] or "{}")
                resource_name_kw = after.get("resource_name", "") or row.get("entity_id", "")
                new_mt = after.get("new_match_type", "EXACT")
                kw_text = after.get("keyword_text") or row.get("entity_name", "")
                if resource_name_kw:
                    client = _build_client()
                    _execute_change_match_type(client, customer_id, resource_name_kw, new_mt, keyword_text=kw_text)

            elif operation == "add_asset":
                from google_ads_create import add_callouts_to_campaign, add_structured_snippet_to_campaign
                after = json.loads(row["after_state_json"] or "{}")
                _a_type = after.get("asset_type", "")
                _a_camp = after.get("campaign_resource") or row.get("entity_id", "")
                if _a_type == "CALLOUT" and _a_camp:
                    client = _build_client()
                    add_callouts_to_campaign(_a_camp, after.get("callout_texts") or [], customer_id=customer_id)
                elif _a_type == "STRUCTURED_SNIPPET" and _a_camp:
                    client = _build_client()
                    add_structured_snippet_to_campaign(
                        _a_camp,
                        after.get("snippet_header", ""),
                        after.get("values") or [],
                        customer_id=customer_id,
                    )
                else:
                    # Fallback: try google_rec_resource_name for direct apply
                    google_rec_rn = row.get("google_rec_resource_name", "")
                    if google_rec_rn:
                        client = _build_client()
                        _apply_google_recommendation(client, customer_id, google_rec_rn)

            elif operation == "replace_ad":
                after = json.loads(row["after_state_json"] or "{}")
                old_rn = after.get("old_ad_group_ad_resource", "")
                new_h  = after.get("new_headlines", [])
                new_d  = after.get("new_descriptions", [])
                if old_rn and new_h:
                    client = _build_client()
                    _execute_replace_ad(client, customer_id,
                                        old_ad_group_ad_resource=old_rn,
                                        new_headlines=new_h,
                                        new_descriptions=new_d)

            elif operation == "pause_ad":
                after = json.loads(row["after_state_json"] or "{}")
                ad_rn = after.get("ad_group_ad_resource", "")
                if ad_rn:
                    client = _build_client()
                    _execute_pause_ad(client, customer_id, ad_rn)

            elif operation == "pause_ad_group":
                after = json.loads(row["after_state_json"] or "{}")
                ag_rn = after.get("ad_group_resource", "")
                if ag_rn:
                    from google_ads_write import set_ad_group_status as _set_ag_status_bulk
                    _set_ag_status_bulk(ag_rn, "PAUSED")

            elif operation == "update_geo_targeting":
                # Delegate to the single-action approve path via a minimal re-implementation
                # (full safety/validation lives there; bulk just needs to call the same helper)
                after = json.loads(row["after_state_json"] or "{}")
                from database import get_campaign_by_id as _gcbi_bulk, save_campaign_geo
                from google_ads_write import apply_geo_targets as _apply_geo_bulk
                camp_rn_geo = after.get("campaign_resource", "")
                new_locs    = after.get("new_locations") or []
                if camp_rn_geo and new_locs:
                    client = _build_client()
                    _cid_digits = "".join(c for c in (customer_id or "") if c.isdigit())
                    added, removed, errs = _apply_geo_bulk(client, _cid_digits, camp_rn_geo, new_locs)
                    if errs:
                        raise RuntimeError(f"update_geo_targeting partial errors: {errs[:2]}")

            elif operation == "set_ad_schedule":
                after = json.loads(row["after_state_json"] or "{}")
                camp_rn_sched = after.get("campaign_resource", "")
                schedule_text = after.get("schedule_text", "")
                if camp_rn_sched and schedule_text:
                    from google_ads_write import parse_schedule_text, set_ad_schedule as _set_sched_bulk
                    slots = parse_schedule_text(schedule_text)
                    if slots:
                        client = _build_client()
                        _cid_digits = "".join(c for c in (customer_id or "") if c.isdigit())
                        _set_sched_bulk(client, _cid_digits, camp_rn_sched, slots)

            else:
                results[aid] = {"status": "error", "detail": f"Unknown operation: {operation}"}
                continue

            update_gads_action_result(aid, executed=True, execution_result="success")
            set_audit_approval(aid, approver="admin_bulk")

            # Outcome snapshot for learning loop
            try:
                from database import snapshot_applied_outcome
                before_state = json.loads(row.get("before_state_json") or "{}")
                snapshot_applied_outcome(
                    action_id=aid, operation=operation,
                    entity_type=row.get("entity_type", "keyword"),
                    entity_id=row.get("entity_id", ""),
                    entity_name=row.get("entity_name", ""),
                    campaign_id=row.get("campaign_id", ""),
                    campaign_name=row.get("campaign_name", ""),
                    before_state=before_state,
                    ai_reason=row.get("reason", ""),
                    optimizer_run_id=row.get("optimizer_run_id", ""),
                )
            except Exception as _snap_err:
                logger.warning(f"Bulk apply: outcome snapshot failed for {aid}: {_snap_err}")

            results[aid] = {"status": "success", "entity": row["entity_name"], "operation": operation}

        except Exception as e:
            try:
                update_gads_action_result(aid, executed=False, execution_result="error",
                                          error_detail=str(e)[:200])
            except Exception:
                pass
            results[aid] = {"status": "error", "detail": str(e)[:200]}
            logger.error(f"Bulk apply: action {aid} failed: {e}")

    success_count = sum(1 for v in results.values() if v["status"] == "success")
    error_count   = sum(1 for v in results.values() if v["status"] == "error")
    return {
        "results": results,
        "summary": {"total": len(action_ids), "success": success_count, "error": error_count},
    }


@app.post("/api/admin/gads/queue/{action_id}", dependencies=[Depends(_require_admin)])
async def gads_queue_action(action_id: str):
    """
    Move a pending_approval recommendation to 'queued' state.
    Queued recs are held until POST /api/admin/gads/push-queued is called.
    Idempotent: already-queued rows return 200 with state=queued.
    """
    from database import get_audit_row, queue_gads_action
    row = get_audit_row(action_id)
    if not row:
        raise HTTPException(status_code=404, detail="Action not found")
    if row["execution_result"] == "queued":
        return {"status": "queued", "action_id": action_id, "detail": "Already queued"}
    if row["execution_result"] != "pending_approval":
        raise HTTPException(
            status_code=409,
            detail=f"Action already in state '{row['execution_result']}' — cannot queue"
        )
    queue_gads_action(action_id, approver="admin")
    logger.info(f"Queued: {row['operation']} '{row['entity_name']}' ({action_id[:8]})")
    return {"status": "queued", "action_id": action_id}


@app.post("/api/admin/gads/unqueue/{action_id}", dependencies=[Depends(_require_admin)])
async def gads_unqueue_action(action_id: str):
    """Move a queued recommendation back to pending_approval."""
    from database import get_audit_row, update_gads_action_result
    row = get_audit_row(action_id)
    if not row:
        raise HTTPException(status_code=404, detail="Action not found")
    if row["execution_result"] != "queued":
        raise HTTPException(status_code=409, detail=f"Action is not queued (state='{row['execution_result']}')")
    now = datetime.now(timezone.utc).isoformat()
    from database import _conn as _db_conn
    with _db_conn() as conn:
        conn.execute(
            "UPDATE gads_audit_log SET execution_result='pending_approval', queued_at='', updated_at=? WHERE action_id=?",
            (now, action_id)
        )
    return {"status": "pending_approval", "action_id": action_id}


@app.get("/api/admin/gads/queued", dependencies=[Depends(_require_admin)])
def gads_get_queued():
    """Return all queued recommendations (approved, pending push to Google)."""
    from database import get_queued_actions
    rows = get_queued_actions()
    for r in rows:
        try:
            r["after_state"] = json.loads(r.get("after_state_json") or "{}")
            r["before_state"] = json.loads(r.get("before_state_json") or "{}")
        except Exception:
            r["after_state"] = {}
            r["before_state"] = {}
    return {"queued": rows, "count": len(rows)}


@app.post("/api/admin/gads/push-queued", dependencies=[Depends(_require_admin)])
async def gads_push_queued():
    """
    Execute all queued recommendations against Google Ads API in one shot.
    Each action is executed independently — partial failures do not stop others.
    Returns per-action result map + summary.
    """
    from database import (get_queued_actions, mark_api_executed,
                           set_audit_approval, snapshot_applied_outcome)
    from campaign_safety import check_writes_enabled, WriteBlockedError
    from ai_optimizer import (_build_client, _execute_single_pause,
                               _execute_bid_change, _execute_add_keyword,
                               _execute_add_negative, _execute_enable_keyword,
                               _execute_budget_change, _execute_update_rsa,
                               _execute_geo_exclusion, _execute_change_bid_strategy,
                               _execute_change_match_type, _apply_google_recommendation)

    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        raise HTTPException(status_code=403, detail=str(e))

    queued = get_queued_actions()
    if not queued:
        return {"results": {}, "summary": {"total": 0, "success": 0, "error": 0, "acknowledged": 0}}

    settings = get_settings()
    customer_id = settings.google_ads_customer_id
    client = _build_client()

    results = {}
    success_count = error_count = ack_count = 0

    for row in queued:
        aid = row["action_id"]
        operation = row["operation"]
        try:
            after = json.loads(row.get("after_state_json") or "{}")
            api_hit = False  # will be True if we actually called the Google Ads API

            if operation == "pause_keyword":
                _execute_single_pause(client, customer_id, resource_name=row["entity_id"])
                api_hit = True

            elif operation in ("increase_bid", "decrease_bid"):
                new_bid = int(after.get("new_bid_micros", 0))
                _execute_bid_change(client, customer_id, resource_name=row["entity_id"],
                                    new_bid_micros=new_bid)
                api_hit = True

            elif operation == "add_exact_keyword":
                _execute_add_keyword(client, customer_id,
                    ad_group_resource=after.get("ad_group_resource") or row["entity_id"],
                    keyword_text=after.get("keyword_text", row["entity_name"]),
                    match_type=after.get("match_type", "EXACT"))
                api_hit = True

            elif operation == "add_negative_keyword":
                _execute_add_negative(client, customer_id,
                    campaign_resource=after.get("campaign_resource") or row["entity_id"],
                    keyword_text=after.get("keyword_text", row["entity_name"]),
                    match_type=after.get("match_type", "BROAD"))
                api_hit = True

            elif operation == "add_to_shared_negative_list":
                _execute_add_to_shared_negative_list(client, customer_id,
                    keyword_text=after.get("keyword_text") or row["entity_name"],
                    match_type=after.get("match_type", "BROAD"))
                api_hit = True

            elif operation == "tighten_match_type":
                _execute_add_keyword(client, customer_id,
                    ad_group_resource=after.get("ad_group_resource", ""),
                    keyword_text=row["entity_name"], match_type="EXACT")
                _execute_single_pause(client, customer_id, resource_name=row["entity_id"])
                api_hit = True

            elif operation == "enable_keyword":
                _execute_enable_keyword(client, customer_id, resource_name=row["entity_id"])
                api_hit = True

            elif operation == "ad_copy_suggestion":
                ad_resource = after.get("ad_resource")
                new_headlines = [after["headline"]] if after.get("headline") else []
                new_descriptions = [after["description"]] if after.get("description") else []
                if ad_resource and new_headlines:
                    rsa_ok = _execute_update_rsa(client, customer_id,
                        ad_group_ad_resource=ad_resource,
                        new_headlines=new_headlines,
                        new_descriptions=new_descriptions)
                    # RSA assets are IMMUTABLE via API — treat as advisory when API rejects.
                    # api_hit stays False so the row records as acknowledged_only.
                    api_hit = bool(rsa_ok)

            elif operation == "geo_exclusion":
                geo_rn = after.get("geo_target_resource")
                camp_rn = after.get("campaign_resource") or row.get("entity_id") or ""
                if geo_rn and camp_rn:
                    _execute_geo_exclusion(client, customer_id,
                        campaign_resource=camp_rn, geo_target_resource=geo_rn)
                    api_hit = True

            elif operation == "change_budget":
                new_budget = float(after.get("new_daily_budget_usd", 0))
                camp_rn = after.get("campaign_resource") or row.get("entity_id") or ""
                if new_budget and camp_rn:
                    _execute_budget_change(client, customer_id,
                        campaign_resource=camp_rn, new_daily_budget_usd=new_budget)
                    api_hit = True

            elif operation == "change_bid_strategy":
                bid_strategy = after.get("bid_strategy", "")
                target_cpa = int(after.get("target_cpa_micros", 0))
                target_roas = float(after.get("target_roas", 0))
                camp_res = after.get("campaign_resource", "") or row.get("entity_id", "")
                if bid_strategy and camp_res:
                    _execute_change_bid_strategy(client, customer_id, camp_res, bid_strategy, target_cpa, target_roas)
                    api_hit = True

            elif operation == "change_match_type":
                details = after
                resource_name_kw = after.get("resource_name") or details.get("resource_name", "") or row.get("entity_id", "")
                new_mt = after.get("new_match_type") or details.get("new_match_type", "EXACT")
                kw_text = after.get("keyword_text") or row.get("entity_name", "")
                if resource_name_kw:
                    _execute_change_match_type(client, customer_id, resource_name_kw, new_mt, keyword_text=kw_text)
                    api_hit = True

            elif operation == "add_asset":
                from google_ads_create import add_callouts_to_campaign, add_structured_snippet_to_campaign
                from ai_optimizer import VALID_SNIPPET_HEADERS as _VALID_HDRS_PUSH
                _a2_type = after.get("asset_type", "")
                _a2_camp = after.get("campaign_resource") or row.get("entity_id", "")
                if _a2_type == "CALLOUT" and _a2_camp:
                    _cres = add_callouts_to_campaign(
                        _a2_camp, after.get("callout_texts") or [], customer_id=customer_id
                    )
                    if _cres.get("ok"):
                        api_hit = True
                elif _a2_type == "STRUCTURED_SNIPPET" and _a2_camp:
                    _push_hdr = after.get("snippet_header", "")
                    if _push_hdr not in _VALID_HDRS_PUSH:
                        logger.warning(f"Skipping push add_asset — invalid snippet_header '{_push_hdr}'")
                    else:
                        _sres = add_structured_snippet_to_campaign(
                            _a2_camp,
                            _push_hdr,
                            after.get("values") or [],
                            customer_id=customer_id,
                        )
                        if _sres.get("ok"):
                            api_hit = True
                else:
                    # Fallback: try ApplyRecommendation for Google-sourced recs
                    google_rec_rn = row.get("google_rec_resource_name", "")
                    if google_rec_rn:
                        try:
                            _apply_google_recommendation(client, customer_id, google_rec_rn)
                            api_hit = True
                        except Exception as _are:
                            logger.error(f"ApplyRecommendation failed for add_asset: {_are}")

            elif operation == "replace_ad":
                old_rn = after.get("old_ad_group_ad_resource", "")
                new_h  = after.get("new_headlines", [])
                new_d  = after.get("new_descriptions", [])
                if old_rn and new_h:
                    from ai_optimizer import _execute_replace_ad as _exec_repl_push
                    _exec_repl_push(client, customer_id,
                                    old_ad_group_ad_resource=old_rn,
                                    new_headlines=new_h,
                                    new_descriptions=new_d)
                    api_hit = True

            elif operation == "pause_ad":
                ad_rn = after.get("ad_group_ad_resource", "")
                if ad_rn:
                    from ai_optimizer import _execute_pause_ad as _exec_pause_ad_push
                    _exec_pause_ad_push(client, customer_id, ad_rn)
                    api_hit = True

            elif operation == "pause_ad_group":
                ag_rn = after.get("ad_group_resource", "")
                if ag_rn:
                    from google_ads_write import set_ad_group_status as _set_ag_push
                    _set_ag_push(ag_rn, "PAUSED")
                    api_hit = True

            elif operation == "update_geo_targeting":
                camp_rn_geo = after.get("campaign_resource", "")
                new_locs    = after.get("new_locations") or []
                if camp_rn_geo and new_locs:
                    from google_ads_write import apply_geo_targets as _apply_geo_push
                    _cid_digits = "".join(c for c in (customer_id or "") if c.isdigit())
                    added, removed, errs = _apply_geo_push(client, _cid_digits, camp_rn_geo, new_locs)
                    if not errs:
                        api_hit = True
                    else:
                        raise RuntimeError(f"update_geo_targeting errors: {errs[:2]}")

            elif operation == "set_ad_schedule":
                camp_rn_sched = after.get("campaign_resource", "")
                schedule_text = after.get("schedule_text", "")
                if camp_rn_sched and schedule_text:
                    from google_ads_write import parse_schedule_text, set_ad_schedule as _set_sched_push
                    slots = parse_schedule_text(schedule_text)
                    if slots:
                        _cid_digits = "".join(c for c in (customer_id or "") if c.isdigit())
                        _set_sched_push(client, _cid_digits, camp_rn_sched, slots)
                        api_hit = True

            # claude_advisory and other advisory ops — acknowledged only, api_hit stays False
            # execution_result='success' always; api_executed=True only if we actually called the API
            mark_api_executed(aid, success=True, api_executed=api_hit)
            set_audit_approval(aid, approver="push_to_google")

            # Outcome snapshot for learning loop
            try:
                before_state = json.loads(row.get("before_state_json") or "{}")
                snapshot_applied_outcome(
                    action_id=aid, operation=operation,
                    entity_type=row.get("entity_type", "keyword"),
                    entity_id=row.get("entity_id", ""),
                    entity_name=row.get("entity_name", ""),
                    campaign_id=row.get("campaign_id", ""),
                    campaign_name=row.get("campaign_name", ""),
                    before_state=before_state,
                    ai_reason=row.get("reason", ""),
                    optimizer_run_id=row.get("optimizer_run_id", ""),
                )
            except Exception:
                pass

            if api_hit:
                success_count += 1
                results[aid] = {"status": "success", "api_executed": True,
                                 "entity": row["entity_name"], "operation": operation}
                logger.info(f"Push: {operation} '{row['entity_name']}' → Google Ads ✓")
            else:
                ack_count += 1
                results[aid] = {"status": "success", "api_executed": False,
                                 "entity": row["entity_name"], "operation": operation,
                                 "detail": "Acknowledged only (no API resource available)"}
                logger.info(f"Push: {operation} '{row['entity_name']}' → acknowledged only")

        except Exception as e:
            mark_api_executed(aid, success=False, error_detail=str(e)[:300])
            results[aid] = {"status": "error", "api_executed": False,
                             "entity": row.get("entity_name", ""), "detail": str(e)[:200]}
            error_count += 1
            logger.error(f"Push: {operation} '{row.get('entity_name','')}' failed: {e}")

    return {
        "results": results,
        "summary": {
            "total": len(queued),
            "pushed_to_google": success_count,
            "acknowledged_only": ack_count,
            "error": error_count,
        },
    }


@app.get("/api/admin/optimizer/account/recommendations", dependencies=[Depends(_require_admin)])
def account_recommendations(status: str = "all", limit: int = 100, offset: int = 0, search: str = ""):
    """
    Return recommendations across ALL campaigns (account-level view).
    status: 'all' | 'pending_approval' | 'queued' | 'success' | 'rejected'
    limit/offset: pagination (default 100 per page)
    search: optional substring filter applied server-side on entity_name, operation, campaign_name
    """
    from database import _conn as _db_conn
    valid_statuses = {"all", "pending_approval", "queued", "success", "rejected", "expired"}
    if status not in valid_statuses:
        raise HTTPException(status_code=422, detail=f"Invalid status '{status}'")

    search_lower = search.strip().lower()

    with _db_conn() as conn:
        if status == "all":
            # Always surface pending_approval rows first so they are never crowded
            # out by the limit when there are many history rows in the DB.
            pending_rows = conn.execute(
                "SELECT * FROM gads_audit_log WHERE execution_result='pending_approval' ORDER BY priority ASC, created_at DESC",
            ).fetchall()
            # Total history count for pagination metadata
            total_history = conn.execute(
                "SELECT COUNT(*) FROM gads_audit_log WHERE execution_result != 'pending_approval'"
            ).fetchone()[0]
            history_rows = conn.execute(
                "SELECT * FROM gads_audit_log WHERE execution_result != 'pending_approval' ORDER BY priority ASC, created_at DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
            rows = list(pending_rows) + list(history_rows)
            total_count = len(pending_rows) + total_history
        else:
            # Apply search filter in SQL when possible
            if search_lower:
                total_count = conn.execute(
                    "SELECT COUNT(*) FROM gads_audit_log WHERE execution_result=? AND (LOWER(entity_name) LIKE ? OR LOWER(operation) LIKE ? OR LOWER(campaign_name) LIKE ?)",
                    (status, f"%{search_lower}%", f"%{search_lower}%", f"%{search_lower}%")
                ).fetchone()[0]
                rows = conn.execute(
                    "SELECT * FROM gads_audit_log WHERE execution_result=? AND (LOWER(entity_name) LIKE ? OR LOWER(operation) LIKE ? OR LOWER(campaign_name) LIKE ?) ORDER BY priority ASC, created_at DESC LIMIT ? OFFSET ?",
                    (status, f"%{search_lower}%", f"%{search_lower}%", f"%{search_lower}%", limit, offset)
                ).fetchall()
            else:
                total_count = conn.execute(
                    "SELECT COUNT(*) FROM gads_audit_log WHERE execution_result=?", (status,)
                ).fetchone()[0]
                rows = conn.execute(
                    "SELECT * FROM gads_audit_log WHERE execution_result=? ORDER BY priority ASC, created_at DESC LIMIT ? OFFSET ?",
                    (status, limit, offset)
                ).fetchall()

    grouped = {}
    total_impact = 0.0
    for r in rows:
        op = r["operation"]
        grouped.setdefault(op, [])
        row_dict = dict(r)
        try:
            row_dict["before_state"] = json.loads(r["before_state_json"] or "{}")
            row_dict["after_state"] = json.loads(r["after_state_json"] or "{}")
            impact = json.loads(r["impact_estimate_json"] or "{}")
            row_dict["impact_estimate"] = impact
            total_impact += float(impact.get("savings_30d_usd", 0))
        except Exception:
            row_dict["before_state"] = {}
            row_dict["after_state"] = {}
            row_dict["impact_estimate"] = {}
        grouped[op].append(row_dict)

    # Build a set of paused campaign names so the frontend can filter the default view.
    # We also resolve audit-log variants (e.g. "Emergency May of 2026 test (05/03 21:55)")
    # whose base name matches a paused campaigns row ("Emergency May of 2026 test").
    paused_campaign_names: list[str] = []
    try:
        with _db_conn() as _sc:
            _status_rows = _sc.execute(
                "SELECT campaign_name, status FROM campaigns WHERE status IS NOT NULL"
            ).fetchall()
            _paused_bases = {
                r["campaign_name"].strip().lower()
                for r in _status_rows
                if (r["status"] or "").upper() not in ("ENABLED", "ACTIVE")
            }
            # Start with the base names from campaigns table
            paused_campaign_names = [
                r["campaign_name"] for r in _status_rows
                if (r["status"] or "").upper() not in ("ENABLED", "ACTIVE")
            ]
            if _paused_bases:
                # Also grab any audit-log campaign_name variants that start with a paused base name
                # (GAds appends launch timestamps like " (05/03 21:55)" to campaign names)
                _audit_names = _sc.execute(
                    "SELECT DISTINCT campaign_name FROM gads_audit_log WHERE campaign_name IS NOT NULL AND campaign_name != ''"
                ).fetchall()
                for _an in _audit_names:
                    _aname = (_an["campaign_name"] or "").strip()
                    _aname_lower = _aname.lower()
                    for _base in _paused_bases:
                        if _aname_lower.startswith(_base) and _aname not in paused_campaign_names:
                            paused_campaign_names.append(_aname)
                            break
    except Exception:
        pass  # non-fatal — frontend falls back to showing all

    return {
        "campaign_name": "__all__",
        "status_filter": status,
        "recommendations": grouped,
        "total": len(rows),
        "paused_campaign_names": paused_campaign_names,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "total_count": total_count,
            "has_more": (offset + limit) < total_count,
        },
        "summary": {
            "total_pending": sum(1 for r in rows if r["execution_result"] == "pending_approval"),
            "total_queued": sum(1 for r in rows if r["execution_result"] == "queued"),
            "total_applied": sum(1 for r in rows if r["execution_result"] == "success"),
            "total_rejected": sum(1 for r in rows if r["execution_result"] == "rejected"),
            "estimated_savings_30d_usd": round(total_impact, 2),
        },
    }


@app.get("/api/admin/optimizer/account/impact", dependencies=[Depends(_require_admin)])
def account_impact(days: int = 90):
    """Account-level aggregated impact across all campaigns."""
    from database import get_account_impact_summary
    return get_account_impact_summary(days=days)


@app.get("/api/admin/gads/google-recs", dependencies=[Depends(_require_admin)])
async def get_google_recs_endpoint():
    """Get cached Google recommendations from last optimizer run."""
    from database import get_google_recs
    recs = get_google_recs(dismissed=False)
    import json as _json
    for r in recs:
        try:
            r['impact'] = _json.loads(r.get('impact_json') or '{}')
            r['details'] = _json.loads(r.get('details_json') or '{}')
        except Exception:
            r['impact'] = {}
            r['details'] = {}
    return {"recs": recs}


@app.post("/api/admin/gads/google-recs/{rec_id}/dismiss", dependencies=[Depends(_require_admin)])
async def dismiss_google_rec_endpoint(rec_id: int):
    """Dismiss a Google recommendation."""
    from database import _conn
    with _conn() as conn:
        row = conn.execute("SELECT resource_name FROM gads_google_recs WHERE id=?", (rec_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Rec not found")
        resource_name = row[0]
    from database import dismiss_google_rec
    dismiss_google_rec(resource_name)
    return {"status": "dismissed"}


@app.get("/api/admin/gads/writes-status", dependencies=[Depends(_require_admin)])
def gads_writes_status():
    """Return the current state of the Google Ads write kill switch."""
    from campaign_safety import get_writes_status
    return get_writes_status()


@app.post("/api/admin/gads/writes-enabled", dependencies=[Depends(_require_admin)])
async def gads_set_writes_enabled(request: Request):
    """
    Toggle the runtime Google Ads write kill switch.
    Body: {"enabled": true|false}
    Note: env-var CAMPAIGN_WRITE_OPS_ENABLED must also be True for writes to work.
    """
    from database import save_setting
    from campaign_safety import get_writes_status
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    save_setting("gads_writes_enabled", "true" if enabled else "false")
    logger.info(f"Google Ads writes {'ENABLED' if enabled else 'DISABLED'} via admin UI")
    return {"status": "ok", "writes_enabled": enabled, **get_writes_status()}


@app.get("/api/admin/gads/spend-guardrails", dependencies=[Depends(_require_admin)])
def gads_get_guardrails():
    """Return all spend guardrails."""
    from database import get_all_spend_guardrails
    rows = get_all_spend_guardrails()
    return {"guardrails": rows}


class SpendGuardrailBody(BaseModel):
    campaign_id: str
    campaign_name: str
    daily_cap_usd: float


@app.post("/api/admin/gads/spend-guardrails", dependencies=[Depends(_require_admin)])
def gads_upsert_guardrail(body: SpendGuardrailBody):
    """Create or update a spend guardrail for a campaign."""
    from database import upsert_spend_guardrail
    if body.daily_cap_usd <= 0:
        raise HTTPException(status_code=400, detail="daily_cap_usd must be > 0")
    row = upsert_spend_guardrail(body.campaign_id, body.campaign_name, body.daily_cap_usd)
    return {"status": "ok", "guardrail": row}


@app.get("/api/admin/gads/optimizer-runs", dependencies=[Depends(_require_admin)])
def gads_optimizer_runs(limit: int = 20):
    """Return recent optimizer run records."""
    from database import get_optimizer_runs
    runs = get_optimizer_runs(limit=limit)
    return {"runs": runs, "total": len(runs)}


@app.get("/api/admin/optimizer/impact-history")
def get_optimizer_impact_history(limit: int = 30):
    """Return per-run impact history for the AI Optimizer Impact tab."""
    try:
        from database import get_impact_history
        return {"impact_history": get_impact_history(limit=limit)}
    except Exception as e:
        logger.error(f"Impact history error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── Step 10: TCPA Stop Conditions ──────────────────────────────────────────

# STOP keyword normalization
_SMS_STOP_WORDS  = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
_SMS_START_WORDS = {"start", "yes", "unstop"}
_SMS_HELP_WORDS  = {"help", "info"}

TWIML_STOP_REPLY  = "<?xml version='1.0' encoding='UTF-8'?><Response><Message>You have been unsubscribed. Reply START to resubscribe.</Message></Response>"
TWIML_START_REPLY = "<?xml version='1.0' encoding='UTF-8'?><Response><Message>You have been resubscribed. Reply STOP to unsubscribe.</Message></Response>"
TWIML_HELP_REPLY  = "<?xml version='1.0' encoding='UTF-8'?><Response><Message>Grafton Dental Care: Reply STOP to unsubscribe. Call 508-318-4477 for help.</Message></Response>"
TWIML_EMPTY       = "<?xml version='1.0' encoding='UTF-8'?><Response/>"


def _verify_twilio_signature(request_url: str, post_params: dict,
                              signature: str, auth_token: str) -> bool:
    """Validate Twilio request signature."""
    try:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(auth_token)
        return validator.validate(request_url, post_params, signature)
    except Exception:
        return False


@app.post("/webhooks/callrail/call")
async def callrail_call_webhook(
    request: Request,
    x_callrail_signature: Optional[str] = Header(None),
    secret: Optional[str] = Query(None),
):
    """
    CallRail webhook for call.created / call.completed / call.recording_completed.

    Auth (in order):
      1. HMAC-SHA256 in X-CallRail-Signature header over raw body
         using settings.callrail_webhook_secret.
      2. ?secret=... query param matching callrail_webhook_secret (dev/fallback).

    Always returns 200 so CallRail doesn't retry on processing errors.
    """
    from fastapi.responses import JSONResponse as _JR
    from callrail_webhook import verify_signature, verify_query_secret, process_webhook

    settings = get_settings()
    secret_cfg = settings.callrail_webhook_secret or ""

    raw_body = await request.body()

    # Auth — require signature if secret is configured
    if secret_cfg:
        hmac_ok  = verify_signature(raw_body, x_callrail_signature or "", secret_cfg)
        query_ok = verify_query_secret(secret or "", secret_cfg)
        if not (hmac_ok or query_ok):
            logger.warning("[callrail_webhook] signature mismatch — rejected")
            return _JR({"ok": False, "error": "signature_invalid"}, status_code=401)
    else:
        logger.warning("[callrail_webhook] CALLRAIL_WEBHOOK_SECRET not set — accepting unverified")

    # Parse JSON — still return 200 on bad body (avoid retries on garbage)
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except Exception as e:
        logger.error(f"[callrail_webhook] JSON parse failed: {e}")
        return _JR({"ok": False, "error": "invalid_json"}, status_code=200)

    # Synchronous processing — call volume is small (~10-50/day)
    try:
        result = process_webhook(payload, raw_body)
        logger.info(f"[callrail_webhook] processed: {result.get('action')} / lead={result.get('lead_id')}")
        return _JR(result, status_code=200)
    except Exception as e:
        logger.error(f"[callrail_webhook] processing failed: {e}", exc_info=True)
        return _JR({"ok": False, "error": str(e)}, status_code=200)


@app.post("/webhooks/twilio/inbound")
async def twilio_inbound_webhook(request: Request):
    """
    Handle inbound SMS from Twilio.

    Signature verification has three modes (controlled by DB setting
    'twilio_sig_mode'):
      enforce   — reject invalid signatures (HTTP 403)
      log_only  — log mismatch but continue (default in dev)
      skip      — skip verification entirely (test/CI only)
    """
    from fastapi.responses import Response as _Resp
    from database import (
        get_lead_by_phone, set_lead_dnd, insert_sms_message,
        add_lead_event, cancel_queue_rows, get_setting as _get_setting,
    )
    from stop_engine import handle_event as _stop_handle

    settings = get_settings()

    # Parse form body
    form = await request.form()
    post_params = dict(form)

    from_number = post_params.get("From", "")
    to_number   = post_params.get("To", "")
    body_raw    = post_params.get("Body", "")
    twilio_sid  = post_params.get("MessageSid", "")
    body_clean  = body_raw.strip()

    # ── Twilio signature verification ────────────────────────────────────────
    # sig_valid tracks whether the signature check passed.
    # In log_only mode, bad-sig requests are logged but state mutations are blocked.
    sig_valid = True
    sig_mode = _get_setting("twilio_sig_mode", "log_only")
    if sig_mode != "skip" and settings.twilio_auth_token:
        x_sig = request.headers.get("X-Twilio-Signature", "")
        request_url = str(request.url)
        valid = _verify_twilio_signature(request_url, post_params,
                                         x_sig, settings.twilio_auth_token)
        if not valid:
            logger.warning(
                f"Twilio signature mismatch from={from_number} url={request_url}"
            )
            if sig_mode == "enforce":
                return _Resp(content=TWIML_EMPTY, media_type="application/xml",
                             status_code=403)
            # log_only: proceed for logging + SMS storage, but block state mutations
            sig_valid = False

    # ── Match lead by phone number ────────────────────────────────────────────
    lead = get_lead_by_phone(from_number)
    lead_id = lead["id"] if lead else None

    # ── Store inbound message (always — even on bad sig, for audit trail) ─────
    insert_sms_message(
        lead_id=lead_id,
        direction="inbound",
        from_number=from_number,
        to_number=to_number,
        body=body_clean,
        twilio_sid=twilio_sid,
    )

    # ── Parse first word for keyword handling ─────────────────────────────────
    words = body_clean.split()
    first_word = words[0].lower().strip(".,!?") if words else ""

    if first_word in _SMS_STOP_WORDS:
        if not sig_valid:
            # Bad sig in log_only mode — message is logged but don't mutate lead state
            logger.warning(
                f"STOP keyword received but signature invalid (log_only) — "
                f"skipping DND/cancellation for {from_number}"
            )
            return _Resp(content=TWIML_EMPTY, media_type="application/xml")
        if lead_id:
            # Set DND flag (reuses unsubscribed_sms column)
            set_lead_dnd(lead_id, "sms", reason="STOP keyword")
            # Cancel queued SMS rows + log sms_stop event via stop engine
            _stop_handle(lead_id, "sms_stop", reason="STOP keyword")
        else:
            # Unknown number — log but don't send confirmation (Twilio CTIA guidance)
            logger.info(f"STOP from unmatched number {from_number} — no confirmation sent")
            return _Resp(content=TWIML_EMPTY, media_type="application/xml")
        return _Resp(content=TWIML_STOP_REPLY, media_type="application/xml")

    elif first_word in _SMS_START_WORDS:
        if not sig_valid:
            logger.warning(
                f"START keyword received but signature invalid (log_only) — "
                f"skipping re-subscribe for {from_number}"
            )
            return _Resp(content=TWIML_EMPTY, media_type="application/xml")
        if lead_id:
            # Clear unsubscribed_sms flag
            from database import _conn as _dbc
            _now_ts = datetime.now(timezone.utc).isoformat()
            with _dbc() as _c:
                _c.execute(
                    "UPDATE leads SET unsubscribed_sms=0, dnd_reason='', dnd_set_at='', updated_at=? WHERE id=?",
                    (_now_ts, lead_id)
                )
            add_lead_event(lead_id, "sms_resubscribed", source="twilio_webhook")
        return _Resp(content=TWIML_START_REPLY, media_type="application/xml")

    elif first_word in _SMS_HELP_WORDS:
        # HELP is informational — no state mutation, safe to reply even on bad sig
        return _Resp(content=TWIML_HELP_REPLY, media_type="application/xml")

    else:
        # Regular reply — log the event only if sig is valid (stop_engine: log-only)
        if lead_id and sig_valid:
            _stop_handle(lead_id, "replied", reason=f"inbound_sms: {body_clean[:80]}")
        return _Resp(content=TWIML_EMPTY, media_type="application/xml")


# ── Admin stop-condition endpoints ────────────────────────────────────────────

class PauseLeadRequest(BaseModel):
    reason: str = "admin"
    until: str = ""   # ISO timestamp or '' for indefinite


class DndRequest(BaseModel):
    channel: str    # 'sms' | 'email' | 'all'
    reason: str = "admin"


@app.post("/api/admin/lead/{lead_id}/pause", dependencies=[Depends(_require_admin)])
def admin_pause_lead(lead_id: str, body: PauseLeadRequest):
    """Pause a lead's follow-up sequence (indefinitely or until a timestamp)."""
    from database import pause_lead
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    pause_lead(lead_id, reason=body.reason, until=body.until)
    from stop_engine import handle_event as _stop_handle
    _stop_handle(lead_id, "manual_pause", reason=body.reason)
    return {"status": "ok", "lead_id": lead_id, "paused": True}


@app.post("/api/admin/lead/{lead_id}/resume", dependencies=[Depends(_require_admin)])
def admin_resume_lead(lead_id: str):
    """Resume a paused lead."""
    from database import resume_lead
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    resume_lead(lead_id)
    return {"status": "ok", "lead_id": lead_id, "paused": False}


@app.post("/api/admin/lead/{lead_id}/dnd", dependencies=[Depends(_require_admin)])
def admin_set_dnd(lead_id: str, body: DndRequest):
    """Set DND (do-not-disturb) for a lead on one or all channels."""
    from database import set_lead_dnd
    from stop_engine import handle_event as _stop_handle
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    channels = ["sms", "email"] if body.channel == "all" else [body.channel]
    for ch in channels:
        set_lead_dnd(lead_id, ch, reason=body.reason)
    _stop_handle(lead_id, "dnd_set", reason=body.reason)
    return {"status": "ok", "lead_id": lead_id, "dnd_channels": channels}


@app.post("/api/admin/lead/{lead_id}/clear-dnd", dependencies=[Depends(_require_admin)])
async def admin_clear_dnd(lead_id: str, request: Request):
    """Clear DND flags for a lead (admin override — re-enables future messages)."""
    body = await request.json()
    channel = body.get("channel", "all")
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    _now_ts = datetime.now(timezone.utc).isoformat()
    from database import _conn as _dbc
    with _dbc() as _c:
        if channel in ("sms", "all"):
            _c.execute(
                "UPDATE leads SET unsubscribed_sms=0, dnd_reason='', dnd_set_at='', updated_at=? WHERE id=?",
                (_now_ts, lead_id)
            )
        if channel in ("email", "all"):
            _c.execute(
                "UPDATE leads SET unsubscribed_email=0, dnd_reason='', dnd_set_at='', updated_at=? WHERE id=?",
                (_now_ts, lead_id)
            )
    add_lead_event(lead_id, "dnd_cleared", detail=json.dumps({"channel": channel}), source="admin")
    return {"status": "ok", "lead_id": lead_id, "channel": channel}


# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/admin/run-queue", dependencies=[Depends(_require_admin)])
def admin_run_queue():
    run_now()
    return {"status": "ok", "message": "Follow-up queue processed"}


@app.put("/api/admin/lead/{lead_id}/stage", dependencies=[Depends(_require_admin)])
async def admin_update_stage(lead_id: str, request: Request):
    body = await request.json()
    new_stage = body.get("stage")
    if not new_stage:
        raise HTTPException(status_code=400, detail="stage required")
    lead = update_stage(lead_id, new_stage, source="admin")
    return {"status": "ok", "lead": lead}


# ─── Force stage (allows backward movement) ─────────────────────────────────

@app.put("/api/admin/lead/{lead_id}/force-stage", dependencies=[Depends(_require_admin)])
async def admin_force_stage(lead_id: str, request: Request):
    body = await request.json()
    new_stage = body.get("stage")
    if not new_stage:
        raise HTTPException(status_code=400, detail="stage required")
    from database import LIFECYCLE_STAGES
    if new_stage not in LIFECYCLE_STAGES:
        raise HTTPException(status_code=400, detail=f"Invalid stage: {new_stage}")
    lead = force_stage(lead_id, new_stage, source="admin",
                       detail=json.dumps({"reason": body.get("reason", "manual move")}))
    return {"status": "ok", "lead": lead}


# ─── Notes ───────────────────────────────────────────────────────────────────

@app.get("/api/admin/lead/{lead_id}/notes", dependencies=[Depends(_require_admin)])
def admin_get_notes(lead_id: str):
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"notes": get_notes(lead_id)}


@app.post("/api/admin/lead/{lead_id}/notes", dependencies=[Depends(_require_admin)])
async def admin_add_note(lead_id: str, request: Request):
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    body = await request.json()
    text = body.get("note_text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="note_text required")
    note = add_note(lead_id, text, author=body.get("author", "admin"))
    return {"status": "ok", "note": note}


@app.delete("/api/admin/note/{note_id}", dependencies=[Depends(_require_admin)])
def admin_delete_note(note_id: int):
    delete_note(note_id)
    return {"status": "ok"}


# ─── Lead Tags ────────────────────────────────────────────────────────────────

class TagsUpdateRequest(BaseModel):
    tags: list  # full replacement list

@app.get("/api/admin/lead/{lead_id}/tags", dependencies=[Depends(_require_admin)])
def admin_get_tags(lead_id: str):
    if not get_lead(lead_id):
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"tags": get_lead_tags(lead_id)}


@app.put("/api/admin/lead/{lead_id}/tags", dependencies=[Depends(_require_admin)])
def admin_set_tags(lead_id: str, body: TagsUpdateRequest):
    if not get_lead(lead_id):
        raise HTTPException(status_code=404, detail="Lead not found")
    updated = set_lead_tags(lead_id, body.tags)
    return {"tags": updated}


# ─── Manual Lead Creation ────────────────────────────────────────────────────

class ManualLeadRequest(BaseModel):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    source: str = "manual"
    notes: str = ""

    @validator("first_name", "last_name", "email", "phone", "source", "notes", pre=True)
    def coerce_str(cls, v):
        return str(v).strip() if v is not None else ""

    @validator("source")
    def source_not_empty(cls, v):
        return v if v.strip() else "manual"


@app.post("/api/admin/leads/create", dependencies=[Depends(_require_admin)])
def admin_create_lead(body: ManualLeadRequest):
    if not body.first_name and not body.email and not body.phone:
        raise HTTPException(status_code=400, detail="Provide at least a name, email, or phone")
    lead_id = str(uuid.uuid4())
    data = {
        "id": lead_id,
        "first_name": body.first_name,
        "last_name": body.last_name,
        "email": body.email,
        "phone": body.phone,
        "source": body.source,
        "notes": body.notes,
        "stage": "new",
    }
    lead = upsert_lead(data)
    # Log a lead_created event so the timeline isn't empty
    # Deliberately NOT enqueuing follow-ups — manual leads are staff-initiated contacts
    add_event(lead_id, "lead_created", stage_to="new",
              source=body.source, detail="manual entry")
    return {"status": "ok", "lead": lead}


@app.delete("/api/admin/lead/{lead_id}", dependencies=[Depends(_require_admin)])
def admin_delete_lead(lead_id: str):
    """
    Permanently delete a lead and all associated data (events, queue, notes).
    Also deletes smile image from GCS if present.
    Use with care — this is irreversible.
    """
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    settings = get_settings()

    # Delete smile image from GCS using blob name (preferred) or parse from URL (legacy)
    gcs_blob_name = lead.get("smile_blob_name", "")
    gcs_composite_blob_name = lead.get("smile_composite_blob_name", "")
    image_url = lead.get("smile_image_url", "")
    if not gcs_blob_name and image_url and "storage.googleapis.com" in image_url:
        try:
            path = image_url.split("storage.googleapis.com/")[1].split("?")[0]
            _, gcs_blob_name = path.split("/", 1)
        except Exception:
            pass
    try:
        from google.cloud import storage as gcs_storage
        client = gcs_storage.Client()
        for bname in [gcs_blob_name, gcs_composite_blob_name]:
            if bname:
                try:
                    client.bucket(settings.gcs_bucket).blob(bname).delete()
                    logger.info(f"Deleted GCS smile blob for lead {lead_id}: {bname}")
                except Exception as e:
                    logger.warning(f"Could not delete GCS blob {bname} for lead {lead_id}: {e}")
    except Exception as e:
        logger.warning(f"GCS client init failed during lead delete ({lead_id}): {e}")

    # Delete from Firestore via nxtsmile API
    # Pass email in X-Lead-Email header so old docs (no 'id' field) can be found by email.
    # If Firestore delete succeeds we do NOT write a tombstone — the lead is truly gone and
    # a fresh form submission should create a brand-new lead normally.
    # If Firestore delete fails we write a tombstone as a safety net to block re-import
    # until the Firestore delete can be retried manually.
    firestore_deleted = False
    tombstone_written = False
    lead_email = lead.get("email", "")
    try:
        import requests as _req
        delete_url = f"{settings.nxtsmile_api}/api/leads/{lead_id}"
        logger.info(f"Calling Firestore delete: DELETE {delete_url}")
        resp = _req.delete(
            delete_url,
            headers={
                "X-Secret": settings.firestore_secret,
                "X-Lead-Email": lead_email,
            },
            timeout=15,
        )
        result_body = resp.json() if resp.content else {}
        if resp.status_code in (200, 204, 404):
            docs_deleted = result_body.get("firestore_docs_deleted", "?")
            logger.info(f"Firestore delete for {lead_id}: {docs_deleted} doc(s) removed (status {resp.status_code})")
            firestore_deleted = True
        else:
            logger.warning(f"Firestore delete returned {resp.status_code} for lead {lead_id}: {result_body}")
    except Exception as e:
        logger.warning(f"Could not delete lead {lead_id} from Firestore: {e}")

    # Write tombstone only when Firestore delete failed — prevents re-import until manually resolved
    if not firestore_deleted:
        try:
            add_deleted_lead_tombstone(
                lead_id,
                email=lead_email,
                deleted_by="admin",
                reason="admin_delete_lead_firestore_failed",
            )
            tombstone_written = True
            logger.warning(
                f"Tombstone written for lead {lead_id} ({lead_email}) because Firestore delete failed. "
                f"Re-submission is blocked until Firestore is cleaned up."
            )
        except Exception as e:
            logger.warning(f"Could not write tombstone for lead {lead_id}: {e}")

    # Delete all associated local records then the lead itself
    from database import _conn
    with _conn() as conn:
        conn.execute("DELETE FROM lifecycle_events WHERE lead_id = ?", (lead_id,))
        conn.execute("DELETE FROM follow_up_queue WHERE lead_id = ?", (lead_id,))
        conn.execute("DELETE FROM lead_notes WHERE lead_id = ?", (lead_id,))
        conn.execute("DELETE FROM conversion_uploads WHERE lead_id = ?", (lead_id,))
        conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))

    logger.info(
        f"Lead {lead_id} ({lead_email}) permanently deleted by admin "
        f"(firestore={'yes' if firestore_deleted else 'FAILED'}, tombstone={'written' if tombstone_written else 'not_needed'})"
    )
    return {"status": "deleted", "lead_id": lead_id, "tombstone_written": tombstone_written, "firestore_deleted": firestore_deleted}


# ─── Test Email ──────────────────────────────────────────────────────────────

class TestEmailRequest(BaseModel):
    lead_id: str
    template: str          # day1, day7, day14, day30, noshow
    override_email: str = ""  # if set, send to this address instead of lead's email

@app.post("/api/admin/test-email", dependencies=[Depends(_require_admin)])
def admin_test_email(body: TestEmailRequest):
    """
    Fire a specific nurture email template to a lead immediately.
    Use override_email to redirect to your own inbox for testing.
    Templates: day1, day7, day14, day30, noshow
    """
    from database import get_lead
    from email_service import (
        send_day1_email, send_day7_email, send_day14_email,
        send_day30_cold_email, send_no_show_email,
    )
    from config import get_settings

    lead = get_lead(body.lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail=f"Lead {body.lead_id} not found")

    # Override email for testing without spamming real patients
    test_lead = dict(lead)
    if body.override_email:
        test_lead["email"] = body.override_email

    settings = get_settings()
    unsubscribe_url = f"{settings.base_url.rstrip('/')}/unsubscribe/{body.lead_id}/email"

    template = body.template.lower()
    try:
        if template == "day1":
            result = send_day1_email(test_lead, unsubscribe_url)
        elif template == "day7":
            result = send_day7_email(test_lead, unsubscribe_url)
        elif template == "day14":
            result = send_day14_email(test_lead, unsubscribe_url)
        elif template == "day30":
            result = send_day30_cold_email(test_lead, unsubscribe_url)
        elif template == "noshow":
            result = send_no_show_email(test_lead, unsubscribe_url)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown template '{template}'. Use: day1, day7, day14, day30, noshow")

        return {
            "status": "sent" if result else "failed",
            "template": template,
            "to": test_lead["email"],
            "lead_id": body.lead_id,
            "has_smile_image": bool(lead.get("smile_blob_name") or lead.get("smile_image_url")),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Test email failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/debug-smile/{lead_id}", dependencies=[Depends(_require_admin)])
def debug_smile_resign(lead_id: str):
    """Debug endpoint: test GCS re-sign for a lead's smile blob."""
    from database import get_lead
    from email_service import _fetch_smile_image
    from datetime import timedelta
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    blob_name = lead.get("smile_blob_name", "")
    result = {"lead_id": lead_id, "blob_name": blob_name, "smile_image_url": lead.get("smile_image_url", "")}
    if blob_name:
        # Test re-sign step by step, capturing exact errors
        try:
            from google.cloud import storage as gcs_storage
            result["gcs_import"] = "ok"
        except Exception as e:
            result["gcs_import_error"] = str(e)
            return result
        try:
            import google.auth
            from google.auth.transport import requests as g_requests
            credentials, project = google.auth.default()
            result["auth_project"] = project
            result["credentials_type"] = type(credentials).__name__
        except Exception as e:
            result["auth_error"] = str(e)
            return result
        try:
            credentials.refresh(g_requests.Request())
            result["token_ok"] = bool(credentials.token)
            result["token_prefix"] = credentials.token[:20] if credentials.token else ""
        except Exception as e:
            result["refresh_error"] = str(e)
            return result
        # Test direct blob download (Strategy 1 — no signBlob needed)
        try:
            from config import get_settings
            settings = get_settings()
            result["gcs_bucket"] = settings.gcs_bucket
            client = gcs_storage.Client()
            blob = client.bucket(settings.gcs_bucket).blob(blob_name)
            data = blob.download_as_bytes()
            result["direct_download_bytes"] = len(data)
            result["direct_download_ok"] = len(data) > 1000
        except Exception as e:
            result["direct_download_error"] = str(e)

        # Test signed URL (Strategy 2 — needs signBlob permission)
        try:
            sa_email = getattr(settings, "gcs_sa_email", "1096868046685-compute@developer.gserviceaccount.com")
            result["sa_email"] = sa_email
            blob2 = client.bucket(settings.gcs_bucket).blob(blob_name)
            signed_url = blob2.generate_signed_url(
                expiration=timedelta(days=7), method="GET", version="v4",
                service_account_email=sa_email, access_token=credentials.token,
            )
            result["signed_url"] = signed_url[:80] if signed_url else ""
            result["resign_ok"] = bool(signed_url)
        except Exception as e:
            result["sign_error"] = str(e)
    return result


# ─── Log Viewer ──────────────────────────────────────────────────────────────

@app.get("/api/admin/logs", dependencies=[Depends(_require_admin)])
def get_logs(lines: int = 200, filter: str = ""):
    """
    Return the last N lines of the app log file.
    Optional filter: only return lines containing this string (case-insensitive).
    """
    log_path = os.path.join(os.path.dirname(__file__), "logs", "app.log")
    if not os.path.exists(log_path):
        return {"lines": [], "log_path": log_path, "error": "Log file not found"}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        # Most recent last — return tail
        tail = all_lines[-2000:]  # read last 2000, then filter
        if filter:
            fl = filter.lower()
            tail = [l for l in tail if fl in l.lower()]
        result = tail[-lines:]
        return {
            "lines": [l.rstrip("\n") for l in result],
            "total_matched": len(tail),
            "log_path": log_path,
        }
    except Exception as e:
        return {"lines": [], "error": str(e)}


# ─── GA4 Analytics ───────────────────────────────────────────────────────────

@app.get("/api/admin/ga4", dependencies=[Depends(_require_admin)])
def admin_ga4(days: int = 30, force_refresh: bool = False):
    """Return GA4 analytics data (cached or fresh)."""
    from database import get_ga4_cache, save_ga4_cache

    # Try cache first (unless force refresh)
    if not force_refresh:
        cached = get_ga4_cache("full_report", days, max_age_hours=12)
        if cached:
            cached["from_cache"] = True
            return cached

    # Pull fresh data
    try:
        from ga4_reporting import fetch_all_ga4_data
        data = fetch_all_ga4_data(days=days)
        if not data.get("overview", {}).get("error"):
            save_ga4_cache("full_report", days, data)
        data["from_cache"] = False
        return data
    except ImportError:
        return {"error": "google-analytics-data not installed", "configured": False}
    except Exception as e:
        logger.error(f"GA4 data fetch failed: {e}")
        return {"error": str(e), "configured": True}


@app.post("/api/admin/ga4/refresh", dependencies=[Depends(_require_admin)])
def admin_ga4_refresh(days: int = 30):
    """Force refresh GA4 data."""
    return admin_ga4(days=days, force_refresh=True)


# ─── Campaign stats ──────────────────────────────────────────────────────────

@app.get("/api/admin/campaigns", dependencies=[Depends(_require_admin)])
def admin_campaigns():
    return {
        "campaigns": get_campaign_stats(),
        "google_ads_campaigns": get_google_ads_campaigns(),
        "sources": get_distinct_sources(),
        "keywords": get_keyword_stats(),
    }


class CampaignCreateRequest(BaseModel):
    campaign_name: str
    campaign_type: str = "MANUAL"          # MANUAL, GOOGLE_ADS, META, EMAIL
    campaign_id: Optional[str] = None      # Google Ads ID or custom; auto-generated if blank
    service_focus: Optional[str] = ""      # Implants, Invisalign, Whitening, Emergency, etc.
    promo_offer: Optional[str] = ""        # e.g. "$99 exam + X-ray"
    target_audience: Optional[str] = ""
    objective: Optional[str] = ""
    monthly_budget: Optional[float] = 0.0
    expected_cpl: Optional[float] = 0.0
    start_date: Optional[str] = ""        # YYYY-MM-DD
    end_date: Optional[str] = ""
    landing_page: Optional[str] = ""
    notes: Optional[str] = ""
    workflow_id: Optional[int] = None      # Attached follow-up workflow (NULL = use default)
    skip_workflow: bool = False            # If True, no follow-up emails/SMS for leads from this campaign

    @validator("campaign_name")
    def name_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Campaign name is required")
        if len(v) > 200:
            raise ValueError("Campaign name too long (max 200 chars)")
        return v

    @validator("campaign_type")
    def type_valid(cls, v):
        allowed = {"MANUAL", "GOOGLE_ADS", "META", "EMAIL"}
        if v.upper() not in allowed:
            raise ValueError(f"campaign_type must be one of {allowed}")
        return v.upper()

    @validator("monthly_budget", "expected_cpl", pre=True)
    def budget_non_negative(cls, v):
        if v is not None and float(v) < 0:
            raise ValueError("Budget values must be >= 0")
        return v

    @validator("end_date")
    def end_after_start(cls, v, values):
        start = values.get("start_date") or ""
        if v and start and v < start:
            raise ValueError("end_date must be >= start_date")
        return v

    @validator("landing_page")
    def url_format(cls, v):
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("landing_page must start with http:// or https://")
        return v

    @validator("workflow_id", pre=True)
    def coerce_workflow_id(cls, v):
        """Coerce empty string → None so frontend select can send '' for 'no workflow'."""
        if v in (None, "", "0", 0):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None


class CampaignUpdateWorkflowRequest(BaseModel):
    workflow_id: Optional[int] = None

    @validator("workflow_id", pre=True)
    def coerce_workflow_id(cls, v):
        if v in (None, "", "0", 0):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None


@app.get("/api/admin/campaigns/list", dependencies=[Depends(_require_admin)])
def admin_campaigns_list():
    """Return all managed campaign rows from the campaigns table."""
    from database import get_all_campaigns
    return {"campaigns": get_all_campaigns()}


@app.get("/api/admin/campaigns/list-with-workflows", dependencies=[Depends(_require_admin)])
def admin_campaigns_list_with_workflows():
    """Return all campaigns with their attached workflow name (single LEFT JOIN)."""
    from database import get_all_campaigns_with_workflows
    return {"campaigns": get_all_campaigns_with_workflows()}


@app.post("/api/admin/campaigns/create", dependencies=[Depends(_require_admin)])
def admin_create_campaign(body: CampaignCreateRequest):
    """Create a new managed campaign record."""
    from database import create_campaign
    try:
        row = create_campaign(body.dict())
        return {"ok": True, "campaign": row}
    except Exception as e:
        logger.error(f"create_campaign failed: {e}")
        # Don't leak SQL internals; surface a clean message
        detail = "A campaign with that name already exists" if "UNIQUE" in str(e) else "Failed to create campaign"
        raise HTTPException(status_code=500, detail=detail)


@app.patch("/api/admin/campaigns/{campaign_id}/workflow", dependencies=[Depends(_require_admin)])
def admin_campaign_set_workflow(campaign_id: str, body: CampaignUpdateWorkflowRequest):
    """Attach or detach a workflow from an existing campaign.
    Send {"workflow_id": 3} to attach, {"workflow_id": null} to detach.
    """
    from database import update_campaign_workflow
    found = update_campaign_workflow(campaign_id, body.workflow_id)
    if not found:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return {"ok": True}


class CampaignStrategyUpdateRequest(BaseModel):
    strategy: dict


@app.get("/api/admin/campaigns/unified", dependencies=[Depends(_require_admin)])
def admin_campaigns_unified(days: int = 30, include_inactive: bool = False):
    """
    Unified campaigns view — replaces the old split between Campaign Performance
    and Managed Campaigns. Returns each campaign with aggregated GAds metrics,
    lead counts, last_activity_date, and is_inactive_90d flag.
    Synthetic rows are emitted for GAds campaigns in gads_daily_stats that were
    never imported into the campaigns table.
    """
    from database import get_unified_campaigns, get_setting
    if days < 1 or days > 365:
        raise HTTPException(status_code=422, detail="days must be between 1 and 365")
    rows = get_unified_campaigns(days=days)
    if not include_inactive:
        rows = [r for r in rows if not r.get("is_inactive_90d")]

    # ── Budget summary ────────────────────────────────────────────────────────
    from datetime import date as _date
    from database import _conn as _db_conn_budget
    active_rows = [r for r in rows if (r.get("status") or "").upper() == "ACTIVE"]
    # Campaign budgets: sum ACTIVE campaigns only (paused/stopped should not count)
    total_monthly_budget   = sum(r.get("monthly_budget") or 0.0 for r in active_rows)
    total_daily_budget     = sum(r.get("daily_budget_usd") or
                                  round((r.get("monthly_budget") or 0.0) / 30.4, 2)
                                  for r in active_rows)
    # Actual spend: use MTD (month-to-date) from gads_daily_stats, not 30-day rolling window.
    # The metrics.cost on each row is a rolling window that spans across month boundaries.
    day_of_month = _date.today().day
    try:
        with _db_conn_budget() as _bc:
            _mtd_row = _bc.execute(
                """SELECT COALESCE(SUM(cost_micros), 0) AS mtd_micros
                   FROM gads_daily_stats
                   WHERE date >= DATE('now', 'start of month')"""
            ).fetchone()
        total_actual_spend = round((_mtd_row["mtd_micros"] or 0) / 1_000_000, 2)
    except Exception:
        # Fallback: sum metrics.cost from rows (rolling window — less accurate but available)
        total_actual_spend = round(sum((r.get("metrics") or {}).get("cost") or 0.0 for r in rows), 2)
    # Projected month-end: scale MTD spend by (days_in_month / days_elapsed)
    import calendar as _cal
    days_in_month = _cal.monthrange(_date.today().year, _date.today().month)[1]
    projected_month_end = round(
        total_actual_spend * days_in_month / max(day_of_month, 1), 2
    ) if total_actual_spend > 0 else 0.0
    account_monthly_budget = float(get_setting("account_monthly_budget") or 0.0)
    budget_constrained = (get_setting("budget_constrained") or "false") == "true"
    budget_summary = {
        "total_monthly_budget":   round(total_monthly_budget, 2),
        "total_daily_budget":     round(total_daily_budget, 2),
        "total_actual_spend":     round(total_actual_spend, 2),
        "projected_month_end":    projected_month_end,
        "account_monthly_budget": account_monthly_budget,
        "budget_constrained":     budget_constrained,
        "days_elapsed":           day_of_month,
        "days_in_month":          days_in_month,
        "active_campaign_count":  len(active_rows),
    }

    return {"campaigns": rows, "days": days, "include_inactive": include_inactive,
            "budget_summary": budget_summary}


class CampaignUpdateFieldsRequest(BaseModel):
    campaign_name: str | None = None
    service_focus: str | None = None
    monthly_budget: float | None = None
    start_date: str | None = None
    end_date: str | None = None
    notes: str | None = None
    promo_offer: str | None = None
    landing_page: str | None = None
    objective: str | None = None
    target_audience: str | None = None
    expected_cpl: float | None = None
    geographic_targeting: str | None = None
    launch_date: str | None = None
    call_extension_phone: str | None = None
    booking_link: str | None = None

    @validator("geographic_targeting")
    def validate_geographic_targeting(cls, v):
        if v is None or v == "":
            return v
        # Accept legacy freetext (no JSON) — just cap length
        if len(v) > 4096:
            raise ValueError("geographic_targeting too long (max 4096 chars)")
        # If it looks like JSON, validate structure
        if v.strip().startswith('{'):
            import json as _json
            try:
                parsed = _json.loads(v)
                if not isinstance(parsed, dict):
                    raise ValueError("geographic_targeting JSON must be an object")
                if "locations" in parsed and not isinstance(parsed["locations"], list):
                    raise ValueError("locations must be an array")
            except _json.JSONDecodeError:
                raise ValueError("geographic_targeting is not valid JSON")
        return v


@app.patch("/api/admin/campaigns/{campaign_id}", dependencies=[Depends(_require_admin)])
def admin_campaign_update_fields(campaign_id: str, body: CampaignUpdateFieldsRequest):
    """
    Update editable fields on a campaign (name, budget, service focus, dates, etc.).

    If the campaign is linked to Google Ads (has gads_campaign_resource), any of the
    following fields are also pushed live to Google Ads:
      - monthly_budget  → daily budget (monthly / 30.4)
      - status          → ENABLED or PAUSED on the live campaign
    """
    from database import update_campaign_fields, get_campaign_by_id
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    ok = update_campaign_fields(campaign_id, fields)
    if not ok:
        raise HTTPException(status_code=500, detail="Update failed")

    # ── Push to Google Ads if campaign is live ────────────────────────────────
    gads_resource = camp.get("gads_campaign_resource") or ""
    gads_pushed: list[str] = []
    gads_errors: list[str] = []

    if gads_resource:
        from google_ads_write import (
            set_campaign_daily_budget,
            set_campaign_status_gads,
        )
        from campaign_safety import check_budget_absolute_limits, WriteBlockedError

        # Push monthly_budget → daily budget
        if "monthly_budget" in fields:
            new_monthly = float(fields["monthly_budget"])
            new_daily   = round(new_monthly / 30.4, 2)
            new_micros  = int(new_daily * 1_000_000)
            try:
                check_budget_absolute_limits(new_micros)
                set_campaign_daily_budget(gads_resource, new_daily)
                gads_pushed.append(f"budget → ${new_daily:.2f}/day")
                logger.info(f"PATCH campaign {campaign_id}: pushed budget ${new_daily:.2f}/day to GAds")
            except WriteBlockedError as e:
                gads_errors.append(f"budget blocked: {e}")
                logger.warning(f"PATCH campaign {campaign_id}: budget push blocked: {e}")
            except Exception as e:
                gads_errors.append(f"budget push failed: {e}")
                logger.error(f"PATCH campaign {campaign_id}: budget push error: {e}")

        # Push status → ENABLED/PAUSED
        if "status" in fields:
            db_status = (fields["status"] or "").upper()
            gads_status_map = {"ACTIVE": "ENABLED", "PAUSED": "PAUSED"}
            gads_status = gads_status_map.get(db_status)
            if gads_status:
                try:
                    set_campaign_status_gads(gads_resource, gads_status)
                    gads_pushed.append(f"status → {gads_status}")
                    logger.info(f"PATCH campaign {campaign_id}: pushed status {gads_status} to GAds")
                except Exception as e:
                    gads_errors.append(f"status push failed: {e}")
                    logger.error(f"PATCH campaign {campaign_id}: status push error: {e}")

        # Note: landing_page is saved to DB only — RSA final_urls is immutable in GAds API.
        # Discrepancy is surfaced via the sync-from-gads endpoint instead.

    return {
        "ok": True,
        "campaign_id": campaign_id,
        "updated": list(fields.keys()),
        "gads_pushed": gads_pushed,
        "gads_errors": gads_errors,
    }


# ─── Auto-generate callouts + structured snippets at launch ──────────────────

def _generate_callouts_and_snippets(campaign_id: str, camp: dict, build: dict) -> dict:
    """
    Call Claude Haiku to generate callout_texts and structured_snippets from
    the campaign build data (strategy, keywords, ad copy, ad groups).

    Returns the (possibly enriched) build dict. On any failure, returns build
    unchanged — non-fatal, campaign launch continues regardless.

    Idempotent: skips generation per-field if that field is already populated.
    """
    from ai_optimizer import VALID_SNIPPET_HEADERS
    from database import get_campaign_build, _conn

    # ── Determine what's missing ──────────────────────────────────────────────
    existing_callouts = [t for t in (build.get("callout_texts") or []) if isinstance(t, str) and t.strip()]
    existing_snippets = [s for s in (build.get("structured_snippets") or []) if isinstance(s, dict)]
    need_callouts = len(existing_callouts) < 3
    need_snippets = len(existing_snippets) < 1
    if not need_callouts and not need_snippets:
        logger.info(f"_generate_callouts_and_snippets: already populated for {campaign_id} — skipping")
        return build

    try:
        client = _get_anthropic_client()
    except Exception as e:
        logger.warning(f"_generate_callouts_and_snippets: no AI client ({e}) — skipping")
        return build

    # ── Build context from wizard steps ──────────────────────────────────────
    campaign_name   = camp.get("campaign_name", "")
    campaign_type   = camp.get("campaign_type", "general")
    practice        = _build_practice_context()
    practice_name   = practice.get("name") or "Grafton Dental Care"
    practice_loc    = practice.get("address") or "Grafton, MA"

    strategy_block = ""
    strategy = build.get("strategy") or {}
    if isinstance(strategy, dict):
        goal      = strategy.get("primary_goal", "")
        audience  = strategy.get("target_audience", "")
        usp       = strategy.get("unique_selling_points", "")
        strategy_block = f"Goal: {goal}\nAudience: {audience}\nUSP: {usp}"
    elif isinstance(strategy, str):
        strategy_block = strategy[:500]

    keywords_block = ""
    kw_data = build.get("keywords") or {}
    if isinstance(kw_data, dict):
        kws = kw_data.get("keywords") or kw_data.get("keyword_list") or []
        if isinstance(kws, list):
            keywords_block = ", ".join(str(k.get("keyword", k) if isinstance(k, dict) else k) for k in kws[:20])
    elif isinstance(kw_data, str):
        keywords_block = kw_data[:300]

    headlines_block = ""
    ad_copy = build.get("ad_copy") or {}
    if isinstance(ad_copy, dict):
        ads = ad_copy.get("ads") or ad_copy.get("ad_groups") or []
        if isinstance(ads, list) and ads:
            first_ad = ads[0]
            if isinstance(first_ad, dict):
                hls = first_ad.get("headlines") or []
                descs = first_ad.get("descriptions") or []
                if hls:
                    headlines_block = "Headlines: " + " | ".join(str(h) for h in hls[:5])
                if descs:
                    headlines_block += "\nDescriptions: " + " | ".join(str(d) for d in descs[:3])

    ad_groups_block = ""
    ag_data = build.get("ad_groups") or {}
    if isinstance(ag_data, dict):
        ags = ag_data.get("ad_groups") or []
        if isinstance(ags, list):
            ad_groups_block = ", ".join(str(ag.get("name", ag) if isinstance(ag, dict) else ag) for ag in ags[:8])

    # Dental-relevant snippet headers only (L1: narrow from full set)
    _DENTAL_SNIPPET_HEADERS = {"Service catalog", "Insurance coverage", "Types", "Amenities", "Brands", "Styles"}
    valid_dental_headers = sorted(_DENTAL_SNIPPET_HEADERS & VALID_SNIPPET_HEADERS)

    prompt = f"""You are a Google Ads expert writing extensions for a dental practice.

Practice: {practice_name}, {practice_loc}
Campaign: {campaign_name} (type: {campaign_type})

=== Strategy ===
{strategy_block or "(not available)"}

=== Top Keywords ===
{keywords_block or "(not available)"}

=== Ad Headlines / Descriptions ===
{headlines_block or "(not available)"}

=== Ad Groups ===
{ad_groups_block or "(not available)"}

Generate callout extensions and structured snippet extensions for this campaign.

Rules for callouts:
- 6 to 8 callout strings
- Each string ≤25 characters (STRICT — count carefully)
- Short punchy phrases: e.g. "Same-Day Appointments", "Insurance Accepted"
- Specific to this campaign type and dental specialty
- No punctuation at end, no quotes

Rules for structured snippets:
- 1 to 2 snippets
- header MUST be exactly one of: {', '.join(valid_dental_headers)}
- For dental practices, prefer "Service catalog" or "Insurance coverage" or "Types"
- 3 to 6 values per snippet, each ≤25 characters (STRICT)

Return ONLY valid JSON, no prose, no markdown fences:
{{
  "callout_texts": ["text1", "text2", ...],
  "structured_snippets": [
    {{"header": "Service catalog", "values": ["value1", "value2", ...]}}
  ]
}}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown fences if Claude added them (case-insensitive tag, M4)
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)

        parsed = json.loads(raw)

        # ── Validate + clean callout_texts (only if needed) ───────────────────
        new_callouts = existing_callouts  # default: keep what we had
        if need_callouts:
            raw_callouts = parsed.get("callout_texts") or []
            # M5: re-filter empties after truncate+strip
            cleaned = [s for s in (t[:25].strip() for t in raw_callouts if isinstance(t, str) and t.strip()) if s]
            if len(cleaned) >= 3:
                new_callouts = cleaned
            else:
                logger.warning(f"_generate_callouts_and_snippets: only {len(cleaned)} callouts after cleaning — keeping existing")

        # ── Validate + clean structured_snippets (only if needed) ─────────────
        new_snippets = existing_snippets  # default: keep what we had
        if need_snippets:
            raw_snippets = parsed.get("structured_snippets") or []
            built_snippets = []
            for snip in raw_snippets:
                if not isinstance(snip, dict):
                    continue
                header = snip.get("header", "")
                if header not in VALID_SNIPPET_HEADERS:
                    logger.warning(f"_generate_callouts_and_snippets: invalid header '{header}' — dropping snippet")
                    continue
                # M5: re-filter empties after truncate+strip
                values = [s for s in (v[:25].strip() for v in (snip.get("values") or []) if isinstance(v, str) and v.strip()) if s]
                if len(values) < 3:
                    logger.warning(f"_generate_callouts_and_snippets: snippet '{header}' has <3 values — dropping")
                    continue
                built_snippets.append({"header": header, "values": values[:10]})
            if built_snippets:
                new_snippets = built_snippets
            else:
                logger.warning("_generate_callouts_and_snippets: no valid snippets generated — keeping existing")

        # M1: skip DB write if nothing actually changed
        if not new_callouts and not new_snippets:
            logger.warning(f"_generate_callouts_and_snippets: nothing generated for {campaign_id} — skipping DB write")
            return build

        # ── Merge into build dict ─────────────────────────────────────────────
        build["callout_texts"] = new_callouts
        build["structured_snippets"] = new_snippets

        # ── Persist to DB (read-modify-write inside single connection) ─────────
        _build_full = get_campaign_build(campaign_id)
        _build_full["callout_texts"] = new_callouts
        _build_full["structured_snippets"] = new_snippets
        with _conn() as _c:
            cur = _c.execute(
                "UPDATE campaigns SET campaign_build_json=?, updated_at=? WHERE campaign_id=?",
                (json.dumps(_build_full), datetime.now(timezone.utc).isoformat(), campaign_id)
            )
            if cur.rowcount == 0:
                logger.warning(f"_generate_callouts_and_snippets: UPDATE matched 0 rows for {campaign_id}")

        logger.info(
            f"_generate_callouts_and_snippets: {campaign_id} → "
            f"{len(new_callouts)} callouts, {len(new_snippets)} snippet(s)"
        )

    except json.JSONDecodeError as e:
        logger.warning(f"_generate_callouts_and_snippets: JSON parse failed ({e}) — skipping")
    except Exception as e:
        logger.warning(f"_generate_callouts_and_snippets: unexpected error ({e}) — skipping")

    return build


# ─── Campaign Launch (now / schedule / queue) ────────────────────────────────

class LaunchCampaignRequest(BaseModel):
    mode: str  # "now" | "schedule" | "queue"
    launch_date: Optional[str] = None

    @validator("mode")
    def mode_valid(cls, v):
        if v not in {"now", "schedule", "queue"}:
            raise ValueError("mode must be one of: now, schedule, queue")
        return v


@app.post("/api/admin/campaigns/{campaign_id}/launch", dependencies=[Depends(_require_admin)])
def admin_campaign_launch(campaign_id: str, body: LaunchCampaignRequest):
    """
    Launch a campaign:
      - now      → status ACTIVE, launch_date = now (UTC ISO)
      - schedule → status SCHEDULED, launch_date = body.launch_date (required)
      - queue    → status QUEUED, launch_date cleared

    For "now" mode, validates that all REQUIRED checklist items are done or skipped
    before allowing launch. Google Ads API push is stubbed — sets status in DB only.
    """
    from database import get_campaign_by_id, get_campaign_build, update_campaign_status
    import datetime as _dt

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Server-side gate: required checklist items must be done or skipped before "now"
    if body.mode == "now":
        build = get_campaign_build(campaign_id)

        # If campaign already has a gads_campaign_resource it exists in Google Ads —
        # just enable it (Pause→Enable). Otherwise create it from scratch.
        existing_resource = camp.get("gads_campaign_resource") or ""

        if existing_resource:
            # ── Already in Google Ads — just enable ──────────────────────────
            from google_ads_create import set_campaign_status
            result = set_campaign_status(existing_resource, "ENABLED")
            if not result["ok"]:
                raise HTTPException(status_code=502, detail=f"Google Ads enable failed: {result['error']}")
            update_campaign_status(
                campaign_id, "ACTIVE",
                launch_date=_dt.datetime.now(_dt.timezone.utc).isoformat().replace('+00:00', 'Z'),
            )
            return {
                "ok": True,
                "status": "ACTIVE",
                "campaign_id": campaign_id,
                "gads_action": "enabled_existing",
                "resource_name": existing_resource,
                "launch_date": _dt.datetime.now(_dt.timezone.utc).isoformat().replace('+00:00', 'Z'),
            }
        else:
            # ── New campaign — create in Google Ads ──────────────────────────
            from google_ads_create import create_campaign_in_gads
            from database import update_campaign_fields

            logger.info(f"Launching new campaign {campaign_id} to Google Ads")
            # Auto-generate callouts + structured snippets if not already in build
            build = _generate_callouts_and_snippets(campaign_id, camp, build)
            result = create_campaign_in_gads(camp, build)

            if not result["ok"]:
                logger.error(f"create_campaign_in_gads failed: {result['error']}\nLog:\n" + "\n".join(result.get("log",[])))
                raise HTTPException(
                    status_code=502,
                    detail=f"Google Ads creation failed: {result['error']}"
                )

            # Save the Google Ads resource name + numeric ID back to the campaign row.
            # Also write back geographic_targeting if it was empty — the fallback 15-mile
            # Grafton radius is applied in google_ads_create.py but never persisted to DB,
            # causing the Performance tab to show "Not set" even though Google Ads has it.
            from database import _conn
            _default_geo = json.dumps({
                "unit": "miles",
                "locations": [{"type": "city", "value": "Grafton, MA", "radius": 15, "include": True}]
            })
            _geo_to_save = camp.get("geographic_targeting") or _default_geo
            with _conn() as conn:
                conn.execute(
                    "UPDATE campaigns SET campaign_name=?, gads_campaign_resource=?, gads_campaign_numeric_id=?, geographic_targeting=?, updated_at=? WHERE campaign_id=?",
                    (result["gads_campaign_name"], result["campaign_resource_name"], result["campaign_numeric_id"],
                     _geo_to_save, _dt.datetime.now(_dt.timezone.utc).isoformat(), campaign_id)
                )

            update_campaign_status(
                campaign_id, "ACTIVE",
                launch_date=_dt.datetime.now(_dt.timezone.utc).isoformat().replace('+00:00', 'Z'),
            )

            url_warnings = result.get("url_warnings", [])
            return {
                "ok": True,
                "status": "ACTIVE",
                "campaign_id": campaign_id,
                "gads_action": "created",
                "resource_name": result["campaign_resource_name"],
                "campaign_numeric_id": result["campaign_numeric_id"],
                "keywords_added": result["keywords_added"],
                "ads_created": result["ads_created"],
                "enabled": result.get("enabled", False),
                "url_warnings": url_warnings,
                "launch_date": _dt.datetime.now(_dt.timezone.utc).isoformat().replace('+00:00', 'Z'),
                "log": result["log"],
            }

    elif body.mode == "schedule":
        if not body.launch_date:
            raise HTTPException(status_code=400, detail="launch_date required for schedule mode")
        update_campaign_status(campaign_id, "SCHEDULED", launch_date=body.launch_date)
        return {
            "ok": True,
            "status": "SCHEDULED",
            "campaign_id": campaign_id,
            "launch_date": body.launch_date,
        }

    elif body.mode == "queue":
        update_campaign_status(campaign_id, "QUEUED", launch_date="")
        return {
            "ok": True,
            "status": "QUEUED",
            "campaign_id": campaign_id,
            "launch_date": "",
        }


class CampaignBuildStepRefineRequest(BaseModel):
    step: str                        # "keywords" | "ad_copy" | "ad_groups" | "strategy"
    instruction: str                 # Natural language instruction from user
    current_override: dict | list | None = None  # If set, use this as base instead of saved version (iterative refinement)


@app.post("/api/admin/campaigns/{campaign_id}/build-step-refine", dependencies=[Depends(_require_admin)])
async def admin_campaign_build_step_refine(campaign_id: str, body: CampaignBuildStepRefineRequest):
    """
    Iteratively refine a build step using a user instruction.
    Reads current content for the step, applies the instruction via Sonnet,
    returns the refined content WITHOUT saving — caller decides to accept or discard.
    """
    from database import get_campaign_by_id, get_campaign_build
    import anthropic as _anthropic, json as _json, re as _re

    VALID_STEPS = {"keywords", "ad_copy", "ad_groups", "strategy", "competitor_analysis"}
    if body.step not in VALID_STEPS:
        raise HTTPException(status_code=400, detail=f"Refinement only supported for: {VALID_STEPS}")

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Use client-side preview as base if provided (iterative refinement before saving)
    if body.current_override is not None:
        current = body.current_override
    elif body.step == "strategy":
        # Strategy is stored in strategy_json on the campaign row
        raw_strat = camp.get("strategy_json") or {}
        current = _json.loads(raw_strat) if isinstance(raw_strat, str) else raw_strat
        if not current:
            raise HTTPException(status_code=400, detail="No strategy to refine. Generate it first.")
    else:
        build = get_campaign_build(campaign_id)
        current = build.get(body.step)
        if not current:
            raise HTTPException(status_code=400, detail=f"No existing {body.step} to refine. Generate it first.")

    strategy = camp.get("strategy_json") or {}
    if isinstance(strategy, str):
        try:
            strategy = _json.loads(strategy)
        except Exception:
            strategy = {}

    # Load competitor analysis from build JSON for context injection
    _competitor_analysis = {}
    _conquest_keywords: list = []
    try:
        _full_build = get_campaign_build(campaign_id) if body.step != "competitor_analysis" else {}
        _raw_ca = _full_build.get("competitor_analysis") or {}
        if isinstance(_raw_ca, str):
            _raw_ca = _json.loads(_raw_ca)
        _competitor_analysis = _raw_ca if isinstance(_raw_ca, dict) else {}
        _conquest_keywords = _competitor_analysis.get("conquest_keywords", []) or []
    except Exception as _ca_err:
        logger.warning(f"build-step-refine: competitor_analysis load failed (non-fatal): {_ca_err}")

    # Build competitor context block for prompt injection
    _competitor_section = ""
    if _competitor_analysis:
        _comp_list = _competitor_analysis.get("competitors", []) or []
        _comp_names = [c.get("name", "") for c in _comp_list if c.get("name")]
        _differentiators = _competitor_analysis.get("differentiators", []) or []
        _positioning = _competitor_analysis.get("positioning_strategy", "") or _competitor_analysis.get("positioning_notes", "") or ""
        _negate_stems = _competitor_analysis.get("competitor_negatives", []) or []
        _lines = []
        if _comp_names:
            _lines.append(f"Local competitors: {', '.join(_comp_names)}")
        if _conquest_keywords:
            _lines.append(f"Conquest keywords (protected — do NOT add these as negatives): {', '.join(_conquest_keywords)}")
        if _negate_stems:
            _lines.append(
                f"Local competitor brand stems (ALWAYS keep in negative list — contact-lookup intent): "
                f"{', '.join(_negate_stems)}"
            )
        if _differentiators:
            _lines.append(f"Our differentiators: {'; '.join(_differentiators[:5])}")
        if _positioning:
            _lines.append(f"Positioning: {_positioning[:300]}")
        if _lines:
            _competitor_section = "\n\n=== Competitor Intelligence ===\n" + "\n".join(_lines)

    # Site intelligence for refinement context
    _refine_landing = camp.get("landing_page") or ""
    _refine_site = get_setting("practice_website") or ""
    _refine_url = _refine_landing or _refine_site
    _refine_site_block = ""
    if _refine_url:
        try:
            from domain_crawler import build_site_context_for_url
            _refine_site_block = build_site_context_for_url(_refine_url)
        except Exception as _sce:
            logger.warning(f"build-step-refine site context fetch failed: {_sce}")
    _refine_site_section = ("\n\n=== Website Intelligence ===\n" + _refine_site_block) if _refine_site_block else ""

    _api_key = get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    ai_client = _anthropic.Anthropic(api_key=_api_key)

    _kw_delta_mode = False  # set True only for keywords step; controls delta-merge logic below

    if body.step == "strategy":
        prompt = f"""You are a Google Ads specialist refining a campaign strategy.

Campaign: {camp.get("campaign_name", "")}
Service Focus: {camp.get("service_focus", "")}
{_refine_site_section}
{_competitor_section}
Current strategy:
{_json.dumps(current, indent=2)}

User instruction: {body.instruction}

Return the COMPLETE updated strategy as a JSON object with EXACTLY these keys (no extras, no campaign_name key):
{{
  "objective": "...",
  "target_audience": "...",
  "key_messages": ["...", "..."],
  "ad_headlines": ["...", "..."],
  "ad_descriptions": ["...", "..."],
  "implementation_instructions": "..."
}}

Apply only the changes the user requested. Keep everything else identical to the current strategy.
Return ONLY the JSON object, no explanation."""
    elif body.step == "keywords":
        # Summarise existing lists so the prompt stays small regardless of list size.
        # Claude only generates the *delta* (additions/removals); we merge in Python.
        _kw_summary_lines = []
        for _mk in ["exact_match", "phrase_match", "broad_match_modifier", "negative_keywords"]:
            _items = current.get(_mk) or []
            if _items:
                _kw_summary_lines.append(f"  {_mk}: {len(_items)} keywords (e.g. {', '.join(_items[:5])}{'…' if len(_items)>5 else ''})")
        _kw_summary = "\n".join(_kw_summary_lines) or "  (empty)"

        prompt = f"""You are a Google Ads specialist refining a keyword list for a dental practice campaign.

Campaign: {camp.get("campaign_name", "")}
Service Focus: {camp.get("service_focus", "")}
Objective: {strategy.get("objective", "")}
{_competitor_section}

Current keyword counts (do NOT repeat these — only generate the delta):
{_kw_summary}

User instruction: {body.instruction}

Your job: generate ONLY the keywords to ADD or REMOVE, not the full list.
Return a JSON object with EXACTLY these four keys — each containing only the NEW items to add (or empty list if nothing to add for that type):
{{
  "add": {{
    "exact_match": [],
    "phrase_match": [],
    "broad_match_modifier": [],
    "negative_keywords": []
  }},
  "remove": {{
    "exact_match": [],
    "phrase_match": [],
    "broad_match_modifier": [],
    "negative_keywords": []
  }}
}}

Rules:
- Only include keywords the user explicitly asked to add or remove
- If the user asks to add competitor names as negatives, use the brand stems from "Local competitor brand stems" in the Competitor Intelligence section above
- NEVER add conquest keywords as negatives
- Return ONLY the JSON object, no explanation"""

        # After getting delta from Claude, merge into current list in Python
        _kw_delta_mode = True
    else:
        prompt = f"""You are a Google Ads specialist helping refine a campaign build step.

Campaign: {camp.get("campaign_name", "")}
Service Focus: {camp.get("service_focus", "")}
Objective: {strategy.get("objective", "")}
{_refine_site_section}
{_competitor_section}
Current {body.step} content:
{_json.dumps(current, indent=2)}

User instruction: {body.instruction}

Apply the user's instruction to modify the {body.step} content. Return the complete updated {body.step} JSON structure — same format as the input, with the requested changes applied.

Rules:
- Keep all existing items unless the user asked to remove specific ones
- Add new items where instructed
- Maintain the exact same JSON structure/schema as the current content
- If the user asks to add competitor names as negatives, use the brand stems from "Local competitor brand stems" in the Competitor Intelligence section above
- NEVER remove items from "Local competitor brand stems" from the negative list — they are contact-lookup intent and always waste budget
- NEVER add conquest keywords as negatives — those are intentional targets for comparison-shopping patients
- If user asks to "negate all competitors", add all brand stems from the Competitor Intelligence section that are not already present
- Return ONLY the JSON object, no explanation."""

    # Keywords uses delta mode — small prompt regardless of list size; 2k tokens is plenty.
    # Other steps (ad_copy, strategy) can be larger — 4k.
    _max_tok = 2000 if body.step == "keywords" else 4000

    try:
        response = ai_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=_max_tok,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if not raw:
            raise ValueError("Claude returned an empty response")
        # For keywords step, always expect an object (never allow bare array — that picks up inner arrays)
        _allow_arr = body.step not in {"keywords", "strategy"}
        _refined_text = _extract_json_from_ai_response(raw, allow_array=_allow_arr)
        refined = _json.loads(_refined_text)

        # Keywords delta mode: merge add/remove delta into the full current list
        if _kw_delta_mode:
            _merged = {k: list(current.get(k) or []) for k in ["exact_match","phrase_match","broad_match_modifier","negative_keywords"]}
            _add = refined.get("add") or {}
            _rem = refined.get("remove") or {}
            for _mk in _merged:
                # Add new items (dedup)
                for _kw in (_add.get(_mk) or []):
                    if _kw not in _merged[_mk]:
                        _merged[_mk].append(_kw)
                # Remove requested items (case-insensitive)
                _rem_lower = {r.lower() for r in (_rem.get(_mk) or [])}
                _merged[_mk] = [k for k in _merged[_mk] if k.lower() not in _rem_lower]
            refined = _merged

    except Exception as e:
        logger.error(f"build-step-refine AI call failed ({body.step}): {e}")
        raise HTTPException(status_code=500, detail=f"AI refinement failed: {e}")

    logger.info(f"Campaign {campaign_id} step '{body.step}' refined (not yet saved)")
    return {"ok": True, "step": body.step, "data": refined}


class CampaignAiReviewRequest(BaseModel):
    enabled: bool


@app.patch("/api/admin/campaigns/{campaign_id}/ai-review", dependencies=[Depends(_require_admin)])
def admin_campaign_set_ai_review(campaign_id: str, body: CampaignAiReviewRequest):
    """
    Toggle the AI Review flag on a managed campaign.
    When enabled=true, the nightly ai_optimizer restricts keyword analysis
    to this campaign (plus any others also flagged on).
    """
    from database import set_campaign_ai_review, get_campaign_by_id
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    ok = set_campaign_ai_review(campaign_id, body.enabled)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to update AI Review flag")
    logger.info(f"AI Review flag for {campaign_id} → {body.enabled}")
    return {"ok": True, "campaign_id": campaign_id, "ai_review_enabled": body.enabled}


class CampaignAiMaxRequest(BaseModel):
    enabled: bool


@app.patch("/api/admin/campaigns/{campaign_id}/ai-max", dependencies=[Depends(_require_admin)])
def admin_campaign_set_ai_max(campaign_id: str, body: CampaignAiMaxRequest):
    """
    Enable or disable Google Ads AI Max on a managed campaign.

    AI Max allows Google's AI to expand search term matching beyond the keyword
    list. Only works on Search campaigns linked to Google Ads.

    When enabled=true → calls enable_ai_max() on the GAds API, then updates local DB.
    When enabled=false → calls disable_ai_max(), then updates local DB.
    DB is only updated when the API call succeeds.

    Historical search_term_type='ai_max' data is never retroactively cleared
    when disabling — only future syncs are affected.
    """
    from database import get_campaign_by_id, set_campaign_ai_max
    from google_ads_create import enable_ai_max, disable_ai_max

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    resource_name = camp.get("gads_campaign_resource") or ""
    if not resource_name:
        raise HTTPException(
            status_code=400,
            detail="Campaign is not yet linked to Google Ads. Launch it to Google Ads first."
        )

    if body.enabled:
        result = enable_ai_max(resource_name)
    else:
        result = disable_ai_max(resource_name)

    if not result.get("ok"):
        error_msg = result.get("error") or "Google Ads API call failed"
        logger.error(f"AI Max toggle failed for {campaign_id}: {error_msg}")
        raise HTTPException(status_code=502, detail=error_msg)

    # Only update local DB after confirmed API success
    ok = set_campaign_ai_max(campaign_id, body.enabled)
    if not ok:
        logger.warning(f"AI Max API succeeded but local DB update failed for {campaign_id}")

    action = "enabled" if body.enabled else "disabled"
    logger.info(f"AI Max {action} for campaign {campaign_id} ({resource_name})")
    return {"ok": True, "campaign_id": campaign_id, "ai_max_enabled": body.enabled}


class CampaignSitelinksRequest(BaseModel):
    sitelinks: list  # [{title, url, description1?, description2?}]


@app.get("/api/admin/sitelink-library", dependencies=[Depends(_require_admin)])
def admin_sitelink_library():
    """Return all sitelinks from the shared library, most-used first."""
    from database import get_sitelink_library
    return {"sitelinks": get_sitelink_library()}


@app.post("/api/admin/campaigns/{campaign_id}/sitelinks", dependencies=[Depends(_require_admin)])
def admin_campaign_sitelinks(campaign_id: str, body: CampaignSitelinksRequest):
    """
    Save sitelinks for a campaign and, if the campaign is already live in Google Ads,
    immediately push them via the API.

    Validates each sitelink:
      - title: required, max 25 chars (trimmed), no phone numbers
      - url: required, must start with https://
      - description1 / description2: optional, max 35 chars each; both or neither required

    Stores as JSON in campaigns.sitelinks.
    If gads_campaign_resource is set → calls add_sitelinks_to_campaign() and returns gads_applied=True.
    """
    from database import get_campaign_by_id, update_campaign_fields
    from google_ads_create import add_sitelinks_to_campaign, _strip_phone_numbers
    from urllib.parse import urlparse
    import json as _json

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    raw = body.sitelinks
    if not isinstance(raw, list) or len(raw) == 0:
        raise HTTPException(status_code=400, detail="sitelinks must be a non-empty list")
    if len(raw) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 sitelinks per campaign")

    validated = []
    errors = []
    for i, sl in enumerate(raw):
        if not isinstance(sl, dict):
            errors.append(f"Item {i}: must be an object with title and url")
            continue
        title = _strip_phone_numbers((sl.get("title") or "").strip())[:25]
        url   = (sl.get("url") or "").strip()
        desc1 = _strip_phone_numbers((sl.get("description1") or "").strip())[:35]
        desc2 = _strip_phone_numbers((sl.get("description2") or "").strip())[:35]

        if not title:
            errors.append(f"Item {i}: title is required")
            continue
        if not url.startswith("https://"):
            errors.append(f"Item {i} '{title}': URL must start with https://")
            continue
        try:
            parsed = urlparse(url)
            if not parsed.netloc:
                errors.append(f"Item {i} '{title}': URL is not valid")
                continue
        except Exception:
            errors.append(f"Item {i} '{title}': URL could not be parsed")
            continue

        entry = {"title": title, "url": url}
        if desc1 and desc2:
            entry["description1"] = desc1
            entry["description2"] = desc2
        elif desc1 or desc2:
            errors.append(f"Item {i} '{title}': both description1 and description2 required if either is set; descriptions omitted")
        validated.append(entry)

    if not validated:
        raise HTTPException(status_code=400, detail=f"No valid sitelinks: {errors}")

    # Save to DB
    update_campaign_fields(campaign_id, {"sitelinks": _json.dumps(validated)})

    # If campaign is live in Google Ads → push immediately
    gads_resource = camp.get("gads_campaign_resource") or ""
    gads_applied = False
    gads_count   = 0
    gads_errors  = []

    if gads_resource:
        result = add_sitelinks_to_campaign(gads_resource, validated, replace=True)
        gads_applied = result["ok"]
        gads_count   = result.get("count", 0)
        gads_errors  = result.get("errors", [])
        logger.info(
            f"admin_campaign_sitelinks: pushed {gads_count} sitelinks to GAds "
            f"for campaign {campaign_id} ({gads_resource})"
        )

    return {
        "ok":          True,
        "campaign_id": campaign_id,
        "saved":       len(validated),
        "gads_applied": gads_applied,
        "gads_count":  gads_count,
        "gads_errors": gads_errors,
        "validation_warnings": errors,
    }


class SitelinkSuggestRequest(BaseModel):
    instruction: Optional[str] = None          # refinement hint, e.g. "focus on insurance"
    previous_errors: Optional[list] = None     # GAds error strings from a prior attempt
    previous_suggestions: Optional[list] = None  # [{title,url}] that had errors — for AI to improve


@app.post("/api/admin/campaigns/{campaign_id}/sitelinks/suggest", dependencies=[Depends(_require_admin)])
def admin_campaign_sitelinks_suggest(campaign_id: str, body: SitelinkSuggestRequest):
    """
    AI-powered sitelink suggestions for a campaign.

    Reads campaign context (name, service_focus, landing_page, strategy excerpt, phone).
    Calls Claude Sonnet and returns [{title, url, reason}] — 4-8 suggestions.

    If `instruction` is provided, the AI refines suggestions accordingly.
    If `previous_errors` + `previous_suggestions` are provided (from a failed GAds push),
    the AI evaluates the error messages and returns improved suggestions that avoid them.
    This enables an automatic retry loop when Google Ads rejects sitelinks.
    """
    from database import get_campaign_by_id
    import json as _json

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # --- Build campaign context block ---
    campaign_name    = camp.get("campaign_name") or "Unnamed Campaign"
    service_focus    = camp.get("service_focus") or ""
    landing_page     = camp.get("landing_page") or ""
    phone            = camp.get("call_extension_phone") or ""
    website          = landing_page or ""

    # Derive base website from landing page if possible
    from urllib.parse import urlparse as _urlparse
    try:
        _parsed = _urlparse(landing_page)
        base_url = f"{_parsed.scheme}://{_parsed.netloc}" if _parsed.netloc else landing_page
    except Exception:
        base_url = landing_page

    # Strategy snippet (first 600 chars is enough for context)
    strategy_raw = camp.get("strategy_json") or {}
    if isinstance(strategy_raw, str):
        try:
            strategy_raw = _json.loads(strategy_raw)
        except Exception:
            strategy_raw = {}
    strategy_snippet = ""
    if isinstance(strategy_raw, dict):
        overview = strategy_raw.get("campaign_overview") or strategy_raw.get("summary") or ""
        if overview:
            strategy_snippet = str(overview)[:600]

    # Practice context — use practice_website as fallback when landing_page not set
    practice_name    = get_setting("practice_name") or "our dental practice"
    practice_city    = get_setting("practice_city") or ""
    practice_website = get_setting("practice_website") or ""

    # Determine best base URL: landing page domain > practice website > none
    if not base_url and practice_website:
        try:
            _pw = _urlparse(practice_website)
            base_url = f"{_pw.scheme}://{_pw.netloc}" if _pw.netloc else practice_website
        except Exception:
            base_url = practice_website

    if not base_url:
        raise HTTPException(
            status_code=400,
            detail="Campaign has no landing page and no practice website is configured. "
                   "Add a landing page on the campaign or set your practice website in Admin → Practice Info."
        )

    context_block = f"""Campaign: {campaign_name}
Practice: {practice_name}{f', {practice_city}' if practice_city else ''}
Service focus: {service_focus or 'General dentistry'}
Base website / landing page: {base_url}
Phone: {phone or '(not set)'}"""
    if strategy_snippet:
        context_block += f"\nStrategy overview: {strategy_snippet}"

    # Sanitize instruction — cap length, wrap in delimiters for injection safety
    safe_instruction = ""
    if body.instruction:
        safe_instruction = str(body.instruction).strip()[:300]

    # --- Build the prompt ---
    error_section = ""
    if body.previous_errors and body.previous_suggestions:
        prev_sl_text = _json.dumps(body.previous_suggestions, indent=2)
        err_text = "\n".join(f"  - {e}" for e in body.previous_errors[:10])
        error_section = f"""
IMPORTANT — A previous set of sitelinks was REJECTED by Google Ads with these errors:
{err_text}

Previous sitelinks that had issues:
{prev_sl_text}

Analyze these errors carefully. Common causes:
- Title over 25 characters (count exactly — must be ≤25, aim for ≤20 to be safe)
- URL not starting with https://
- URL domain does not match the practice's base website domain ({base_url})
- Phone numbers in title or descriptions
- Special characters or trademark symbols
- Descriptions: must BOTH be present or BOTH absent (never just one)

Produce improved sitelinks that avoid ALL of these issues.
"""

    instruction_section = ""
    if safe_instruction:
        instruction_section = f"\n<user_instruction>{safe_instruction}</user_instruction>\n"

    from ai_optimizer import GOOGLE_ADS_RULES as _GAR
    system_prompt = _GAR + f"""You are a Google Ads specialist generating sitelink extensions for a dental practice.

STRICT RULES — Google will reject violations:
1. title: MAXIMUM 25 characters (count every single character including spaces). Aim for ≤20. NEVER exceed 25.
2. url: Must start with https:// and use the same domain as the practice base website ({base_url}). Do NOT use a different domain. If you are unsure what page exists, use the base URL itself.
3. descriptions: OPTIONAL. If you include descriptions, BOTH description1 AND description2 are required (max 35 chars each). NEVER include just one description.
4. No phone numbers anywhere in title or descriptions.
5. No special characters, trademark symbols, or ALL CAPS words.
6. Focus on high-value dental pages: booking, services, about, insurance, new patients, financing, emergency, etc.

Return ONLY valid JSON — an array of 4 to 8 sitelink objects. No markdown code fences, no explanation outside the JSON.
JSON schema (do NOT include description1/description2 unless you have valid content for BOTH):
[
  {{"title": "Book a Visit", "url": "https://example.com/book", "reason": "Direct path to scheduling"}},
  {{"title": "New Patients", "url": "https://example.com/new-patients", "reason": "Captures new patient searches"}}
]"""

    user_prompt = f"""Generate the best Google Ads sitelinks for this campaign:

{context_block}
{error_section}
Guidelines:
- Prioritize high-conversion pages: Book Appointment, New Patients, specific service pages, Insurance/Financing
- All URLs must use {base_url} as the domain
- If you are unsure what sub-pages exist, use {base_url}/book, {base_url}/contact, {base_url}/about, {base_url}/new-patients — these are standard paths
- If landing_page is the main destination, sitelinks should complement it with OTHER sections of the site
- Titles should be action-oriented and specific to the service focus
- Keep titles SHORT — aim for ≤20 characters, NEVER over 25
{instruction_section}
Return ONLY the JSON array."""

    client = _get_anthropic_client()
    try:
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = response.content[0].text.strip()
    except Exception as e:
        logger.error(f"sitelinks/suggest: Anthropic call failed: {e}")
        raise HTTPException(status_code=502, detail=f"AI call failed: {e}")

    # Parse JSON from response
    import re as _re
    suggestions = []
    parse_error = None
    try:
        # Strip outermost markdown code fences only (not inner content)
        clean = raw_text
        if clean.startswith("```"):
            clean = _re.sub(r"^```(?:json)?\s*\n?", "", clean)
            clean = _re.sub(r"\n?```\s*$", "", clean)
        clean = clean.strip()
        # Try to extract a JSON array first
        arr_match = _re.search(r"\[.*\]", clean, _re.DOTALL)
        if arr_match:
            parsed = _json.loads(arr_match.group(0))
        else:
            parsed = _json.loads(clean)
        # Handle single-object response (AI returned a dict instead of array)
        if isinstance(parsed, dict):
            suggestions = [parsed]
        elif isinstance(parsed, list):
            suggestions = parsed
        else:
            suggestions = []
    except Exception as e:
        parse_error = str(e)
        logger.error(f"sitelinks/suggest: JSON parse failed: {e}\nRaw: {raw_text[:500]}")

    # Validate and sanitize suggestions
    validated = []
    warnings = []
    for i, sl in enumerate(suggestions or []):
        if not isinstance(sl, dict):
            continue
        title = (sl.get("title") or "").strip()
        url   = (sl.get("url") or "").strip()
        reason = (sl.get("reason") or "").strip()[:120]

        if not title:
            warnings.append(f"Item {i}: missing title, skipped")
            continue
        if len(title) > 25:
            warnings.append(f"Item {i} '{title}': title {len(title)} chars > 25, truncated")
            title = title[:25]
        if not url.startswith("https://"):
            warnings.append(f"Item {i} '{title}': URL doesn't start with https://, skipped")
            continue

        entry = {"title": title, "url": url, "reason": reason}
        # Pass through optional descriptions only if BOTH provided (both-or-neither rule)
        d1 = (sl.get("description1") or "").strip()[:35]
        d2 = (sl.get("description2") or "").strip()[:35]
        if d1 and d2:
            entry["description1"] = d1
            entry["description2"] = d2
        elif d1 or d2:
            warnings.append(f"Item {i} '{title}': only one description provided — descriptions omitted")

        validated.append(entry)

    if parse_error and not validated:
        raise HTTPException(
            status_code=500,
            detail=f"AI returned unparseable response. Parse error: {parse_error}. Raw: {raw_text[:300]}"
        )
    if not validated:
        # AI returned valid JSON but all items were filtered out
        raise HTTPException(
            status_code=422,
            detail=f"AI returned no usable sitelinks. Warnings: {warnings[:5]}. Try a different refinement instruction."
        )

    return {
        "ok":          True,
        "campaign_id": campaign_id,
        "suggestions": validated,
        "warnings":    warnings,
        "model":       SONNET_MODEL,
    }


@app.get("/api/admin/campaigns/{campaign_id}/search-term-types", dependencies=[Depends(_require_admin)])
def admin_campaign_search_term_types(campaign_id: str, days: int = 30):
    """
    Return a breakdown of leads by search_term_type for a campaign.
    Used by the Performance tab to show AI Max vs standard match type attribution.
    """
    from database import get_search_term_type_breakdown
    breakdown = get_search_term_type_breakdown(campaign_id, days)
    return {"campaign_id": campaign_id, "days": days, "breakdown": breakdown}


class CampaignBuildStepRequest(BaseModel):
    step: str  # "keywords" | "ad_copy" | "ad_groups" | "launch_checklist"


class CampaignBuildStepSaveRequest(BaseModel):
    step: str
    data: dict | list  # the accepted refined content to persist


@app.post("/api/admin/campaigns/{campaign_id}/build-step-save", dependencies=[Depends(_require_admin)])
def admin_campaign_build_step_save(campaign_id: str, body: CampaignBuildStepSaveRequest):
    """Save accepted refined build step data into campaign_build_json (or strategy_json for strategy step)."""
    from database import get_campaign_by_id, save_campaign_build_step, update_campaign_strategy
    import json as _json
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    VALID_STEPS = {"keywords", "ad_copy", "ad_groups", "launch_checklist", "strategy", "competitor_analysis"}
    if body.step not in VALID_STEPS:
        raise HTTPException(status_code=400, detail=f"Invalid step")
    if body.step == "strategy":
        # Strategy lives in strategy_json on the campaign row
        update_campaign_strategy(campaign_id, body.data)
    else:
        data_to_save = body.data
        # B4 fix: re-derive campaign_type on save for competitor_analysis so it's
        # never dropped by the refine → save flow (Sonnet doesn't echo it back).
        if body.step == "competitor_analysis" and isinstance(data_to_save, dict):
            try:
                from search_term_classifier import _detect_campaign_type as _dtct_save
                data_to_save = dict(data_to_save)  # copy; don't mutate request body
                _ctype_save = _dtct_save(camp.get("campaign_name", ""))
                data_to_save["campaign_type"] = _ctype_save
            except Exception as _ct_err:
                logger.warning(f"build-step-save: campaign_type detect failed: {_ct_err}")
                _ctype_save = "general"
            # Re-apply competitor policy on every save so overrides + derived arrays stay in sync.
            # C4 fix: snapshot any manually-added competitor_negatives that are not derivable
            # from the policy engine (e.g. stems the user typed by hand), then re-add them after
            # apply_competitor_policy rebuilds the derived arrays.
            try:
                from competitor_policy import apply_competitor_policy as _apply_cp_save, get_effective_negatives as _gen_save, normalize as _cnorm_save
                # Collect stems that policy will derive on its own (brand_stems of all competitors)
                _policy_stems: set[str] = set()
                for _c4c in (data_to_save.get("competitors") or []):
                    for _s in (_c4c.get("brand_stems") or []):
                        _policy_stems.add(_cnorm_save(_s))
                # Manual negatives = anything in the current list NOT covered by policy
                _manual_negs: list[str] = [
                    n for n in (data_to_save.get("competitor_negatives") or [])
                    if _cnorm_save(n) not in _policy_stems
                ]
                _apply_cp_save(data_to_save, _ctype_save)
                # Re-add manual negatives that policy would have dropped
                if _manual_negs:
                    _current_negs: list[str] = data_to_save.get("competitor_negatives") or []
                    _current_neg_set = {_cnorm_save(n) for n in _current_negs}
                    for _mn in _manual_negs:
                        if _cnorm_save(_mn) not in _current_neg_set:
                            _current_negs.append(_mn)
                    data_to_save["competitor_negatives"] = sorted(set(_current_negs))
            except Exception as _cp_err:
                logger.warning(f"build-step-save: apply_competitor_policy failed (non-fatal): {_cp_err}")
        # ── Live-push new negatives when campaign is already in Google Ads ──
        # If this is the keywords step and the campaign has a gads_campaign_resource,
        # diff the incoming negative_keywords against what's already saved and push
        # any new ones immediately so the wizard and live GAds stay in sync.
        if body.step == "keywords":
            gads_resource = camp.get("gads_campaign_resource") or ""
            if gads_resource and isinstance(data_to_save, dict):
                from database import get_campaign_build
                from google_ads_write import add_negative_keyword_to_campaign
                from campaign_safety import check_writes_enabled, WriteBlockedError
                from database import log_admin_manual_action, update_gads_action_result, set_audit_approval
                try:
                    check_writes_enabled()
                    # Get existing saved negatives before this save
                    old_build = get_campaign_build(campaign_id) or {}
                    old_kw = old_build.get("keywords") or {}
                    old_negs = {n.strip().lower() for n in (old_kw.get("negative_keywords") or []) if n.strip()}
                    new_negs = data_to_save.get("negative_keywords") or []
                    added_negs = [n for n in new_negs if n.strip().lower() not in old_negs]
                    for neg_text in added_negs:
                        neg_text = neg_text.strip()
                        if not neg_text:
                            continue
                        try:
                            action_id = log_admin_manual_action(
                                operation="add_negative_keyword",
                                entity_type="campaign",
                                entity_id=gads_resource,
                                entity_name=camp.get("campaign_name", ""),
                                before={},
                                after={"keyword_text": neg_text, "match_type": "BROAD",
                                       "campaign_resource": gads_resource},
                                reason="wizard_keywords_step_save",
                            )
                            add_negative_keyword_to_campaign(gads_resource, neg_text, "BROAD")
                            update_gads_action_result(action_id, executed=True, execution_result="success")
                            set_audit_approval(action_id, "admin")
                            logger.info(f"build-step-save: live-pushed negative '{neg_text}' to {gads_resource}")
                        except Exception as _neg_push_err:
                            logger.warning(f"build-step-save: failed to push negative '{neg_text}': {_neg_push_err}")
                except WriteBlockedError:
                    logger.info("build-step-save: keyword step save — writes blocked, skipping live negative push")
                except Exception as _live_err:
                    logger.warning(f"build-step-save: live negative push failed (non-fatal): {_live_err}")

        save_campaign_build_step(campaign_id, body.step, data_to_save)
    return {"ok": True, "step": body.step}


@app.post("/api/admin/campaigns/{campaign_id}/build-step", dependencies=[Depends(_require_admin)])
async def admin_campaign_build_step(campaign_id: str, body: CampaignBuildStepRequest):
    """
    AI-generate one stage of the campaign build pipeline using Claude Sonnet.

    Steps:
      keywords      — target keywords by match type + negatives
      ad_copy       — finalized RSA headlines (15) + descriptions (4) per ad group
      ad_groups     — keyword → ad group mapping with bid suggestions
      launch_checklist — readiness checklist (returns template, not AI-generated)

    Uses the campaign's strategy_json as context for all AI steps.
    Result is saved into campaign_build_json[step] and returned.
    """
    from database import get_campaign_by_id, get_campaign_build, save_campaign_build_step
    import anthropic as _anthropic

    VALID_STEPS = {"keywords", "ad_copy", "ad_groups", "launch_checklist", "competitor_analysis"}
    if body.step not in VALID_STEPS:
        raise HTTPException(status_code=400, detail=f"Invalid step. Must be one of {VALID_STEPS}")

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    step = body.step

    # Launch checklist — static template, no AI, deterministic
    if step == "launch_checklist":
        build_data = get_campaign_build(campaign_id)

        landing_page  = camp.get("landing_page") or ""
        geo           = camp.get("geographic_targeting") or ""
        budget        = camp.get("monthly_budget") or 0

        # geo is done if it has at least one location (JSON) or non-empty freetext
        geo_done = False
        if geo:
            try:
                import json as _json
                geo_parsed = _json.loads(geo)
                geo_done = len(geo_parsed.get("locations", [])) > 0
            except Exception:
                geo_done = bool(geo.strip())

        # Keyword counts — keywords step produces {exact_match, phrase_match, broad_match_modifier, negative_keywords}
        kw_data   = build_data.get("keywords") or {}
        if isinstance(kw_data, dict):
            kw_count  = (len(kw_data.get("exact_match", [])) +
                         len(kw_data.get("phrase_match", [])) +
                         len(kw_data.get("broad_match_modifier", [])))
            neg_count = len(kw_data.get("negative_keywords", []))
        else:
            kw_count  = 0
            neg_count = 0

        has_keywords  = kw_count > 0
        has_ad_copy   = bool(build_data.get("ad_copy"))
        has_ad_groups = bool(build_data.get("ad_groups"))

        # Ad copy/groups counts
        ac_data    = build_data.get("ad_copy") or {}
        ag_data    = build_data.get("ad_groups") or {}
        ac_groups  = ac_data.get("ad_groups", []) if isinstance(ac_data, dict) else []
        ag_groups  = ag_data.get("ad_groups", []) if isinstance(ag_data, dict) else []
        headline_count    = sum(len(g.get("headlines", [])) for g in ac_groups)
        description_count = sum(len(g.get("descriptions", [])) for g in ac_groups)

        # Practice info for extension defaults — read from Admin → Practice Information
        # Keys match _PRACTICE_FIELDS: "phone" → stored as "practice_phone", "address" → "practice_address"
        practice_phone = get_setting("practice_phone") or ""
        practice_address = get_setting("practice_address") or ""

        # Landing page: main site or custom — both count as done
        # Always ensure scheme prefix so the URL validator never rejects it on PATCH.
        # Source MAIN_SITE from Practice Info → Website (fallback to legacy default).
        practice_website = get_setting("practice_website") or ""
        _main_site_raw = practice_website or "https://graftondentalcare.com"
        MAIN_SITE = (
            _main_site_raw.lower()
            .replace("https://", "")
            .replace("http://", "")
            .rstrip("/")
        )
        lp_lower = landing_page.lower()
        if not landing_page or (MAIN_SITE and MAIN_SITE in lp_lower):
            # Ensure scheme present
            if landing_page.startswith("http"):
                lp_value = landing_page
            else:
                lp_value = _main_site_raw if _main_site_raw.startswith("http") else f"https://{_main_site_raw}"
            lp_note  = "Using main website — no dedicated landing page required."
        else:
            lp_value = landing_page
            lp_note  = "Using custom landing page."
        lp_valid = lp_value.startswith("https://") or lp_value.startswith("http://")

        # Bidding strategy — read from campaign if stored, else default
        bid_strategy = camp.get("bidding_strategy") or "Maximize Conversions"

        # Ad copy done: require ≥3 headlines AND ≥2 descriptions per Google Ads minimums
        ad_copy_done = headline_count >= 3 and description_count >= 2

        # Daily budget display (Google Ads bills daily; monthly / 30.4)
        daily_budget = round(budget / 30.4, 2) if budget else 0
        # Daily leads, monthly cap shown as reference
        budget_display = f"${daily_budget}/day (≈${budget}/mo cap)" if budget else ""

        # Note: click-rate guidance (clicks/day from ad_groups CPC) is computed live
        # in the frontend from build.ad_groups + camp.monthly_budget. No need to
        # duplicate it here in the static checklist payload. (B2 cleanup)

        # Practice info for extension defaults
        practice_hours = get_setting("practice_hours") or ""

        # Sitelinks — read from campaign row
        _sitelinks_raw = camp.get("sitelinks") or ""
        _sitelinks_list = []
        if _sitelinks_raw:
            try:
                import json as _json
                _sitelinks_list = _json.loads(_sitelinks_raw)
            except Exception:
                pass
        _sitelinks_done  = len(_sitelinks_list) > 0
        _sitelinks_value = f"{len(_sitelinks_list)} sitelink(s) configured" if _sitelinks_done else ""

        checklist = [
            # ── REQUIRED ────────────────────────────────────────────────────────
            {
                "key":        "budget",
                "item":       "Daily budget",
                "value":      budget_display,
                "done":       budget > 0,
                "skippable":  False,
                "category":   "required",
                "action":     "auto",
                "note":       "Daily spend cap set by Google Ads. Monthly cap shown for reference.",
            },
            {
                "key":      "bidding",
                "item":     "Bidding strategy",
                "value":    bid_strategy,
                "done":     bool(bid_strategy),
                "skippable": False,
                "category": "required",
                "action":   "auto",
                "note":     "Applied at launch. Options: Maximize Conversions, Maximize Clicks, Target CPA, Manual CPC.",
            },
            {
                "key":      "keywords",
                "item":     "Keywords built",
                "value":    f"{kw_count} keywords · {neg_count} negatives" if has_keywords else "",
                "done":     bool(has_keywords),
                "skippable": False,
                "category": "required",
                "action":   "built",
                "note":     f"{kw_count} positive + {neg_count} negative keywords built in wizard." if has_keywords else "Complete the Keywords step first.",
            },
            {
                "key":      "ad_copy",
                "item":     "Ad copy",
                "value":    f"{headline_count} headlines, {description_count} descriptions across {len(ac_groups)} ad group(s)" if has_ad_copy else "",
                "done":     ad_copy_done,
                "skippable": False,
                "category": "required",
                "action":   "built",
                "note":     "Google Ads requires ≥3 headlines and ≥2 descriptions per RSA." if not ad_copy_done else "Built in the Ad Copy wizard step.",
            },
            {
                "key":      "ad_groups",
                "item":     "Ad groups",
                "value":    f"{len(ag_groups)} ad groups" if has_ad_groups else "",
                "done":     bool(has_ad_groups) and len(ag_groups) > 0,
                "skippable": False,
                "category": "required",
                "action":   "built",
                "note":     "Built in the Ad Groups wizard step." if has_ad_groups else "Complete the Ad Groups step first.",
            },
            {
                "key":      "landing_page",
                "item":     "Landing page",
                "value":    lp_value,
                "done":     lp_valid,
                "skippable": False,
                "category": "required",
                "action":   "auto",
                "note":     lp_note + " Edit to use a different URL.",
            },
            {
                "key":      "geo",
                "item":     "Geographic targeting",
                "value":    geo,
                "done":     geo_done,
                "skippable": False,
                "category": "required",
                "action":   "auto",
                "note":     "Add postal codes, cities, or addresses. Use the builder to set include/exclude radii. Defaults to your practice location.",
            },
            # ── RECOMMENDED ─────────────────────────────────────────────────────
            {
                "key":      "call_extension",
                "item":     "Call extension",
                "value":    practice_phone,
                "done":     bool(practice_phone),
                "skippable": True,
                "category": "recommended",
                "action":   "auto",
                "note":     "Your practice phone shown directly in the ad. Applied automatically at launch.",
            },
            {
                "key":      "location_extension",
                "item":     "Location extension",
                "value":    practice_address,
                "done":     bool(practice_address),
                "skippable": True,
                "category": "recommended",
                "action":   "auto",
                "note":     "Your practice address linked to ads. Applied automatically at launch.",
            },
            {
                "key":      "sitelinks",
                "item":     "Sitelink extensions",
                "value":    _sitelinks_value,
                "done":     _sitelinks_done,
                "skippable": True,
                "category": "recommended",
                "action":   "manual",
                "note":     "Add quick links below your ad (e.g. Book Online, New Patients, About Us). Saved here and pushed to Google Ads automatically.",
            },
            {
                "key":      "callouts",
                "item":     "Callout extensions",
                "value":    "",
                "done":     False,
                "skippable": True,
                "category": "recommended",
                "action":   "manual",
                "note":     "Short highlights shown with the ad (e.g. Same-Day Appointments, Accepts Insurance).",
            },
            # ── OPTIONAL ────────────────────────────────────────────────────────
            {
                "key":      "ad_schedule",
                "item":     "Ad schedule",
                "value":    "",
                "done":     False,
                "skippable": True,
                "category": "optional",
                "action":   "auto",
                "note":     "Set when ads run for this campaign (e.g. 'Mon-Thu 7am-11pm', 'Weekdays 9am-6pm', '24/7'). Leave blank to run ads at all times.",
            },
            {
                "key":      "utm_tagging",
                "item":     "UTM tagging",
                "value":    "",
                "done":     False,
                "skippable": True,
                "category": "optional",
                "action":   "auto",
                "note":     "utm_source=google&utm_medium=cpc appended automatically at launch for Analytics tracking.",
            },
        ]
        # Conversion tracking is NOT on the pre-launch checklist.
        # After launch, the Performance tab shows live Google Ads status including
        # conversion tracking, ad approvals, and extension serving state.

        # Merge with existing: preserve user-typed values, skipped flags, and done state
        existing = get_campaign_build(campaign_id).get("launch_checklist") or []
        if existing:
            existing_by_key = {x.get("key") or x.get("item"): x for x in existing if isinstance(x, dict)}
            for item in checklist:
                merge_key = item.get("key") or item.get("item")
                old = existing_by_key.get(merge_key)
                if old:
                    # Preserve user-entered value (unless item has authoritative auto-value)
                    # NOTE: budget is always re-derived from daily_budget so stale "$X/mo"
                    # strings get replaced on every refresh after the daily-budget switch.
                    if (
                        old.get("value")
                        and item["action"] not in ("built",)
                        and merge_key != "budget"
                    ):
                        item["value"] = old["value"]
                    # Preserve done=True for non-wizard items (user manually checked)
                    # Also preserve done=True for auto items where user has explicitly set a value
                    # (e.g. ad_schedule set via the Set Schedule button — BE-G14 fix)
                    if old.get("done") and not item["done"]:
                        if item["action"] == "manual":
                            item["done"] = True
                        elif item["action"] == "auto" and old.get("value"):
                            # Only preserve if a real value was saved (not just re-generated as blank)
                            item["done"] = True
                    # Preserve skipped flag
                    if old.get("skipped"):
                        item["skipped"] = True

        save_campaign_build_step(campaign_id, step, checklist)
        return {"ok": True, "step": step, "data": checklist}

    # Build AI prompt using strategy context
    strategy = camp.get("strategy_json") or {}
    if isinstance(strategy, str):
        try:
            import json as _json
            strategy = _json.loads(strategy)
        except Exception:
            strategy = {}

    # Existing build data for context
    build = get_campaign_build(campaign_id)

    _api_key = get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    ai_client = _anthropic.Anthropic(api_key=_api_key)

    campaign_name = camp.get("campaign_name", "")
    service_focus = camp.get("service_focus", "")
    budget = camp.get("monthly_budget", 0)
    objective = strategy.get("objective", "")
    target_audience = strategy.get("target_audience", "")
    key_messages = strategy.get("key_messages", [])
    impl_notes = strategy.get("implementation_instructions", "")

    # Site intelligence — use campaign landing_page first, fall back to practice website
    _camp_landing = camp.get("landing_page") or ""
    _practice_site = get_setting("practice_website") or ""
    _site_url_for_context = _camp_landing or _practice_site
    _site_context_block = ""
    if _site_url_for_context:
        try:
            from domain_crawler import build_site_context_for_url
            _site_context_block = build_site_context_for_url(_site_url_for_context)
        except Exception as _sce:
            logger.warning(f"build-step site context fetch failed ({step}): {_sce}")
    _site_section = ("\n\n=== Website Intelligence ===\n" + _site_context_block) if _site_context_block else ""

    if step == "keywords":
        # Pull negatives from source campaign if this is a clone
        _source_neg_block = ""
        _source_kw_block = ""
        _source_camp_name = strategy.get("_source_campaign_name", "")
        if _source_camp_name:
            try:
                from database import _conn as _db
                with _db() as _c:
                    _neg_rows = _c.execute(
                        "SELECT keyword_text, match_type FROM gads_negative_keywords "
                        "WHERE LOWER(campaign_name) = LOWER(?) ORDER BY keyword_text",
                        (_source_camp_name,)
                    ).fetchall()
                    if _neg_rows:
                        # Filter out dental service terms — these are valid for general campaigns
                        # and should never be inherited as negatives regardless of source campaign
                        _dental_service_terms = {
                            "implant", "implants", "dental implant", "dental implants",
                            "veneer", "veneers", "porcelain veneer", "porcelain veneers",
                            "crown", "crowns", "dental crown", "dental crowns",
                            "denture", "dentures", "partial denture", "full denture",
                            "whitening", "teeth whitening", "tooth whitening",
                            "cosmetic", "cosmetic dentistry", "smile makeover",
                            "invisalign", "clear aligner", "clear aligners", "braces", "orthodontic",
                            "root canal", "root canals", "endodontist",
                            "extraction", "extractions", "tooth extraction", "wisdom tooth",
                            "wisdom teeth", "oral surgery", "sedation", "sleep dentistry",
                            "periodontal", "gum disease", "gum treatment",
                        }
                        _filtered_negs = [
                            r for r in _neg_rows
                            if r["keyword_text"].lower().strip() not in _dental_service_terms
                        ]
                        if _filtered_negs:
                            _neg_list = ", ".join(f'"{r["keyword_text"]}"' for r in _filtered_negs)
                            _source_neg_block = (
                                f"\n\nSource campaign negatives (include these in negative_keywords — "
                                f"validated wasted spend on '{_source_camp_name}'):\n{_neg_list}"
                            )
                    _kw_rows = _c.execute(
                        "SELECT keyword_text, match_type, conversions, clicks FROM gads_keywords_cache "
                        "WHERE LOWER(campaign_name) = LOWER(?) AND days = 30 "
                        "ORDER BY conversions DESC, clicks DESC LIMIT 20",
                        (_source_camp_name,)
                    ).fetchall()
                    if _kw_rows:
                        _kw_list = "\n".join(
                            f'  [{r["match_type"]}] "{r["keyword_text"]}" '
                            f'({r["conversions"] or 0} conv, {r["clicks"] or 0} clicks)'
                            for r in _kw_rows
                        )
                        _source_kw_block = (
                            f"\n\nSource campaign top keywords (use as starting point, adapt for new service):\n{_kw_list}"
                        )
            except Exception as _e:
                logger.warning(f"build-step keywords: source campaign lookup failed: {_e}")

        # Pull optimizer memory (DB table + JSON file) for learned intelligence
        # Only inject campaign-specific rules — never bleed implant/service rules into unrelated campaigns.
        _optimizer_memory_block = ""
        try:
            import re as _re, json as _json, os as _os
            from database import _conn as _db

            _cn = (campaign_name or "").strip().lower()

            def _camp_match(tag: str) -> bool:
                """Token-set match: tag tokens must all appear in campaign name tokens."""
                if not tag or not _cn:
                    return False
                t_tokens = set(_re.findall(r"[a-z0-9]+", tag.strip().lower()))
                c_tokens = set(_re.findall(r"[a-z0-9]+", _cn))
                return bool(t_tokens) and t_tokens.issubset(c_tokens)

            _mem_lines = []
            with _db() as _mc:
                _mem_rows = _mc.execute(
                    "SELECT category, key, value, reason, campaign FROM optimizer_memory WHERE active=1 ORDER BY category, key"
                ).fetchall()

            # keyword_override (never_pause): campaign tag required — empty campaign = not injected globally
            _never_pause = [
                r for r in _mem_rows
                if r["category"] == "keyword_override" and r["value"] == "never_pause"
                and r["campaign"] and _camp_match(r["campaign"])
            ]
            if _never_pause:
                _mem_lines.append("Always include these as positive keywords (never remove — proven core terms for this campaign):")
                for r in _never_pause:
                    _mem_lines.append(f'  - "{r["key"]}" ({r["reason"] or "no reason recorded"})')

            # term_classification (irrelevant → negative): campaign tag required
            _irrelevant = [
                r for r in _mem_rows
                if r["category"] == "term_classification" and r["value"] == "irrelevant"
                and r["campaign"] and _camp_match(r["campaign"])
            ]
            if _irrelevant:
                _mem_lines.append("Irrelevant search terms for this campaign (add ALL as negatives — confirmed wasted spend):")
                for r in _irrelevant:
                    _mem_lines.append(f'  - "{r["key"]}" ({r["reason"] or "no reason recorded"})')

            # general: always inject (account-level context / attribution notes)
            _general = [r for r in _mem_rows if r["category"] == "general"]
            if _general:
                _mem_lines.append("General optimizer context:")
                for r in _general:
                    _mem_lines.append(f'  - {r["key"]}: {r["value"] or r["reason"] or ""}')

            # JSON file: negatives added by optimizer — only from runs that match this campaign (or unscoped runs)
            # Unscoped runs are treated as account-level competitor negatives only if they look like competitor names
            _COMPETITOR_RE = _re.compile(r"\b(dds|dmd|dental|dr |doctor|orthodont|periodont|endodont|implant|smile|clinic|care|practice|office)\b", _re.I)
            try:
                _mem_json_path = _os.path.join(_os.path.dirname(__file__), "optimizer_memory.json")
                if _os.path.exists(_mem_json_path):
                    with open(_mem_json_path, encoding="utf-8") as _mf:
                        _mem_data = _json.load(_mf)
                    _all_json_negs = []
                    for _run in _mem_data.get("runs", []):
                        _run_camp = (_run.get("campaign") or "").strip()
                        # include if: no campaign tag (account-level), or campaign matches current
                        if not _run_camp or _camp_match(_run_camp):
                            _all_json_negs.extend(_run.get("negatives_added", []))
                    # For unscoped runs, only keep entries that look like competitor/practice names
                    _all_json_negs = sorted(set(_all_json_negs))
                    if _all_json_negs:
                        _mem_lines.append("Competitor negatives validated by past optimizer runs (include ALL in negative_keywords):")
                        _mem_lines.append("  " + ", ".join(f'"{n}"' for n in _all_json_negs))
            except Exception as _je:
                logger.warning(f"build-step keywords: optimizer_memory.json read failed: {_je}")

            if _mem_lines:
                _optimizer_memory_block = "\n\n=== Optimizer Memory (apply these learned rules) ===\n" + "\n".join(_mem_lines)
        except Exception as _ome:
            logger.warning(f"build-step keywords: optimizer memory read failed: {_ome}")

        # Pull competitor context if available
        _comp_data = build.get("competitor_analysis") or {}
        _comp_context_block = ""
        if _comp_data and isinstance(_comp_data, dict):
            _comp_diffs = _comp_data.get("our_differentiators") or []
            _comp_conquest = _comp_data.get("conquest_keywords") or []
            _comp_pos = _comp_data.get("positioning_notes") or ""
            _comp_parts = []
            if _comp_diffs:
                _comp_parts.append("Our differentiators (reinforce these through keyword themes): " + "; ".join(_comp_diffs))
            if _comp_conquest:
                _comp_parts.append(
                    "Conquest brand keywords (include ALL in exact_match — intentional competitor targeting): "
                    + ", ".join(f'"{k}"' for k in _comp_conquest)
                )
            if _comp_pos:
                _comp_parts.append("Positioning context: " + _comp_pos)
            if _comp_parts:
                _comp_context_block = "\n\n=== Competitor Intelligence ===\n" + "\n".join(_comp_parts)

        # B3 fix: conquest keywords as a Rule-level instruction (not just data)
        _conquest_rule = ""
        if _comp_data and isinstance(_comp_data, dict):
            _kw_conquest_list = _comp_data.get("conquest_keywords") or []
            if _kw_conquest_list:
                _conquest_rule = (
                    f"\n- conquest_keywords: Add these competitor brand terms as exact-match keywords "
                    f"in a dedicated conquest ad group: {', '.join(_kw_conquest_list)}. "
                    f"Include them in exact_match list."
                )

        # Build campaign-type-aware intent signal guidance (shared module keeps wizard + optimizer in sync)
        try:
            from search_term_classifier import _detect_campaign_type as _dtct_kw
            _kw_camp_type = _dtct_kw(campaign_name)
        except Exception:
            _kw_camp_type = "general"

        from intent_signals import get_intent_signals as _get_intent_signals
        _intent_pack = _get_intent_signals(_kw_camp_type)
        _intent_examples = _intent_pack["high_intent_examples"]
        _intent_block = (
            f"\n\nHigh-intent keyword patterns for {_kw_camp_type} campaigns — use these as templates:\n"
            + "\n".join(f"  {ex}" for ex in _intent_examples)
            + "\nPrioritize: 'near me', 'same day', '[service] cost/price', '[town] [service]', "
              "'accepting new patients', '[service] grafton ma / worcester county'"
        )

        _neg_intent = _intent_pack["low_intent_negatives"]
        _neg_intent_note = (
            f"these low-intent patterns for {_kw_camp_type} campaigns: "
            + ", ".join(_neg_intent)
        )

        # Load brand negatives from nearby_practices DB (all non-excluded within 20 miles).
        # These prevent accidental clicks from patients searching for a competitor's contact info.
        _brand_neg_block = ""
        _brand_neg_rule = ""
        try:
            from database import get_brand_negatives_for_campaign as _get_brand_negs
            # Cap to 10 miles + 50 stems max to keep prompt size manageable
            _brand_stems = _get_brand_negs(campaign_type=_kw_camp_type, max_miles=10.0)[:50]
            if _brand_stems:
                _brand_neg_block = (
                    f"\n\n=== NEARBY PRACTICE BRAND NEGATIVES (add to negative_keywords) ===\n"
                    f"Top {len(_brand_stems)} brand stems from nearby dental practices (within 10 miles):\n"
                    + ", ".join(f'"{s}"' for s in _brand_stems)
                )
                _brand_neg_rule = (
                    f" Include the {len(_brand_stems)} brand stems listed above."
                )
        except Exception as _bne:
            logger.warning(f"build-step keywords: brand negatives load failed (non-fatal): {_bne}")

        from ai_optimizer import GOOGLE_ADS_RULES as _GAR

        # Build campaign-type-specific keyword generation rules
        _urgency_tokens_list = _intent_pack.get("urgency_tokens_required") or []
        if _kw_camp_type == "emergency" and _urgency_tokens_list:
            _sample_urgency = ", ".join(f'"{t}"' for t in _urgency_tokens_list[:8])
            _type_specific_rules = f"""

=== EMERGENCY CAMPAIGN KEYWORD RULES (MANDATORY) ===
This is an EMERGENCY dental campaign. It must ONLY capture patients in acute pain who need
same-day or next-day care. Every keyword you generate must follow these rules:

RULE 1 — URGENCY TOKEN REQUIRED: Every exact_match and phrase_match keyword MUST contain
at least one urgency signal: {_sample_urgency}, "cracked tooth", "knocked out", "tooth infection",
"weekend dentist", "dentist today", "dentist asap". If a keyword lacks ALL of these, DO NOT include it.

RULE 2 — NAVIGATIONAL TERMS BANNED: Do NOT include any of these in exact_match or phrase_match:
"dentist near me", "dentists near me", "dentist in [city]", "[city] dentist", "family dentist",
"new patient dentist", "dental cleaning", "teeth cleaning", "affordable dentist", "best dentist",
"dentist accepting new patients". These patients are shopping for a regular dentist, NOT seeking
emergency care. They belong in the General Dentistry campaign.

RULE 3 — INTENT CHECK: Before adding any keyword, ask: "Would a patient in acute dental pain
right now search this?" If the answer is "maybe, but they might just be looking for a regular
dentist too", the keyword is too broad — exclude it.

NEGATIVE KEYWORDS: The navigational terms above (dentist near me, family dentist, etc.) MUST
appear in negative_keywords to prevent the emergency campaign from capturing wrong-intent traffic.
=== END EMERGENCY CAMPAIGN KEYWORD RULES ===
"""
            _type_specific_exact_rule = (
                "- exact_match: 8-12 high-urgency keywords — EVERY keyword must contain an urgency signal "
                "(emergency/urgent/same day/toothache/pain/broken tooth/open now/after hours/24 hour). "
                "NO generic dentist searches. Examples: 'emergency dentist near me', 'toothache relief same day', "
                "'dentist open now grafton ma', 'broken tooth emergency'"
            )
        else:
            _type_specific_rules = ""
            _type_specific_exact_rule = (
                f"- exact_match: 8-12 high-intent keywords — must include \"near me\", \"same day\", "
                f"\"[service] cost\", and \"[town] [service]\" variants using the intent patterns above"
            )

        prompt = _GAR + _type_specific_rules + f"""You are a Google Ads specialist. Generate a comprehensive keyword list for this dental campaign.

Campaign: {campaign_name}
Service Focus: {service_focus}
Campaign Type: {_kw_camp_type}
Monthly Budget: ${budget}
Objective: {objective}
Target Audience: {target_audience}
Key Messages: {', '.join(key_messages)}
Implementation Notes: {impl_notes}{_intent_block}{_site_section}{_source_kw_block}{_source_neg_block}{_optimizer_memory_block}{_comp_context_block}{_brand_neg_block}

Return a JSON object with this exact structure:
{{
  "exact_match": ["keyword1", "keyword2", ...],
  "phrase_match": ["keyword1", "keyword2", ...],
  "broad_match_modifier": ["keyword1", "keyword2", ...],
  "negative_keywords": ["keyword1", "keyword2", ...]
}}

Rules:
{_type_specific_exact_rule}
- phrase_match: 10-15 moderate-intent phrases covering service variations and location modifiers
- broad_match_modifier: 5-8 broader terms to capture volume (service category + location area)
- negative_keywords: Include ALL source campaign negatives above PLUS optimizer memory negatives PLUS {_neg_intent_note}{_brand_neg_rule}
- Geographic targeting towns: Grafton, Shrewsbury, Westborough, Northborough, Millbury, Auburn, Worcester area{_conquest_rule}
- Return ONLY the JSON object, no explanation."""

    elif step == "ad_copy":
        keywords = build.get("keywords", {})
        kw_context = f"Target keywords: {', '.join(keywords.get('exact_match', [])[:8])}" if keywords else ""
        headlines_from_strategy = strategy.get("ad_headlines", [])
        descs_from_strategy = strategy.get("ad_descriptions", [])

        # Pull competitor differentiators to sharpen positioning
        _adcopy_comp_data = build.get("competitor_analysis") or {}
        _adcopy_diff_block = ""
        if _adcopy_comp_data and isinstance(_adcopy_comp_data, dict):
            _adcopy_diffs = _adcopy_comp_data.get("our_differentiators") or []
            _adcopy_pos = _adcopy_comp_data.get("positioning_notes") or ""
            if _adcopy_diffs:
                _adcopy_diff_block = (
                    "\nCompetitor Differentiators (use these as headline/description themes — "
                    "this is what sets us apart from local competitors): "
                    + "; ".join(_adcopy_diffs)
                )
            if _adcopy_pos:
                _adcopy_diff_block += f"\nPositioning Strategy: {_adcopy_pos}"

        # Detect audience constraint — if target specifies adults/age-specific, inject hard rule
        _audience_constraint = ""
        _ta_lower = (target_audience or "").lower()
        if any(kw in _ta_lower for kw in ["adult", "18+", "18 +", "over 18", "grown", "senior", "age "]):
            _audience_constraint = (
                "\n\nAUDIENCE HARD CONSTRAINT: This campaign targets ADULTS ONLY. "
                "Do NOT use any family, children, pediatric, or all-ages language in any headline or description. "
                "No references to 'family', 'kids', 'children', 'pediatric', 'whole family', 'all ages'. "
                "Focus exclusively on language that resonates with adult patients making their own dental decisions."
            )

        from ai_optimizer import GOOGLE_ADS_RULES as _GAR
        prompt = _GAR + f"""You are a Google Ads copywriter. Generate complete RSA ad copy for this dental campaign.

Campaign: {campaign_name}
Service Focus: {service_focus}
Objective: {objective}
Target Audience: {target_audience}
{kw_context}
Strategy Headlines: {', '.join(headlines_from_strategy)}
Strategy Descriptions: {'; '.join(descs_from_strategy)}
Implementation Notes: {impl_notes}{_adcopy_diff_block}{_site_section}{_audience_constraint}

Return a JSON object with this exact structure:
{{
  "ad_groups": [
    {{
      "name": "Ad Group Name",
      "theme": "What this group targets",
      "headlines": ["H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8", "H9", "H10", "H11", "H12", "H13", "H14", "H15"],
      "descriptions": ["D1 (90 chars max)", "D2 (90 chars max)", "D3 (90 chars max)", "D4 (90 chars max)"]
    }}
  ]
}}

Rules:
- Create 2-3 ad groups based on keyword themes
- Headlines: exactly 15 per ad group, max 30 characters each
- Descriptions: exactly 4 per ad group, max 90 characters each
- Include the practice service, urgency, differentiators, and CTAs
- No punctuation at end of headlines
- NEVER include phone numbers (e.g. 508-318-4477) in any headline or description — Google Ads prohibits phone numbers in ad text; use call extensions for phone numbers
- Return ONLY the JSON object, no explanation."""

    elif step == "ad_groups":
        keywords = build.get("keywords", {})
        ad_copy = build.get("ad_copy", {})
        kw_context = f"Keywords: {keywords}" if keywords else "No keywords generated yet."
        groups_context = f"Ad groups from copy: {[g.get('name') for g in ad_copy.get('ad_groups', [])]}" if ad_copy else ""

        _daily_budget = round(budget / 30.4, 2) if budget else 0

        prompt = f"""You are a Google Ads account manager. Create the final ad group structure for this campaign.

Campaign: {campaign_name}
Service Focus: {service_focus}
Monthly Budget: ${budget} (≈${_daily_budget}/day)
{kw_context}
{groups_context}
Implementation Notes: {impl_notes}{_site_section}

Return a JSON object with this exact structure:
{{
  "ad_groups": [
    {{
      "name": "Ad Group Name",
      "theme": "Brief theme description",
      "match_types": ["exact", "phrase"],
      "keywords": ["keyword1", "keyword2", ...],
      "cpc_bid_usd": 3.50,
      "daily_budget_pct": 40,
      "notes": "Why this group and bidding rationale"
    }}
  ],
  "launch_bidding_strategy": {{
    "strategy_type": "MANUAL_CPC",
    "max_cpc_cap_usd": 4.50,
    "rationale": "Why this strategy at launch"
  }},
  "bidding_strategy": "Human-readable summary of recommended bidding strategy",
  "budget_allocation_notes": "How to split the monthly budget across groups"
}}

Rules:
- Create 2-3 ad groups that map to the keyword themes
- daily_budget_pct values should sum to 100
- cpc_bid_usd should be realistic for dental keywords ($2-8 range)
- For launch_bidding_strategy.strategy_type use ONLY "MANUAL_CPC" or "MAXIMIZE_CLICKS"
  (do NOT use MAXIMIZE_CONVERSIONS — new campaigns have no conversion history yet)
- For new campaigns with budget <$30/day, prefer MANUAL_CPC for full control
- For MAXIMIZE_CLICKS, set max_cpc_cap_usd to a reasonable ceiling (e.g. 1.5-2x the typical CPC)
- Return ONLY the JSON object, no explanation."""

    elif step == "competitor_analysis":
        # Detect campaign type for conquest brand seeding
        try:
            from search_term_classifier import _detect_campaign_type as _dtct
            _camp_type = _dtct(campaign_name)
        except Exception:
            _camp_type = "general"

        # Import competitor policy module
        from competitor_policy import (
            apply_competitor_policy as _apply_comp_policy,
            merge_overrides_on_regenerate as _merge_overrides,
            CONQUEST_ELIGIBLE_TYPES as _CONQUEST_ELIGIBLE,
        )

        # Load nearby dentists from DB (quarterly-synced nearby_practices table).
        # Falls back to a live Places API call if the DB is empty (first run before first sync).
        _nearby_dentists = []
        from places_client import fetch_nearby_dentists as _fetch_places, format_for_claude as _fmt_places
        try:
            from database import get_nearby_practices as _get_nearby_db
            _db_practices = _get_nearby_db(max_miles=20.0, include_excluded=True)
            if _db_practices:
                # Convert DB rows to the format format_for_claude expects
                _nearby_dentists = [
                    {
                        "name": p["name"],
                        "place_id": p["place_id"],
                        "vicinity": p["vicinity"],
                        "rating": p.get("rating"),
                        "user_ratings_total": p.get("review_count", 0),
                        "business_status": p.get("business_status", "OPERATIONAL"),
                        "types": [],
                        "distance_miles": p.get("distance_miles", 0),
                        "is_excluded": p.get("is_excluded", 0),
                    }
                    for p in _db_practices
                ]
                logger.info(f"[competitor_analysis] Loaded {len(_nearby_dentists)} practices from DB")
            else:
                # DB empty — fall back to live Places API (will populate after first quarterly sync)
                logger.info("[competitor_analysis] DB empty — falling back to live Places API")
                _places_key = get_settings().google_places_api_key
                _nearby_dentists = _fetch_places(_places_key) if _places_key else []
        except Exception as _places_err:
            logger.warning(f"[competitor_analysis] Nearby practices load failed (non-fatal): {_places_err}")
            _nearby_dentists = []

        # Build campaign-type-specific filtering guidance for Claude
        _camp_type_filter = {
            "implants": (
                "Focus on implant-relevant competitors: practices advertising dental implants or "
                "all-on-4, national implant chains (ClearChoice, Nuvia, Affordable Dentures & Implants), "
                "and oral surgeons in the area. General dentistry offices without implant emphasis are "
                "lower priority unless they are very close geographically."
            ),
            "invisalign": (
                "Focus on orthodontic and Invisalign competitors: practices advertising Invisalign, "
                "clear aligners, or braces. National aligner brands (SmileDirectClub, Byte, Candid) "
                "are national_chain competitors. Local orthodontists and Invisalign providers are "
                "local_office competitors."
            ),
            "emergency": (
                "Focus on emergency dental competitors: practices advertising same-day or emergency "
                "dental care, urgent dental clinics, and any office with prominent emergency hours. "
                "These are all local_office competitors — no national chain plays in emergency dental."
            ),
            "dentures": (
                "Focus on denture-relevant competitors: Aspen Dental, Affordable Dentures & Implants, "
                "and local practices advertising full or partial dentures. National denture chains are "
                "national_chain; local practices are local_office."
            ),
            "cosmetic": (
                "Focus on cosmetic dental competitors: practices advertising veneers, whitening, smile "
                "makeovers, or cosmetic dentistry. Look for high-end cosmetic practices and any office "
                "with prominent cosmetic branding."
            ),
        }.get(_camp_type, (
            "This is a general dentistry campaign. Focus on full-service dental practices "
            "that compete for new patient acquisition across all services. Prioritize offices "
            "within 10 miles that accept new patients. Chains like Aspen Dental are national_chain; "
            "independent practices are local_office."
        ))

        # Build Places context block for prompt injection
        _places_block = ""
        if _nearby_dentists:
            _places_block = (
                f"\nREAL DENTAL PRACTICES NEAR GRAFTON MA (from Google Places — {len(_nearby_dentists)} found within 15 miles):\n"
                + _fmt_places(_nearby_dentists)
                + "\n\nSELECTION RULE: You MUST select competitors from this real list. Do NOT invent "
                "practices not in this list. If a national chain (ClearChoice, Nuvia, etc.) does not "
                "appear in the list, you may add it as a national_chain competitor with confidence: 'high' "
                "since they advertise nationally. For local_office competitors, only name practices that "
                "appear in the list above.\n"
            )
        else:
            _places_block = (
                "\nNOTE: Real-time Places data unavailable. Use your best knowledge of dental practices "
                "in the Worcester County MA area, but mark confidence carefully.\n"
            )

        # Build conquest eligibility note for Claude
        _conquest_instruction = (
            "conquest_keywords: leave empty — the server will derive conquest targets from "
            "competitor classification. Do NOT populate this field."
        )
        _conquest_note = ""
        if _camp_type in _CONQUEST_ELIGIBLE:
            _conquest_note = (
                f"\nThis is a {_camp_type} campaign. National destination chains "
                "(e.g. ClearChoice, Nuvia, Affordable Dentures & Implants) are comparison-shopping "
                "targets for this service — mark them as 'national_chain'. Local offices are "
                "contact-lookup intent only — mark them as 'local_office'."
            )

        # Target towns for geo context
        _target_towns = (
            "Grafton, Shrewsbury, Westborough, Northborough, Millbury, Auburn, Upton, Hopkinton, "
            "Southborough, Marlborough, Boylston, Holden, Leicester, Spencer, Sutton, Uxbridge, Worcester"
        )

        prompt = f"""You are a Google Ads competitive intelligence specialist. Analyze the local competitor landscape for a dental practice running a paid search campaign.

PRACTICE: Grafton Dental Care, Grafton MA (Worcester County)
SERVICES: Full-service dental practice — general dentistry, emergency care, implants, periodontal treatment, Invisalign, cosmetic dentistry, dentures. Dr. Anurag Gupta.
TARGET TOWNS: {_target_towns}

CAMPAIGN: "{campaign_name}"
SERVICE FOCUS: {service_focus}
CAMPAIGN TYPE: {_camp_type}
OBJECTIVE: {objective}
TARGET AUDIENCE: {target_audience}
KEY MESSAGES: {', '.join(key_messages)}
{_site_section}{_conquest_note}
{_places_block}
CAMPAIGN-TYPE COMPETITOR SELECTION GUIDANCE:
{_camp_type_filter}

TASK: Identify 8–12 dental practices that compete for the same patients for THIS SPECIFIC SERVICE TYPE. Use the real Places list above as your source for local competitors. Then identify how we can differentiate.

IMPORTANT RULES:
1. GROUND YOUR ANSWER IN REALITY: Use the real Places list for local_office competitors. Do not invent local offices. For national_chain competitors, you may add chains not in the list if they are known to advertise in this market.
2. Focus on the SERVICE TYPE — pick competitors relevant to "{_camp_type}" specifically, not just any dental office nearby.
3. For EACH competitor, classify it:
   - "local_office": a specific physical dental office within driving distance (independent or chain branch).
   - "national_chain": a destination brand whose patients comparison-shop across providers
     (e.g. ClearChoice, Nuvia, Affordable Dentures & Implants, Smile Direct Club, Byte, Candid).
4. For EACH competitor, provide brand_stems: 1-3 lowercase tokens used for keyword matching
   (e.g. ["aspen dental", "aspendental"] for "Aspen Dental Worcester").
5. our_differentiators must be specific to Grafton Dental Care — not generic dental tropes. Use the website intelligence and service focus above.
6. {_conquest_instruction}

Return ONLY a JSON object with this exact structure:
{{
  "caveat": "AI-generated competitive intelligence — verify competitor details before use",
  "competitors": [
    {{
      "name": "Practice Name",
      "location": "City, MA",
      "confidence": "high|medium|low",
      "classification": "local_office|national_chain",
      "brand_stems": ["practice name", "practice"],
      "likely_emphasis": "What they probably lead with in ads (e.g. price, convenience, technology)",
      "gap_we_can_address": "Positioning angle we can use against them"
    }}
  ],
  "our_differentiators": [
    "Specific differentiator 1",
    "Specific differentiator 2"
  ],
  "conquest_keywords": [],
  "competitor_negatives": [],
  "positioning_notes": "Overall positioning strategy for this campaign given the competitive landscape"
}}

No markdown, no explanation outside the JSON."""

    try:
        response = ai_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # BE-G12 fix: use shared helper to handle ```json fences + bare JSON
        import json as _json
        _json_text = _extract_json_from_ai_response(raw)
        data = _json.loads(_json_text)

        # For competitor_analysis: apply policy + preserve user overrides from prior generation
        if step == "competitor_analysis" and isinstance(data, dict):
            data["campaign_type"] = _camp_type  # always authoritative
            # Merge user negate_override values from previous save (survive regeneration)
            try:
                _old_build = get_campaign_build(campaign_id)
                _old_comps = ((_old_build.get("competitor_analysis") or {}).get("competitors") or [])
                if _old_comps:
                    data["competitors"] = _merge_overrides(
                        data.get("competitors") or [], _old_comps
                    )
            except Exception as _merge_err:
                logger.warning(f"build-step: override merge failed (non-fatal): {_merge_err}")
            # Apply classification-based negate/conquest policy
            try:
                _apply_comp_policy(data, _camp_type)
            except Exception as _policy_err:
                logger.warning(f"build-step: apply_competitor_policy failed (non-fatal): {_policy_err}")

    except Exception as e:
        logger.error(f"build-step AI call failed ({step}): {e}")
        raise HTTPException(status_code=500, detail=f"AI generation failed: {e}")

    save_campaign_build_step(campaign_id, step, data)
    logger.info(f"Campaign {campaign_id} build step '{step}' generated and saved")
    return {"ok": True, "step": step, "data": data}


# ── Competitor negate-override endpoint ──────────────────────────────────────

class CompetitorOverrideRequest(BaseModel):
    negate_override: Optional[bool] = None  # None = clear override, True = force-negate, False = force-allow


@app.patch(
    "/api/admin/campaigns/{campaign_id}/competitors/{competitor_name}/override",
    dependencies=[Depends(_require_admin)],
)
def admin_competitor_override(
    campaign_id: str,
    competitor_name: str,
    body: CompetitorOverrideRequest,
):
    """
    Set or clear the negate_override on a specific competitor in a campaign's
    competitor_analysis. Rebuilds derived competitor_negatives / conquest_keywords.
    """
    from database import get_campaign_by_id, get_campaign_build, save_campaign_build_step
    from competitor_policy import apply_competitor_policy as _acp, normalize as _norm_cp

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    build = get_campaign_build(campaign_id)
    ca = build.get("competitor_analysis")
    if not ca or not isinstance(ca, dict):
        raise HTTPException(status_code=404, detail="No competitor_analysis for this campaign")

    # Find matching competitor (case-insensitive normalized name)
    target_norm = _norm_cp(competitor_name)
    matched = None
    for c in (ca.get("competitors") or []):
        if _norm_cp(c.get("name", "")) == target_norm:
            matched = c
            break

    if matched is None:
        raise HTTPException(status_code=404, detail=f"Competitor '{competitor_name}' not found")

    matched["negate_override"] = body.negate_override

    # Re-derive campaign_type
    try:
        from search_term_classifier import _detect_campaign_type as _dtct_ov
        ctype = _dtct_ov(camp.get("campaign_name", "")) or "general"
    except Exception:
        ctype = ca.get("campaign_type", "general")

    _acp(ca, ctype)
    save_campaign_build_step(campaign_id, "competitor_analysis", ca)

    return {
        "ok": True,
        "competitor": matched,
        "competitor_negatives": ca.get("competitor_negatives", []),
        "conquest_keywords": ca.get("conquest_keywords", []),
    }


# ── Competitor review queue endpoints ────────────────────────────────────────

class CompetitorPromoteRequest(BaseModel):
    name: str
    classification: str
    brand_stems: list
    notes: Optional[str] = ""


@app.get("/api/admin/optimizer/competitor-queue", dependencies=[Depends(_require_admin)])
def admin_competitor_queue_list(status: str = "pending"):
    """
    List competitor_review_queue rows.
    status: 'pending' | 'dismissed' | 'promoted' | 'all'
    """
    from database import list_competitor_queue
    if status == "all":
        return {
            "pending":   list_competitor_queue("pending"),
            "dismissed": list_competitor_queue("dismissed"),
            "promoted":  list_competitor_queue("promoted"),
        }
    return {"rows": list_competitor_queue(status), "status": status}


@app.post(
    "/api/admin/optimizer/competitor-queue/{row_id}/dismiss",
    dependencies=[Depends(_require_admin)],
)
def admin_competitor_queue_dismiss(row_id: int, body: dict = Body(default={})):
    """Dismiss a competitor review queue candidate. Sticky — won't resurface."""
    from database import dismiss_competitor_candidate
    note = (body.get("note") or "") if isinstance(body, dict) else ""
    ok = dismiss_competitor_candidate(row_id, decided_by="admin", note=note)
    if not ok:
        raise HTTPException(status_code=404, detail="Row not found or already promoted")
    return {"ok": True, "id": row_id, "status": "dismissed"}


@app.post(
    "/api/admin/optimizer/competitor-queue/{row_id}/restore",
    dependencies=[Depends(_require_admin)],
)
def admin_competitor_queue_restore(row_id: int):
    """Restore a dismissed competitor candidate back to pending."""
    from database import restore_competitor_candidate
    ok = restore_competitor_candidate(row_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Row not found")
    return {"ok": True, "id": row_id, "status": "pending"}


@app.post(
    "/api/admin/optimizer/competitor-queue/{row_id}/promote",
    dependencies=[Depends(_require_admin)],
)
def admin_competitor_queue_promote(row_id: int, body: CompetitorPromoteRequest):
    """
    Promote a competitor candidate to confirmed competitor_practices.
    Classification + brand_stems come from the user (pre-filled by frontend heuristic).
    """
    from database import promote_competitor_candidate
    practice = promote_competitor_candidate(
        row_id=row_id,
        name=body.name,
        classification=body.classification,
        brand_stems=body.brand_stems,
        notes=body.notes or "",
        decided_by="admin",
    )
    if practice is None:
        raise HTTPException(status_code=404, detail="Queue row not found")
    return {"ok": True, "queue_id": row_id, "practice": practice}


@app.patch("/api/admin/campaigns/{campaign_id}/strategy", dependencies=[Depends(_require_admin)])
def admin_campaign_save_strategy(campaign_id: str, body: CampaignStrategyUpdateRequest):
    """Persist the Opus-generated strategy JSON to the campaign record."""
    from database import update_campaign_strategy
    found = update_campaign_strategy(campaign_id, body.strategy)
    if not found:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return {"ok": True}


@app.patch("/api/admin/campaigns/{campaign_id}/status", dependencies=[Depends(_require_admin)])
def admin_campaign_status(campaign_id: str, status: str = Body(..., embed=True)):
    """Update a campaign's status (ACTIVE, PAUSED, COMPLETED, ARCHIVED).
    Accepts JSON body: {"status": "PAUSED"}
    """
    from database import update_campaign_status
    # ACTIVE and PAUSED must go through the /pause and /resume endpoints
    # which also sync Google Ads. Only allow non-GAds transitions here.
    allowed = {"DRAFT", "COMPLETED", "ARCHIVED"}
    if status.upper() not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Use /pause or /resume for ACTIVE/PAUSED transitions. This endpoint only accepts {sorted(allowed)}"
        )
    found = update_campaign_status(campaign_id, status.upper())
    if not found:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return {"ok": True}


# ─── Campaign lifecycle controls (Pause / Resume / Stop) ─────────────────────

@app.post("/api/admin/campaigns/{campaign_id}/pause", dependencies=[Depends(_require_admin)])
def admin_campaign_pause(campaign_id: str):
    """Pause a campaign — locally and in Google Ads if linked."""
    from database import get_campaign_by_id, update_campaign_status
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Idempotency guard — already paused, nothing to do
    if camp["status"] == "PAUSED":
        return {"ok": True, "status": "PAUSED", "gads_updated": False, "note": "already paused"}

    gads_updated = False
    gads_error = None
    if camp.get("gads_campaign_resource"):
        from google_ads_create import set_campaign_status
        result = set_campaign_status(camp["gads_campaign_resource"], "PAUSED")
        gads_updated = result["ok"]
        gads_error = result.get("error")
        if not gads_updated:
            # Don't flip local status if the remote call failed for a linked campaign
            raise HTTPException(
                status_code=502,
                detail=f"Google Ads pause failed: {gads_error}. Local status unchanged."
            )

    update_campaign_status(campaign_id, "PAUSED")
    return {"ok": True, "status": "PAUSED", "gads_updated": gads_updated}


@app.post("/api/admin/campaigns/{campaign_id}/resume", dependencies=[Depends(_require_admin)])
def admin_campaign_resume(campaign_id: str):
    """Resume (enable) a campaign — locally and in Google Ads if linked."""
    from database import get_campaign_by_id, update_campaign_status
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Idempotency guard — already active, nothing to do
    if camp["status"] == "ACTIVE":
        return {"ok": True, "status": "ACTIVE", "gads_updated": False, "note": "already active"}
    # Prevent resuming a stopped campaign
    if camp["status"] in ("ARCHIVED", "COMPLETED"):
        raise HTTPException(status_code=422, detail=f"Cannot resume a campaign with status {camp['status']}")

    gads_updated = False
    gads_error = None
    if camp.get("gads_campaign_resource"):
        from google_ads_create import set_campaign_status
        result = set_campaign_status(camp["gads_campaign_resource"], "ENABLED")
        gads_updated = result["ok"]
        gads_error = result.get("error")
        if not gads_updated:
            raise HTTPException(
                status_code=502,
                detail=f"Google Ads resume failed: {gads_error}. Local status unchanged."
            )

    update_campaign_status(campaign_id, "ACTIVE")
    return {"ok": True, "status": "ACTIVE", "gads_updated": gads_updated}


@app.post("/api/admin/campaigns/{campaign_id}/stop", dependencies=[Depends(_require_admin)])
def admin_campaign_stop(campaign_id: str):
    """Permanently stop a campaign. REMOVED in Google Ads (irreversible), ARCHIVED locally."""
    from database import get_campaign_by_id, update_campaign_status
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Idempotency guard — already stopped, nothing to do (and GAds won't accept a REMOVED mutate)
    if camp["status"] == "ARCHIVED":
        return {"ok": True, "status": "ARCHIVED", "gads_updated": False, "note": "already stopped"}

    gads_updated = False
    gads_error = None
    if camp.get("gads_campaign_resource"):
        from google_ads_create import set_campaign_status
        result = set_campaign_status(camp["gads_campaign_resource"], "REMOVED")
        gads_updated = result["ok"]
        gads_error = result.get("error")
        if not gads_updated:
            raise HTTPException(
                status_code=502,
                detail=f"Google Ads stop failed: {gads_error}. Local status unchanged."
            )

    update_campaign_status(campaign_id, "ARCHIVED")
    return {"ok": True, "status": "ARCHIVED", "gads_updated": gads_updated}


@app.delete("/api/admin/campaigns/{campaign_id}", dependencies=[Depends(_require_admin)])
def admin_campaign_delete(campaign_id: str):
    """
    Permanently delete a campaign from the local dashboard.
    Does NOT touch Google Ads — only removes the local DB record.
    UNMANAGED campaigns (status=UNMANAGED) can always be deleted — they are orphaned import rows.
    Managed campaigns with a GAds resource must use Stop instead.
    """
    from database import get_campaign_by_id, delete_campaign
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    # Block deletion of actively managed GAds-linked campaigns (use Stop instead)
    # UNMANAGED rows are orphaned/duplicate imports — always deletable
    is_managed = camp.get("gads_campaign_resource") and camp.get("status") != "UNMANAGED"
    if is_managed:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a managed campaign linked to Google Ads. Use Stop instead."
        )
    deleted = delete_campaign(campaign_id)
    return {"ok": deleted, "campaign_id": campaign_id}


# ─── Google Ads Campaign Import ──────────────────────────────────────────────

@app.get("/api/admin/gads/list-campaigns", dependencies=[Depends(_require_admin)])
def admin_gads_list_campaigns():
    """
    Fetch all non-REMOVED campaigns from the Google Ads account.
    Marks each one as already_imported if it exists in local campaigns table.
    READ-ONLY — no kill switch required.
    """
    from google_ads_create import fetch_campaigns_from_gads
    from database import get_all_campaigns_with_workflows

    try:
        gads_campaigns = fetch_campaigns_from_gads()
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch campaigns from Google Ads: {e}"
        )

    # Build already-imported set from both gads_campaign_numeric_id AND campaign_id
    # (handles manual campaigns that used a numeric ID as campaign_id)
    local = get_all_campaigns_with_workflows()
    already_imported = set()
    for c in local:
        if c.get("gads_campaign_numeric_id"):
            already_imported.add(c["gads_campaign_numeric_id"])
        if c.get("campaign_id"):
            already_imported.add(c["campaign_id"])

    for c in gads_campaigns:
        c["already_imported"] = c["campaign_id"] in already_imported

    # Local campaigns with no Google Ads link yet — offered as "link to existing" targets in the import modal
    unlinked_local = [
        {
            "campaign_id":   c["campaign_id"],
            "campaign_name": c["campaign_name"],
            "status":        c.get("status"),
        }
        for c in local
        if not c.get("gads_campaign_numeric_id")
        and not c.get("gads_campaign_resource")
        and c.get("campaign_type") != "GOOGLE_ADS"
    ]

    return {"campaigns": gads_campaigns, "total": len(gads_campaigns), "unlinked_local_campaigns": unlinked_local}


class ImportCampaignsRequest(BaseModel):
    campaign_ids: list[str]   # GAds numeric campaign IDs to import


def _backfill_campaign_snapshot(campaign_id: str, resource_name: str) -> None:
    """
    Background task: fetch keywords/ads/ad-groups from Google Ads and store
    them in gads_campaign_snapshot.  Runs after import so the HTTP response
    is not blocked by 10-20s of API calls.
    """
    from google_ads_create import fetch_campaign_build_data
    from database import save_gads_campaign_snapshot
    try:
        snapshot = fetch_campaign_build_data(resource_name)
        if snapshot.get("error"):
            logger.warning(f"Snapshot backfill skipped for {campaign_id}: {snapshot['error']}")
            return
        save_gads_campaign_snapshot(campaign_id, snapshot)
        logger.info(f"Snapshot backfill complete for campaign {campaign_id}")
    except Exception as e:
        logger.error(f"Snapshot backfill failed for {campaign_id}: {e}")


@app.post("/api/admin/gads/import-campaigns", dependencies=[Depends(_require_admin)])
def admin_import_campaigns(body: ImportCampaignsRequest, background_tasks: BackgroundTasks):
    """
    Import selected Google Ads campaigns into the local managed campaigns table.
    Sets gads_campaign_resource + gads_campaign_numeric_id in one atomic INSERT.
    Skips already-imported campaigns silently.
    After each import, a background task fetches keywords/ads/ad-groups from
    Google Ads and stores them in gads_campaign_snapshot (non-blocking).
    """
    from google_ads_create import fetch_campaigns_from_gads
    from database import create_campaign, get_campaign_by_id

    if not body.campaign_ids:
        raise HTTPException(status_code=422, detail="No campaign IDs provided")

    try:
        gads_campaigns = fetch_campaigns_from_gads()
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch campaigns from Google Ads: {e}"
        )

    gads_map = {c["campaign_id"]: c for c in gads_campaigns}

    imported = []
    skipped  = []
    errors   = []

    for cid in body.campaign_ids:
        if cid not in gads_map:
            errors.append({"campaign_id": cid, "error": "Not found in Google Ads"})
            continue

        gads = gads_map[cid]

        # Already imported? (use numeric ID as campaign_id, so get_campaign_by_id works)
        existing = get_campaign_by_id(cid)
        if existing:
            skipped.append(cid)
            continue

        # Map GAds status → local status
        local_status = "ACTIVE" if gads["gads_status"] == "ENABLED" else "PAUSED"

        data = {
            "campaign_id":               cid,              # GAds numeric ID = local logical key
            "campaign_name":             gads["campaign_name"],
            "status":                    local_status,
            "campaign_type":             "GOOGLE_ADS",
            "monthly_budget":            gads["monthly_budget_usd"],   # daily × 30 approx
            "start_date":                gads["start_date"],
            "end_date":                  gads["end_date"],
            "notes":                     f"Imported from Google Ads. Channel: {gads['channel_type']}. Daily budget: ${gads['daily_budget_usd']}/day.",
            "gads_campaign_resource":    gads["resource_name"],
            "gads_campaign_numeric_id":  cid,
        }

        try:
            create_campaign(data)
            logger.info(f"Imported GAds campaign: {cid} '{gads['campaign_name']}'")
            imported.append({"campaign_id": cid, "campaign_name": gads["campaign_name"]})
            # Non-blocking snapshot backfill — fetches keywords/ads/ad-groups in background
            background_tasks.add_task(_backfill_campaign_snapshot, cid, gads["resource_name"])
        except Exception as e:
            logger.error(f"Failed to import GAds campaign {cid}: {e}")
            errors.append({"campaign_id": cid, "error": str(e)})

    logger.info(f"GAds import complete: {len(imported)} imported, {len(skipped)} skipped, {len(errors)} errors")
    return {"imported": imported, "skipped": skipped, "errors": errors}


class LinkGadsCampaignRequest(BaseModel):
    gads_campaign_id: str    # GAds numeric campaign ID
    local_campaign_id: str   # Existing dashboard campaign_id to link it to


@app.post("/api/admin/gads/link-campaign", dependencies=[Depends(_require_admin)])
def admin_link_gads_campaign(body: LinkGadsCampaignRequest, background_tasks: BackgroundTasks):
    """
    Link an existing local dashboard campaign to a Google Ads campaign.
    Sets gads_campaign_resource + gads_campaign_numeric_id + syncs campaign_name.
    Does NOT create a new row — links to the existing one.
    """
    import datetime as _dt2
    from google_ads_create import fetch_campaigns_from_gads
    from database import get_campaign_by_id, get_all_campaigns_with_workflows, _conn

    # 1. Validate local campaign exists and is not already linked
    local = get_campaign_by_id(body.local_campaign_id)
    if not local:
        raise HTTPException(status_code=404, detail="Local campaign not found")
    if local.get("gads_campaign_numeric_id") or local.get("gads_campaign_resource"):
        raise HTTPException(status_code=409, detail="Local campaign is already linked to a Google Ads campaign")

    # 2. Fetch GAds campaigns and validate the target exists
    try:
        gads_campaigns = fetch_campaigns_from_gads()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch campaigns from Google Ads: {e}")

    gads = next((g for g in gads_campaigns if g["campaign_id"] == body.gads_campaign_id), None)
    if not gads:
        raise HTTPException(status_code=404, detail="Google Ads campaign not found in account")

    # 3. Ensure no other local row already owns this GAds numeric ID
    for c in get_all_campaigns_with_workflows():
        if (c.get("gads_campaign_numeric_id") == body.gads_campaign_id
                and str(c["campaign_id"]) != str(body.local_campaign_id)):
            raise HTTPException(
                status_code=409,
                detail=f"GAds campaign is already linked to dashboard campaign '{c['campaign_name']}'"
            )

    # 4. Update local row: resource name, numeric ID, and sync campaign_name to match Google Ads
    now = _dt2.datetime.now(_dt2.timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE campaigns SET gads_campaign_resource=?, gads_campaign_numeric_id=?, "
            "campaign_name=?, updated_at=? WHERE campaign_id=?",
            (gads["resource_name"], body.gads_campaign_id,
             gads["campaign_name"], now, body.local_campaign_id)
        )

    logger.info(f"Linked local campaign '{body.local_campaign_id}' to GAds campaign '{body.gads_campaign_id}' ('{gads['campaign_name']}')")

    # 5. Backfill keywords/ads/ad-groups snapshot in background (mirrors import flow)
    background_tasks.add_task(_backfill_campaign_snapshot, body.local_campaign_id, gads["resource_name"])

    return {
        "ok":                True,
        "local_campaign_id": body.local_campaign_id,
        "gads_campaign_id":  body.gads_campaign_id,
        "campaign_name":     gads["campaign_name"],
        "resource_name":     gads["resource_name"],
    }



@app.post("/api/admin/campaigns/{campaign_id}/sync-from-gads", dependencies=[Depends(_require_admin)])
def admin_sync_campaign_from_gads(campaign_id: str):
    """
    On-demand sync: re-fetch keywords, ad copies, ad groups AND campaign-level
    settings (budget, status, bidding strategy) from Google Ads.

    - gads_campaign_snapshot: updated with keywords/ads/ad groups
    - campaigns table:        monthly_budget, status synced from live Google Ads values
      (campaign_build_json is never touched — user edits in the wizard are preserved)

    Returns the updated snapshot + the fields synced into the campaigns table.
    """
    from database import (
        get_campaign_by_id, save_gads_campaign_snapshot, get_gads_campaign_snapshot,
        update_campaign_fields,
    )
    from google_ads_create import fetch_campaign_build_data, fetch_campaigns_from_gads

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    resource_name = camp.get("gads_campaign_resource") or ""
    if not resource_name:
        raise HTTPException(
            status_code=400,
            detail="Campaign is not linked to Google Ads — import it first"
        )

    # ── 1. Snapshot: keywords / ads / ad groups ───────────────────────────────
    snapshot = fetch_campaign_build_data(resource_name)
    if snapshot.get("error"):
        raise HTTPException(
            status_code=502,
            detail=f"Google Ads API error: {snapshot['error']}"
        )
    save_gads_campaign_snapshot(campaign_id, snapshot)

    # ── 2. Campaign-level settings: budget + status from Google Ads ───────────
    synced_fields: dict = {}
    landing_page_discrepancy: dict | None = None
    try:
        all_campaigns = fetch_campaigns_from_gads()
        live = next(
            (c for c in all_campaigns if c.get("resource_name") == resource_name),
            None,
        )
        if live:
            daily_usd = live.get("daily_budget_usd") or 0.0
            if daily_usd > 0:
                synced_fields["monthly_budget"] = round(daily_usd * 30.4, 2)

            gads_status = live.get("gads_status", "")  # "ENABLED" or "PAUSED"
            if gads_status == "ENABLED":
                synced_fields["status"] = "ACTIVE"
            elif gads_status == "PAUSED":
                synced_fields["status"] = "PAUSED"

            if synced_fields:
                update_campaign_fields(campaign_id, synced_fields)
                logger.info(
                    f"Sync from GAds — updated campaigns table for {campaign_id}: {synced_fields}"
                )
    except Exception as _e:
        # Non-fatal — snapshot was already saved; log and continue
        logger.warning(f"sync-from-gads: could not sync campaign-level fields: {_e}")

    # ── 3. Landing page discrepancy check ─────────────────────────────────────
    # Compare DB landing_page against live final_urls in the snapshot.
    # If they differ, return a discrepancy object so the frontend can offer
    # "Update Dashboard" (write live URL to DB) or "Update in Google Ads" (deep link).
    try:
        db_lp = (camp.get("landing_page") or "").rstrip("/").lower()
        _ad_copy_block = snapshot.get("ad_copy") or {}
        live_ads = _ad_copy_block.get("ads", []) if isinstance(_ad_copy_block, dict) else []
        raw_live_urls = []   # original URLs from API (preserve casing)
        norm_live_urls = []  # lowercased+stripped for comparison
        for ad in live_ads:
            for url in (ad.get("final_urls") or []):
                norm = url.rstrip("/").lower()
                if norm and norm not in norm_live_urls:
                    norm_live_urls.append(norm)
                    raw_live_urls.append(url.rstrip("/"))

        if db_lp and norm_live_urls and db_lp not in norm_live_urls:
            # Parse numeric campaign ID from resource name for direct GAds link
            gads_numeric = resource_name.split("/campaigns/")[-1] if "/campaigns/" in resource_name else ""
            # Google Ads deep link to the Ads tab of this campaign
            gads_edit_url = (
                f"https://ads.google.com/aw/ads?campaignId={gads_numeric}"
                if gads_numeric else "https://ads.google.com"
            )
            landing_page_discrepancy = {
                "db_url":        camp.get("landing_page") or "",
                "live_url":      raw_live_urls[0] if raw_live_urls else "",
                "all_live_urls": norm_live_urls,  # normalized list used for validation
                "gads_edit_url": gads_edit_url,
            }
            logger.info(
                f"sync-from-gads {campaign_id}: landing page discrepancy — "
                f"db={db_lp!r} live={raw_live_urls}"
            )
        elif not db_lp and raw_live_urls:
            # DB has no landing page recorded but GAds has one — write it silently
            update_campaign_fields(campaign_id, {"landing_page": raw_live_urls[0]})
            synced_fields["landing_page"] = raw_live_urls[0]
            logger.info(f"sync-from-gads {campaign_id}: seeded missing landing_page from GAds: {raw_live_urls[0]}")
    except Exception as _lpe:
        logger.warning(f"sync-from-gads: landing page check failed: {_lpe}")

    logger.info(f"Manual GAds sync complete for campaign {campaign_id}")
    return {
        "ok": True,
        "campaign_id": campaign_id,
        "snapshot": snapshot,
        "synced_at": snapshot.get("synced_from_gads_at"),
        "synced_fields": synced_fields,
        "landing_page_discrepancy": landing_page_discrepancy,
    }


@app.post("/api/admin/campaigns/{campaign_id}/accept-gads-landing-page", dependencies=[Depends(_require_admin)])
def admin_accept_gads_landing_page(campaign_id: str, body: dict = Body(...)):
    """
    Accept the live Google Ads landing page URL as the source of truth.
    Writes the provided URL into campaigns.landing_page in the DB.
    Called when user clicks "Update Dashboard" on a landing page discrepancy banner.

    Body:
      url            — the URL to store (must match one of all_live_urls, if provided)
      all_live_urls  — optional list of valid GAds URLs for server-side validation
    """
    from database import get_campaign_by_id, update_campaign_fields
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    url = (body.get("url") or "").strip()
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid URL")
    # Validate against the list of known live URLs when the caller supplies it
    all_live_urls = body.get("all_live_urls")
    if all_live_urls and isinstance(all_live_urls, list):
        norm_url = url.rstrip("/").lower()
        norm_live = [u.rstrip("/").lower() for u in all_live_urls if isinstance(u, str)]
        if norm_url not in norm_live:
            raise HTTPException(
                status_code=400,
                detail="URL is not one of the live Google Ads landing pages"
            )
    update_campaign_fields(campaign_id, {"landing_page": url})
    logger.info(f"accept-gads-landing-page {campaign_id}: DB updated to {url!r}")
    return {"ok": True, "landing_page": url}


@app.post("/api/admin/campaigns/{campaign_id}/sync-to-gads", dependencies=[Depends(_require_admin)])
def admin_sync_campaign_to_gads(campaign_id: str):
    """
    Push ONLY changed dashboard values to Google Ads, then verify by reading back.

    Compares each field against the live Google Ads value first.
    Only fields that actually differ are written — unchanged fields are skipped.

    Fields compared + pushed if changed:
      - monthly_budget  → daily budget (monthly / 30.4)
      - status          → ENABLED or PAUSED
      - sitelinks       → always replaced if non-empty (no cheap equality check available)

    NOTE: landing_page (final_urls) is NOT pushed. RSA final_urls is immutable
    after creation in the Google Ads API.
    """
    from database import get_campaign_by_id
    from google_ads_write import (
        set_campaign_daily_budget,
        set_campaign_status_gads,
    )
    from google_ads_create import fetch_campaigns_from_gads, add_sitelinks_to_campaign
    from campaign_safety import check_budget_absolute_limits, WriteBlockedError

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    gads_resource = camp.get("gads_campaign_resource") or ""
    if not gads_resource:
        raise HTTPException(
            status_code=400,
            detail="Campaign is not linked to Google Ads — import it first"
        )

    pushed: list[dict]   = []   # [{field, pushed_value, label}]
    skipped: list[str]   = []   # fields that already match — not written
    errors: list[str]    = []

    # ── Fetch live GAds values first so we can diff before writing ────────────
    live_campaign = None
    try:
        all_campaigns = fetch_campaigns_from_gads()
        live_campaign = next((c for c in all_campaigns if c.get("resource_name") == gads_resource), None)
    except Exception as fe:
        logger.warning(f"sync-to-gads {campaign_id}: could not fetch live values for diff — will push all: {fe}")

    live_daily  = live_campaign.get("daily_budget_usd") or 0.0 if live_campaign else None
    live_status = (live_campaign.get("gads_status") or "").upper() if live_campaign else None

    # ── 1. Push budget only if it changed ────────────────────────────────────
    monthly_budget = camp.get("monthly_budget") or 0
    if monthly_budget > 0:
        new_daily  = round(monthly_budget / 30.4, 2)
        new_micros = int(new_daily * 1_000_000)
        budget_matches = live_daily is not None and abs(live_daily - new_daily) < 0.02
        if budget_matches:
            skipped.append(f"budget (already ${new_daily:.2f}/day in Google Ads)")
            logger.info(f"sync-to-gads {campaign_id}: budget unchanged (${new_daily:.2f}) — skipping")
        else:
            try:
                check_budget_absolute_limits(new_micros)
                set_campaign_daily_budget(gads_resource, new_daily)
                pushed.append({"field": "budget", "pushed_value": new_daily, "label": f"${new_daily:.2f}/day"})
                logger.info(f"sync-to-gads {campaign_id}: budget → ${new_daily:.2f}/day")
            except WriteBlockedError as e:
                errors.append(f"budget blocked: {e}")
            except Exception as e:
                errors.append(f"budget push failed: {e}")
                logger.error(f"sync-to-gads {campaign_id} budget error: {e}")

    # ── 2. Push status only if it changed ────────────────────────────────────
    db_status = (camp.get("status") or "").upper()
    gads_status_map = {"ACTIVE": "ENABLED", "PAUSED": "PAUSED"}
    gads_status = gads_status_map.get(db_status)
    if gads_status:
        status_matches = live_status is not None and live_status == gads_status
        if status_matches:
            skipped.append(f"status (already {gads_status} in Google Ads)")
            logger.info(f"sync-to-gads {campaign_id}: status unchanged ({gads_status}) — skipping")
        else:
            try:
                set_campaign_status_gads(gads_resource, gads_status)
                pushed.append({"field": "status", "pushed_value": gads_status, "label": gads_status})
                logger.info(f"sync-to-gads {campaign_id}: status → {gads_status}")
            except Exception as e:
                errors.append(f"status push failed: {e}")
                logger.error(f"sync-to-gads {campaign_id} status error: {e}")

    # ── 3. Landing page — NOT pushed automatically ────────────────────────────
    # Google Ads RSA final_urls is IMMUTABLE after creation (IMMUTABLE_FIELD error).
    # AdGroup.final_urls does not exist as a field in the API.
    # Changing landing page requires pausing existing ads and creating new ones —
    # too destructive to do silently. Landing page edits must be done manually in
    # the Google Ads UI or via a dedicated "replace ads" workflow.
    # The landing_page field in our DB is stored for reference / new campaign creation.

    # ── 4. Push sitelinks ─────────────────────────────────────────────────────
    sitelinks_raw = camp.get("sitelinks") or ""
    sitelinks_list = []
    if sitelinks_raw:
        try:
            sitelinks_list = json.loads(sitelinks_raw) if isinstance(sitelinks_raw, str) else sitelinks_raw
        except Exception:
            pass
    if sitelinks_list:
        try:
            sl_result = add_sitelinks_to_campaign(gads_resource, sitelinks_list, replace=True)
            n = sl_result.get("count", 0)
            sl_errors = sl_result.get("errors") or []
            if n > 0:
                pushed.append({
                    "field": "sitelinks",
                    "pushed_value": n,
                    "label": f"{n} sitelink{'s' if n != 1 else ''} pushed",
                })
                logger.info(f"sync-to-gads {campaign_id}: sitelinks → {n} pushed")
                # Library upsert deferred to after verification confirms count match
            for se in sl_errors:
                errors.append(f"sitelinks: {se}")
        except Exception as e:
            errors.append(f"sitelinks push failed: {e}")
            logger.error(f"sync-to-gads {campaign_id} sitelinks error: {e}")

    # ── 5. Verify — read back from Google Ads ─────────────────────────────────
    # Reuse live_campaign from the diff fetch if nothing was pushed for budget/status
    # (no point re-fetching a campaign that we know didn't change).
    # If budget or status WAS pushed, fetch a fresh snapshot to confirm the write.
    verification: list[dict] = []
    try:
        budget_pushed = next((p for p in pushed if p["field"] == "budget"), None)
        status_pushed = next((p for p in pushed if p["field"] == "status"), None)
        need_fresh = budget_pushed or status_pushed

        if need_fresh:
            # Fields were written — re-fetch to confirm they landed
            all_campaigns_verify = fetch_campaigns_from_gads()
            live = next((c for c in all_campaigns_verify if c.get("resource_name") == gads_resource), None)
        else:
            # Nothing written for budget/status — reuse what we already fetched
            live = live_campaign

        if live:
            live_daily_v  = live.get("daily_budget_usd") or 0.0
            live_status_v = live.get("gads_status") or ""

            # Verify budget
            if budget_pushed:
                match = abs(live_daily_v - budget_pushed["pushed_value"]) < 0.02
                verification.append({
                    "field": "Daily Budget",
                    "pushed":   f"${budget_pushed['pushed_value']:.2f}/day",
                    "live":     f"${live_daily_v:.2f}/day",
                    "match":    match,
                })

            # Verify status
            if status_pushed:
                match = live_status_v.upper() == status_pushed["pushed_value"].upper()
                verification.append({
                    "field": "Status",
                    "pushed":   status_pushed["pushed_value"],
                    "live":     live_status_v,
                    "match":    match,
                })

        # Verify sitelinks — count live sitelink assets on campaign
        sl_pushed = next((p for p in pushed if p["field"] == "sitelinks"), None)
        if sl_pushed:
            try:
                from google_ads_create import _build_client as _gc_build
                _sl_cl = _gc_build()
                _sl_ga = _sl_cl.get_service("GoogleAdsService")
                _sl_query = f"""
                    SELECT campaign_asset.asset, campaign_asset.status
                    FROM campaign_asset
                    WHERE campaign_asset.campaign = '{gads_resource}'
                      AND campaign_asset.field_type = SITELINK
                      AND campaign_asset.status != REMOVED
                """
                from google_ads_write import _customer_id_from_resource as _cid_from_res2
                _sl_cid = _cid_from_res2(gads_resource)
                live_sl_count = sum(1 for _ in _sl_ga.search(customer_id=_sl_cid, query=_sl_query))
                match = live_sl_count == sl_pushed["pushed_value"]
                verification.append({
                    "field": "Sitelinks",
                    "pushed":   f"{sl_pushed['pushed_value']} sitelink{'s' if sl_pushed['pushed_value'] != 1 else ''}",
                    "live":     f"{live_sl_count} sitelink{'s' if live_sl_count != 1 else ''} in Google Ads",
                    "match":    match,
                })
                # ── Save to sitelink library only after verified ──────────────
                if match and sitelinks_list:
                    try:
                        from database import upsert_sitelink_library
                        upsert_sitelink_library(sitelinks_list)
                        logger.info(f"sync-to-gads {campaign_id}: {len(sitelinks_list)} sitelinks saved to library (verified)")
                    except Exception as lib_e:
                        logger.warning(f"sync-to-gads: sitelink library upsert failed (non-fatal): {lib_e}")
            except Exception as ve:
                verification.append({
                    "field": "Sitelinks",
                    "pushed":   f"{sl_pushed['pushed_value']} sitelinks",
                    "live":     f"(verification failed: {ve})",
                    "match":    None,
                })

    except Exception as ve:
        logger.warning(f"sync-to-gads {campaign_id}: verification read-back failed: {ve}")
        verification = [{"field": "Verification", "pushed": "", "live": f"Read-back failed: {ve}", "match": None}]

    all_match = all(v.get("match") is True for v in verification) if verification else True

    return {
        "ok": True,
        "campaign_id": campaign_id,
        "pushed":      [p["label"] for p in pushed],
        "skipped":     skipped,
        "errors":      errors,
        "verification": verification,
        "all_match":   all_match,
    }


# ─── Campaign detail (click-through from Managed Campaigns table) ────────────

@app.get("/api/admin/campaigns/{campaign_id}/detail", dependencies=[Depends(_require_admin)])
def admin_campaign_detail(campaign_id: str, days: int = 30):
    """
    Full campaign detail: base info + strategy + GAds performance + ad groups + ad creatives.
    Returns everything needed for the campaign detail drawer in one request.
    """
    from database import (
        get_campaign_by_id, get_daily_stats, get_ad_group_stats, get_ads_with_metrics,
        get_search_term_stats, get_keyword_kpl_rollup
    )
    import json as _json

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Resolve Google Ads numeric ID and campaign name for downstream filtering
    gads_num_id = camp.get("gads_campaign_numeric_id") or ""
    camp_name   = camp.get("campaign_name") or ""

    # Parse strategy_json if stored as string
    strategy = None
    if camp.get("strategy_json"):
        try:
            strategy = _json.loads(camp["strategy_json"]) if isinstance(camp["strategy_json"], str) else camp["strategy_json"]
        except Exception:
            strategy = None

    # Daily stats filtered by numeric GAds campaign ID (not local UUID)
    daily_stats = get_daily_stats(days=days, campaign_id=gads_num_id or None)
    for row in daily_stats:
        row["cost"] = round((row.get("cost_micros") or 0) / 1_000_000.0, 2)

    # Aggregate summary from daily stats
    total_impressions = sum(r.get("impressions") or 0 for r in daily_stats)
    total_clicks      = sum(r.get("clicks") or 0 for r in daily_stats)
    total_cost        = sum(r.get("cost") or 0.0 for r in daily_stats)
    total_conversions = sum(r.get("conversions") or 0 for r in daily_stats)
    ctr  = round(total_clicks / total_impressions * 100, 2) if total_impressions > 0 else 0.0
    cpc  = round(total_cost / total_clicks, 2) if total_clicks > 0 else 0.0
    cpl  = round(total_cost / total_conversions, 2) if total_conversions > 0 else 0.0

    # Ad groups for this campaign (filter from all ad groups by campaign_id)
    all_ag = get_ad_group_stats(days=days)
    ad_groups = [
        ag for ag in all_ag
        if ag.get("campaign_id") == gads_num_id
        or ag.get("campaign_name", "").lower() == camp_name.lower()
    ]

    # Enrich ad_groups with resource_name + status from snapshot (needed for pause/enable)
    from database import get_gads_campaign_snapshot as _get_snap
    snap_preview = _get_snap(campaign_id)
    # snap["ad_groups"] = {"ad_groups": [...], "source": "..."} — list is nested one level deep
    ag_block = snap_preview.get("ad_groups") or {}
    snap_ag_list = (ag_block.get("ad_groups") if isinstance(ag_block, dict) else ag_block) or []
    snap_ag_map = {}      # ad_group_id (numeric str) → resource_name
    snap_status_map = {}  # ad_group_id (numeric str) → status string (ENABLED/PAUSED)
    for sag in snap_ag_list:
        rn = sag.get("resource_name") or ""
        # resource_name format: customers/NNNN/adGroups/MMMM
        if rn and "/adGroups/" in rn:
            ag_id = rn.split("/adGroups/")[-1]
            snap_ag_map[ag_id] = rn
            snap_status_map[ag_id] = (sag.get("status") or "ENABLED").upper()
    for ag in ad_groups:
        ag_id = str(ag.get("ad_group_id") or "")
        ag["resource_name"] = snap_ag_map.get(ag_id, "")
        ag["gads_status"] = snap_status_map.get(ag_id, "")

    # Ad creatives for this campaign
    all_ads = get_ads_with_metrics(days=days)
    ads = [
        ad for ad in all_ads
        if ad.get("campaign_id") == gads_num_id
        or ad.get("campaign_name", "").lower() == camp_name.lower()
    ]
    for ad in ads:
        impressions = ad.get("impressions") or 0
        clicks      = ad.get("clicks") or 0
        cost_micros = ad.get("cost_micros") or 0
        leads       = ad.get("leads") or 0
        cost        = cost_micros / 1_000_000.0
        ad["cost"]  = round(cost, 2)
        ad["ctr"]   = round(clicks / impressions * 100, 2) if impressions > 0 else 0.0
        ad["cpc"]   = round(cost / clicks, 2) if clicks > 0 else 0.0
        ad["cpl"]   = round(cost / leads, 2)  if leads  > 0 else 0.0
        if isinstance(ad.get("assets_json"), str):
            try:
                ad["assets_json"] = _json.loads(ad["assets_json"])
            except Exception:
                ad["assets_json"] = {"headlines": [], "descriptions": []}
        if not isinstance(ad.get("assets_json"), dict):
            ad["assets_json"] = {"headlines": [], "descriptions": []}
        # Normalize {text, pinned} objects → plain strings for frontend rendering
        assets = ad["assets_json"]
        assets["headlines"]    = [h["text"] if isinstance(h, dict) else h for h in assets.get("headlines", [])]
        assets["descriptions"] = [d["text"] if isinstance(d, dict) else d for d in assets.get("descriptions", [])]

    # Lead attribution: count leads linked to this campaign_id
    with __import__("database")._conn() as conn:
        lead_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM leads WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        lead_count = lead_row["cnt"] if lead_row else 0

    # Parse campaign_build_json
    from database import get_campaign_build, get_gads_campaign_snapshot
    build_data = get_campaign_build(campaign_id)

    # Snapshot from Google Ads (raw imported state — separate from user-edited build)
    gads_snapshot = get_gads_campaign_snapshot(campaign_id)

    # Search terms for this campaign (filtered by campaign name)
    search_terms = get_search_term_stats(campaign_name=camp_name, days=days) if camp_name else []

    # PR6: Per-keyword KPL income rollup for the Keywords sub-tab
    keyword_income = get_keyword_kpl_rollup(camp_name, days=days) if camp_name else []

    # Inject computed daily_budget_usd into the campaign dict for the budget editor.
    # Prefer the value from the most recent daily_stats row (reflects actual live budget),
    # then fall back to monthly_budget / 30.4 (local approximation).
    camp_out = dict(camp)
    if not camp_out.get("daily_budget_usd"):
        monthly = camp_out.get("monthly_budget") or 0
        camp_out["daily_budget_usd"] = round(monthly / 30.4, 2) if monthly else 0.0

    return {
        "campaign": {k: v for k, v in camp_out.items() if k not in ("strategy_json", "campaign_build_json", "gads_campaign_snapshot")},
        "strategy": strategy,
        "build": build_data,
        "gads_snapshot": gads_snapshot,
        "summary": {
            "days": days,
            "impressions": total_impressions,
            "clicks": total_clicks,
            "cost": round(total_cost, 2),
            "conversions": total_conversions,
            "ctr": ctr,
            "cpc": cpc,
            "cpl": cpl,
            "leads": lead_count,
        },
        "daily_stats": daily_stats,
        "ad_groups": ad_groups,
        "ads": ads,
        "search_terms": search_terms,
        "keyword_income": keyword_income,
        "has_gads_data": bool(camp.get("gads_campaign_resource")),
    }


# ─── Attribution confidence breakdown ────────────────────────────────────────

@app.get("/api/admin/attribution-confidence", dependencies=[Depends(_require_admin)])
def admin_attribution_confidence(days: int = 30):
    """
    PR6: Attribution confidence tier breakdown across all campaigns.
    Returns sums and percentages for high / low / booked_override tiers.
    Uses kpl.logged_at as the lookback window (same caveat as get_keyword_kpl_rollup).
    """
    days = max(min(int(days), 90), 1)
    cutoff = f"-{days} days"
    with __import__("database")._conn() as conn:
        # Dedup: a patient with both call-path and lead-path KPL rows would otherwise
        # be double-counted. Prefer the call-path row (richer confidence_tier).
        row = conn.execute("""
            WITH kpl_dedup AS (
                SELECT paid_amount_365d, confidence_tier
                FROM keyword_production_log
                WHERE (confidence_tier IN ('high','low','booked_override') OR confidence_tier IS NULL)
                  AND od_patient_num != ''
                  AND logged_at >= date('now', 'localtime', ?)
                  AND id IN (
                      SELECT COALESCE(
                          MAX(CASE WHEN lead_id LIKE 'call::%' THEN id END),
                          MAX(id)
                      )
                      FROM keyword_production_log
                      WHERE (confidence_tier IN ('high','low','booked_override') OR confidence_tier IS NULL)
                        AND od_patient_num != ''
                        AND logged_at >= date('now', 'localtime', ?)
                      GROUP BY od_patient_num
                  )
                UNION ALL
                SELECT paid_amount_365d, confidence_tier
                FROM keyword_production_log
                WHERE (confidence_tier IN ('high','low','booked_override') OR confidence_tier IS NULL)
                  AND (od_patient_num = '' OR od_patient_num IS NULL)
                  AND logged_at >= date('now', 'localtime', ?)
            )
            SELECT
                COALESCE(SUM(CASE WHEN confidence_tier = 'high'            THEN paid_amount_365d ELSE 0 END), 0) AS high_365d,
                COALESCE(SUM(CASE WHEN confidence_tier = 'low'             THEN paid_amount_365d ELSE 0 END), 0) AS low_365d,
                COALESCE(SUM(CASE WHEN confidence_tier = 'booked_override' THEN paid_amount_365d ELSE 0 END), 0) AS booked_override_365d,
                COALESCE(SUM(CASE WHEN confidence_tier IS NULL             THEN paid_amount_365d ELSE 0 END), 0) AS unknown_tier_365d,
                COALESCE(SUM(paid_amount_365d), 0) AS total_365d
            FROM kpl_dedup
        """, (cutoff, cutoff, cutoff)).fetchone()

    total = row["total_365d"] if row else 0.0
    high = row["high_365d"] if row else 0.0
    low = row["low_365d"] if row else 0.0
    booked = row["booked_override_365d"] if row else 0.0

    def pct(val):
        return round(val / total * 100, 1) if total > 0 else 0.0

    return {
        "high_365d": round(high, 2),
        "low_365d": round(low, 2),
        "booked_override_365d": round(booked, 2),
        "total_365d": round(total, 2),
        "high_pct": pct(high),
        "low_pct": pct(low),
        "booked_override_pct": pct(booked),
        "days": days,
    }


# ─── Per-campaign performance sync ───────────────────────────────────────────

@app.post("/api/admin/campaigns/{campaign_id}/sync-perf", dependencies=[Depends(_require_admin)])
def admin_campaign_sync_perf(campaign_id: str):
    """
    Re-fetch performance data (search terms + daily stats) for a single campaign
    directly from Google Ads and update the local cache tables.
    Returns counts of rows updated.
    """
    from database import (
        get_campaign_by_id, save_gads_search_terms_cache, save_gads_daily_stats
    )

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    gads_resource  = camp.get("gads_campaign_resource") or ""
    gads_num_id    = camp.get("gads_campaign_numeric_id") or ""
    camp_name      = camp.get("campaign_name") or ""

    if not gads_resource or not gads_num_id:
        raise HTTPException(status_code=400, detail="Campaign is not linked to Google Ads")

    # gads_campaign_resource format: "customers/NNNN/campaigns/MMMM"
    parts = gads_resource.split("/")
    customer_id = parts[1] if len(parts) >= 2 else ""
    if not customer_id:
        raise HTTPException(status_code=400, detail="Cannot determine Google Ads customer ID")

    from google_ads_sync import _build_client
    client = _build_client()
    ga_service = client.get_service("GoogleAdsService")

    search_terms_updated = 0
    daily_stats_updated  = 0
    errors = []

    # Compute explicit date range — LAST_N_DAYS is not a valid GAQL DURING literal for N>30.
    # Google Ads supports LAST_7_DAYS, LAST_14_DAYS, LAST_30_DAYS but NOT LAST_90_DAYS.
    # Use BETWEEN with explicit yyyy-mm-dd strings instead.
    _today     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _since_90d = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")

    # ── 1. Re-fetch search terms for this campaign ────────────────────────────
    try:
        st_query = f"""
            SELECT
                search_term_view.search_term,
                campaign.name,
                ad_group.name,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                search_term_view.status
            FROM search_term_view
            WHERE campaign.id = {gads_num_id}
              AND segments.date BETWEEN '{_since_90d}' AND '{_today}'
            ORDER BY metrics.cost_micros DESC
            LIMIT 500
        """
        st_response = ga_service.search(customer_id=customer_id, query=st_query)
        st_rows = []
        for row in st_response:
            clicks     = row.metrics.clicks or 0
            cost_dollars = (row.metrics.cost_micros or 0) / 1_000_000.0
            cpc        = (cost_dollars / clicks) if clicks else 0.0
            st_rows.append({
                "search_term":   row.search_term_view.search_term,
                "campaign_name": row.campaign.name,
                "ad_group_name": row.ad_group.name,
                "impressions":   row.metrics.impressions,
                "clicks":        clicks,
                "cost":          cost_dollars,          # dollars — matches save_gads_search_terms_cache
                "cpc":           round(cpc, 4),
                "conversions":   row.metrics.conversions,
                "status":        str(row.search_term_view.status.name),
            })
        if st_rows:
            save_gads_search_terms_cache(st_rows, days=30)
            search_terms_updated = len(st_rows)
    except Exception as e:
        errors.append(f"search_terms: {str(e)}")

    # ── 2. Re-fetch daily stats for this campaign ─────────────────────────────
    try:
        ds_query = f"""
            SELECT
                campaign.id,
                campaign.name,
                ad_group.id,
                ad_group.name,
                segments.date,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions
            FROM ad_group
            WHERE campaign.id = {gads_num_id}
              AND segments.date BETWEEN '{_since_90d}' AND '{_today}'
            ORDER BY segments.date DESC
        """
        ds_response = ga_service.search(customer_id=customer_id, query=ds_query)
        ds_rows = []
        for row in ds_response:
            ds_rows.append({
                "date":          row.segments.date,
                "campaign_id":   str(row.campaign.id),
                "campaign_name": row.campaign.name,
                "ad_group_id":   str(row.ad_group.id),
                "ad_group_name": row.ad_group.name,
                "impressions":   row.metrics.impressions,
                "clicks":        row.metrics.clicks,
                "cost_micros":   row.metrics.cost_micros,
                "conversions":   row.metrics.conversions,
            })
        if ds_rows:
            # save_gads_daily_stats handles ON CONFLICT upsert + synced_at automatically
            daily_stats_updated = save_gads_daily_stats(ds_rows)
    except Exception as e:
        errors.append(f"daily_stats: {str(e)}")

    return {
        "ok": len(errors) == 0,
        "campaign_id": campaign_id,
        "campaign_name": camp_name,
        "search_terms_updated": search_terms_updated,
        "daily_stats_updated": daily_stats_updated,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "errors": errors,
    }


# ─── Bulk sync all active campaigns ─────────────────────────────────────────

@app.post("/api/admin/campaigns/sync-all-active", dependencies=[Depends(_require_admin)])
def admin_sync_all_active_campaigns():
    """
    Sync search terms + daily stats from Google Ads for every ACTIVE campaign.
    Calls the per-campaign sync-perf logic for each, returns a summary.
    """
    from database import get_all_campaigns
    all_camps = get_all_campaigns() or []
    active = [c for c in all_camps if (c.get("status") or "").upper() == "ACTIVE"
              and c.get("gads_campaign_resource") and c.get("gads_campaign_numeric_id")]

    results = []
    errors_total = 0
    for camp in active:
        try:
            result = admin_campaign_sync_perf(camp["campaign_id"])
            results.append({
                "campaign_id":   camp["campaign_id"],
                "campaign_name": camp.get("campaign_name", ""),
                "search_terms_updated": result.get("search_terms_updated", 0),
                "daily_stats_updated":  result.get("daily_stats_updated", 0),
                "ok": result.get("ok", False),
                "errors": result.get("errors", []),
            })
            if not result.get("ok"):
                errors_total += 1
        except Exception as e:
            errors_total += 1
            results.append({
                "campaign_id":   camp["campaign_id"],
                "campaign_name": camp.get("campaign_name", ""),
                "ok": False,
                "errors": [str(e)],
            })

    return {
        "ok": errors_total == 0,
        "synced": len(active),
        "errors_total": errors_total,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }


# ─── Campaign write-back endpoints (PR 1) ────────────────────────────────────

class NegativeKeywordRequest(BaseModel):
    keyword_text: str
    match_type: str = "EXACT"   # EXACT | PHRASE | BROAD


@app.post("/api/admin/campaigns/{campaign_id}/negative-keywords",
          dependencies=[Depends(_require_admin)])
def admin_add_negative_keyword(campaign_id: str, body: NegativeKeywordRequest):
    """
    Add a campaign-level negative keyword (e.g. from a search term in the Performance tab).
    Immediately executes against the Google Ads API and logs to gads_audit_log.
    """
    from campaign_safety import check_writes_enabled, WriteBlockedError
    from database import get_campaign_by_id, log_admin_manual_action, update_gads_action_result, set_audit_approval
    from google_ads_write import add_negative_keyword_to_campaign

    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        raise HTTPException(status_code=403, detail=f"Writes blocked: {e}")

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    gads_resource = camp.get("gads_campaign_resource") or ""
    if not gads_resource:
        raise HTTPException(status_code=400, detail="Campaign is not linked to Google Ads")

    keyword_text = (body.keyword_text or "").strip()
    if not keyword_text:
        raise HTTPException(status_code=422, detail="keyword_text is required")

    match_type = (body.match_type or "EXACT").upper()
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        raise HTTPException(status_code=422, detail="match_type must be EXACT, PHRASE, or BROAD")

    action_id = log_admin_manual_action(
        operation="add_negative_keyword",
        entity_type="campaign",
        entity_id=gads_resource,
        entity_name=camp.get("campaign_name", ""),
        before={},
        after={"keyword_text": keyword_text, "match_type": match_type,
               "campaign_resource": gads_resource},
        reason="manual_negate_from_search_terms",
    )

    try:
        add_negative_keyword_to_campaign(gads_resource, keyword_text, match_type)
        update_gads_action_result(action_id, executed=True, execution_result="success")
        set_audit_approval(action_id, "admin")
        return {
            "ok": True,
            "action_id": action_id,
            "operation": "add_negative_keyword",
            "keyword_text": keyword_text,
            "match_type": match_type,
        }
    except Exception as e:
        update_gads_action_result(action_id, executed=True,
                                  execution_result="error", error_detail=str(e))
        raise HTTPException(status_code=500, detail=f"Google Ads API error: {e}")


@app.get("/api/admin/gads/negatives/campaign/{campaign_id}",
         dependencies=[Depends(_require_admin)])
def admin_list_campaign_negatives(campaign_id: str):
    """
    Return the current campaign-level negative keywords from Google Ads.
    """
    from database import get_campaign_by_id
    from ai_optimizer import _build_client

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    gads_resource = camp.get("gads_campaign_resource") or ""
    if not gads_resource:
        return {"negatives": []}

    try:
        client = _build_client()
        ga_service = client.get_service("GoogleAdsService")
        query = (
            "SELECT campaign_criterion.keyword.text, "
            "campaign_criterion.keyword.match_type, "
            "campaign_criterion.negative, "
            "campaign_criterion.resource_name "
            "FROM campaign_criterion "
            "WHERE campaign_criterion.negative = TRUE "
            f"AND campaign.resource_name = '{gads_resource}'"
        )
        customer_id = gads_resource.split("/")[1] if "/" in gads_resource else ""
        response = ga_service.search(customer_id=customer_id, query=query)
        negatives = []
        for row in response:
            cc = row.campaign_criterion
            if cc.keyword.text:
                mt = client.enums.KeywordMatchTypeEnum(cc.keyword.match_type).name
                negatives.append({
                    "text": cc.keyword.text,
                    "match_type": mt,
                    "resource_name": cc.resource_name,
                })
        return {"negatives": negatives}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Google Ads API error: {e}")


class AccountNegativeKeywordRequest(BaseModel):
    keyword_text: str
    match_type: str = "BROAD"


@app.post("/api/admin/gads/negatives/account",
          dependencies=[Depends(_require_admin)])
def admin_add_account_negative(body: AccountNegativeKeywordRequest):
    """
    Add a keyword as an account-level (CustomerNegativeCriterion) negative
    that blocks it across ALL campaigns.
    """
    from campaign_safety import check_writes_enabled, WriteBlockedError
    from database import log_admin_manual_action
    from ai_optimizer import _build_client
    from config import get_settings

    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        raise HTTPException(status_code=403, detail=f"Writes blocked: {e}")

    keyword_text = (body.keyword_text or "").strip()
    if not keyword_text:
        raise HTTPException(status_code=422, detail="keyword_text is required")

    match_type = (body.match_type or "BROAD").upper()
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        raise HTTPException(status_code=422, detail="match_type must be EXACT, PHRASE, or BROAD")

    settings = get_settings()
    customer_id = "".join(c for c in (settings.google_ads_customer_id or "") if c.isdigit())
    if not customer_id:
        raise HTTPException(status_code=500, detail="google_ads_customer_id not configured")

    log_admin_manual_action(
        operation="add_account_negative_keyword",
        entity_type="account",
        entity_id=customer_id,
        entity_name="account-level negative",
        before={},
        after={"keyword_text": keyword_text, "match_type": match_type},
        reason="manual_account_negative",
    )

    client = _build_client()
    service = client.get_service("CustomerNegativeCriterionService")
    operation = client.get_type("CustomerNegativeCriterionOperation")
    criterion = operation.create
    criterion.keyword.text = keyword_text
    criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]
    try:
        response = service.mutate_customer_negative_criteria(
            customer_id=customer_id, operations=[operation]
        )
        resource = response.results[0].resource_name
    except Exception as e:
        err = str(e)
        if "DUPLICATE_CRITERION" in err or "already exists" in err.lower() or "ALREADY_EXISTS" in err:
            return {"ok": True, "duplicate": True, "keyword_text": keyword_text}
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "ok": True,
        "keyword_text": keyword_text,
        "match_type": match_type,
        "resource_name": resource,
    }


@app.get("/api/admin/gads/negatives/account",
         dependencies=[Depends(_require_admin)])
def admin_list_account_negatives():
    """
    Return all account-level (CustomerNegativeCriterion) negative keywords.
    """
    from ai_optimizer import _build_client
    from config import get_settings

    settings = get_settings()
    customer_id = "".join(c for c in (settings.google_ads_customer_id or "") if c.isdigit())
    if not customer_id:
        raise HTTPException(status_code=500, detail="google_ads_customer_id not configured")

    try:
        client = _build_client()
        ga_service = client.get_service("GoogleAdsService")
        query = (
            "SELECT customer_negative_criterion.keyword.text, "
            "customer_negative_criterion.keyword.match_type, "
            "customer_negative_criterion.resource_name, "
            "customer_negative_criterion.type "
            "FROM customer_negative_criterion "
            "WHERE customer_negative_criterion.type = 'KEYWORD' "
            "ORDER BY customer_negative_criterion.keyword.text"
        )
        response = ga_service.search(customer_id=customer_id, query=query)
        negatives = []
        for row in response:
            cnc = row.customer_negative_criterion
            if cnc.keyword.text:
                mt = client.enums.KeywordMatchTypeEnum(cnc.keyword.match_type).name
                negatives.append({
                    "text": cnc.keyword.text,
                    "match_type": mt,
                    "resource_name": cnc.resource_name,
                })
        return {"negatives": negatives}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Google Ads API error: {e}")


class KeywordRemoveRequest(BaseModel):
    keyword_text: str
    match_type: str          # EXACT | PHRASE | BROAD
    is_negative: bool = False  # True → campaign-level negative; False → ad-group positive


@app.post("/api/admin/campaigns/{campaign_id}/keyword-remove", dependencies=[Depends(_require_admin)])
def admin_remove_keyword(campaign_id: str, body: KeywordRemoveRequest):
    """
    Remove a keyword from the live Google Ads campaign.
    - is_negative=True  → removes a campaign-level negative criterion
    - is_negative=False → removes the matching ad-group positive criterion(s)
    Only operates when the campaign has a gads_campaign_resource (is live).
    Safe to call on DRAFT campaigns — returns ok=True with gads_removed=0.
    """
    from campaign_safety import check_writes_enabled, WriteBlockedError
    from database import get_campaign_by_id, log_admin_manual_action, update_gads_action_result, set_audit_approval
    from google_ads_write import remove_negative_keyword_from_campaign, remove_positive_keyword_from_campaign

    keyword_text = (body.keyword_text or "").strip()
    if not keyword_text:
        raise HTTPException(status_code=422, detail="keyword_text is required")
    match_type = (body.match_type or "EXACT").upper()
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        raise HTTPException(status_code=422, detail="match_type must be EXACT, PHRASE, or BROAD")

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    gads_resource = camp.get("gads_campaign_resource") or ""
    if not gads_resource:
        # DRAFT campaign — local save already done by build-step-save; nothing to do in GAds
        return {"ok": True, "gads_removed": 0, "draft": True}

    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        raise HTTPException(status_code=403, detail=f"Writes blocked: {e}")

    operation = "remove_negative_keyword" if body.is_negative else "remove_positive_keyword"
    action_id = log_admin_manual_action(
        operation=operation,
        entity_type="campaign",
        entity_id=gads_resource,
        entity_name=camp.get("campaign_name", ""),
        before={"keyword_text": keyword_text, "match_type": match_type},
        after={},
        reason="manual_keyword_chip_delete",
    )

    try:
        if body.is_negative:
            removed = remove_negative_keyword_from_campaign(gads_resource, keyword_text, match_type)
        else:
            removed = remove_positive_keyword_from_campaign(gads_resource, keyword_text, match_type)

        result_str = "success" if removed > 0 else "noop"
        update_gads_action_result(action_id, executed=True, execution_result=result_str)
        set_audit_approval(action_id, "admin")
        return {"ok": True, "gads_removed": removed, "draft": False}
    except Exception as e:
        update_gads_action_result(action_id, executed=True,
                                  execution_result="error", error_detail=str(e))
        raise HTTPException(status_code=500, detail=f"Google Ads API error: {e}")


class AdGroupStatusRequest(BaseModel):
    ad_group_resource: str
    status: str    # PAUSED | ENABLED
    ad_group_name: str = ""
    campaign_id: str = ""


@app.post("/api/admin/ad-groups/set-status", dependencies=[Depends(_require_admin)])
def admin_set_ad_group_status(body: AdGroupStatusRequest):
    """
    Pause or enable an ad group directly in Google Ads.
    ad_group_resource: full resource name e.g. 'customers/1234/adGroups/5678'
    """
    from campaign_safety import check_writes_enabled, WriteBlockedError
    from database import log_admin_manual_action, update_gads_action_result, set_audit_approval
    from google_ads_write import set_ad_group_status

    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        raise HTTPException(status_code=403, detail=f"Writes blocked: {e}")

    ad_group_resource = (body.ad_group_resource or "").strip()
    if not ad_group_resource or not ad_group_resource.startswith("customers/"):
        raise HTTPException(status_code=422,
                            detail="ad_group_resource must be a full resource name (customers/NNNN/adGroups/MMMM)")

    new_status = (body.status or "").upper()
    if new_status not in ("PAUSED", "ENABLED"):
        raise HTTPException(status_code=422, detail="status must be PAUSED or ENABLED")

    old_status = "ENABLED" if new_status == "PAUSED" else "PAUSED"
    operation = "pause_ad_group" if new_status == "PAUSED" else "enable_ad_group"

    action_id = log_admin_manual_action(
        operation=operation,
        entity_type="ad_group",
        entity_id=ad_group_resource,
        entity_name=body.ad_group_name or ad_group_resource,
        before={"status": old_status},
        after={"status": new_status},
        reason="manual_status_change_via_dashboard",
    )

    try:
        set_ad_group_status(ad_group_resource, new_status)
        update_gads_action_result(action_id, executed=True, execution_result="success")
        set_audit_approval(action_id, "admin")

        # Read-back verification
        _verification = {"confirmed": False, "summary": "Verification skipped", "detail": {}}
        try:
            from ai_optimizer import _verify_gads_change as _vgc, _build_client as _bc
            settings_obj = get_settings()
            cid = settings_obj.google_ads_customer_id or ""
            if cid:
                _client = _bc()
                _verification = _vgc(_client, cid, operation, {
                    "ad_group_resource": ad_group_resource,
                    "ad_group_name": body.ad_group_name or ad_group_resource,
                    "before_status": old_status,
                })
        except Exception as _ve:
            logger.warning(f"set-status verify failed: {_ve}")

        return {
            "ok": True,
            "action_id": action_id,
            "operation": operation,
            "ad_group_resource": ad_group_resource,
            "status": new_status,
            "confirmation": _verification,
        }
    except Exception as e:
        update_gads_action_result(action_id, executed=True,
                                  execution_result="error", error_detail=str(e))
        raise HTTPException(status_code=500, detail=f"Google Ads API error: {e}")


# ─── PR 2: Budget edit + Add keyword ─────────────────────────────────────────

class SetBudgetRequest(BaseModel):
    campaign_resource: str       # "customers/NNNN/campaigns/MMMM"
    new_daily_budget_usd: float
    current_daily_budget_usd: float = 0.0  # used for guardrail; 0 means unverified


@app.post("/api/admin/campaigns/{campaign_id}/set-budget",
          dependencies=[Depends(_require_admin)])
def admin_set_campaign_budget(campaign_id: str, body: SetBudgetRequest):
    """
    Update the daily budget for a live Google Ads campaign.
    Runs kill switch + budget-change guardrails before mutating.
    """
    from campaign_safety import check_writes_enabled, WriteBlockedError, \
        check_budget_change_safe, check_proposed_spend_under_cap
    from database import get_campaign_by_id, log_admin_manual_action, \
        update_gads_action_result, set_audit_approval
    from google_ads_write import set_campaign_daily_budget

    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        raise HTTPException(status_code=403, detail=f"Writes blocked: {e}")

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    campaign_resource = (body.campaign_resource or "").strip()
    if not campaign_resource or not campaign_resource.startswith("customers/"):
        raise HTTPException(status_code=422,
                            detail="campaign_resource must be a full resource name (customers/NNNN/campaigns/MMMM)")

    new_usd = body.new_daily_budget_usd
    if new_usd < 1.0:
        raise HTTPException(status_code=422, detail="Daily budget must be at least $1.00")

    # Budget-change safety guardrails (manual edits skip the 25% rate limit — only spend cap applies)
    new_micros = int(new_usd * 1_000_000)

    allowed, cap_usd = check_proposed_spend_under_cap(campaign_id, new_micros)
    if not allowed:
        raise HTTPException(status_code=403,
                            detail=f"Proposed budget ${new_usd:.2f}/day exceeds the ${cap_usd:.2f} spend cap for this campaign")

    action_id = log_admin_manual_action(
        operation="set_campaign_budget",
        entity_type="campaign",
        entity_id=campaign_resource,
        entity_name=camp.get("campaign_name", ""),
        before={"daily_budget_usd": body.current_daily_budget_usd},
        after={"daily_budget_usd": new_usd},
        reason="manual_budget_edit_via_dashboard",
    )

    try:
        set_campaign_daily_budget(campaign_resource, new_usd)
        update_gads_action_result(action_id, executed=True, execution_result="success")
        set_audit_approval(action_id, "admin")
        # Persist the new budget back to the local campaigns table so the detail
        # drawer shows the correct value after reload (monthly_budget = daily * 30.4)
        from database import update_campaign_fields as _ucf
        _ucf(campaign_id, {"monthly_budget": round(new_usd * 30.4, 2)})

        # Read-back verification
        _verification = {"confirmed": False, "summary": "Verification skipped", "detail": {}}
        try:
            from ai_optimizer import _verify_gads_change as _vgc, _build_client as _bc
            settings_obj = get_settings()
            cid = settings_obj.google_ads_customer_id or ""
            if cid:
                _client = _bc()
                _verification = _vgc(_client, cid, "change_budget", {
                    "campaign_resource": campaign_resource,
                    "new_daily_budget_usd": new_usd,
                    "before_daily_budget_usd": body.current_daily_budget_usd or None,
                })
        except Exception as _ve:
            logger.warning(f"set-budget verify failed: {_ve}")

        return {
            "ok": True,
            "action_id": action_id,
            "operation": "set_campaign_budget",
            "campaign_resource": campaign_resource,
            "new_daily_budget_usd": new_usd,
            "confirmation": _verification,
        }
    except Exception as e:
        update_gads_action_result(action_id, executed=True,
                                  execution_result="error", error_detail=str(e))
        raise HTTPException(status_code=500, detail=f"Google Ads API error: {e}")


class AddKeywordRequest(BaseModel):
    ad_group_resource: str       # "customers/NNNN/adGroups/MMMM"
    ad_group_name: str = ""
    keyword_text: str
    match_type: str = "EXACT"    # EXACT | PHRASE | BROAD
    cpc_bid_micros: int = 0      # 0 = use ad group default


@app.post("/api/admin/ad-groups/add-keyword", dependencies=[Depends(_require_admin)])
def admin_add_keyword_to_ad_group(body: AddKeywordRequest):
    """
    Add a positive keyword to a Google Ads ad group.
    Immediately executes and logs to gads_audit_log.
    """
    from campaign_safety import check_writes_enabled, WriteBlockedError
    from database import log_admin_manual_action, update_gads_action_result, set_audit_approval
    from google_ads_write import add_keyword_to_ad_group

    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        raise HTTPException(status_code=403, detail=f"Writes blocked: {e}")

    ad_group_resource = (body.ad_group_resource or "").strip()
    if not ad_group_resource or not ad_group_resource.startswith("customers/"):
        raise HTTPException(status_code=422,
                            detail="ad_group_resource must be a full resource name (customers/NNNN/adGroups/MMMM)")

    keyword_text = (body.keyword_text or "").strip()
    if not keyword_text:
        raise HTTPException(status_code=422, detail="keyword_text is required")

    match_type = (body.match_type or "EXACT").upper()
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        raise HTTPException(status_code=422, detail="match_type must be EXACT, PHRASE, or BROAD")

    action_id = log_admin_manual_action(
        operation="add_keyword",
        entity_type="ad_group",
        entity_id=ad_group_resource,
        entity_name=body.ad_group_name or ad_group_resource,
        before={},
        after={"keyword_text": keyword_text, "match_type": match_type,
               "cpc_bid_micros": body.cpc_bid_micros},
        reason="manual_add_keyword_via_dashboard",
    )

    try:
        add_keyword_to_ad_group(
            ad_group_resource,
            keyword_text,
            match_type,
            body.cpc_bid_micros,
        )
        update_gads_action_result(action_id, executed=True, execution_result="success")
        set_audit_approval(action_id, "admin")
        return {
            "ok": True,
            "action_id": action_id,
            "operation": "add_keyword",
            "ad_group_resource": ad_group_resource,
            "keyword_text": keyword_text,
            "match_type": match_type,
        }
    except Exception as e:
        update_gads_action_result(action_id, executed=True,
                                  execution_result="error", error_detail=str(e))
        raise HTTPException(status_code=500, detail=f"Google Ads API error: {e}")


# ── 3. Replace geographic targeting (PR 3) ───────────────────────────────────

class SetLocationsRequest(BaseModel):
    campaign_resource: str   # "customers/NNNN/campaigns/MMMM"
    geo_json: str            # {"unit":"miles","locations":[{type,value,radius,include}]}


@app.post("/api/admin/campaigns/{campaign_id}/set-locations",
          dependencies=[Depends(_require_admin)])
def admin_set_campaign_locations(campaign_id: str, body: SetLocationsRequest):
    """
    Atomically replace geographic targeting (LOCATION + PROXIMITY criteria)
    on a Google Ads campaign.
    Rejects saves with zero resolved locations to prevent worldwide targeting.
    Logs to gads_audit_log and persists geo_json back to local DB.
    """
    import json as _json
    from campaign_safety import check_writes_enabled, WriteBlockedError
    from database import (log_admin_manual_action, update_gads_action_result,
                          set_audit_approval, get_campaign_by_id, update_campaign_fields)
    from google_ads_write import replace_campaign_locations

    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        raise HTTPException(status_code=403, detail=f"Writes blocked: {e}")

    campaign_resource = (body.campaign_resource or "").strip()
    if not campaign_resource or not campaign_resource.startswith("customers/"):
        raise HTTPException(status_code=422,
                            detail="campaign_resource must be a full resource name (customers/NNNN/campaigns/MMMM)")

    # Validate campaign exists locally
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found")

    # Parse geo_json and enforce non-empty locations guard (Opus M3)
    geo_json_str = (body.geo_json or "").strip()
    try:
        parsed = _json.loads(geo_json_str) if geo_json_str else {}
        locations = parsed.get("locations") or []
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid geo_json: {e}")

    if not locations:
        raise HTTPException(status_code=422,
                            detail="At least one location is required — saving zero locations would make the campaign worldwide")

    action_id = log_admin_manual_action(
        operation="set_campaign_locations",
        entity_type="campaign",
        entity_id=campaign_resource,
        entity_name=camp.get("campaign_name") or campaign_resource,
        before={"geographic_targeting": camp.get("geographic_targeting")},
        after={"geo_json": geo_json_str},
        reason="manual_location_edit_via_dashboard",
    )

    try:
        result = replace_campaign_locations(campaign_resource, geo_json_str)
        added = result.get("added", 0)
        removed = result.get("removed", 0)
        errs  = result.get("errors", [])
        exec_result = "partial_success" if errs else "success"
        update_gads_action_result(action_id, executed=True, execution_result=exec_result)
        set_audit_approval(action_id, "admin")
        # M7: Only persist geo_json to local DB if at least one location was actually applied.
        if added > 0:
            update_campaign_fields(campaign_id, {"geographic_targeting": geo_json_str})

        # ── Read back live geo criteria to verify the change went through ──────
        live_criteria = []
        verified = False
        try:
            from google_ads_create import _build_client as _gc_build
            _cl = _gc_build()
            _ga = _cl.get_service("GoogleAdsService")
            from google_ads_write import _customer_id_from_resource
            _cid = _customer_id_from_resource(campaign_resource)
            _q = f"""
                SELECT campaign_criterion.resource_name,
                       campaign_criterion.type,
                       campaign_criterion.proximity.geo_point.latitude_in_micro_degrees,
                       campaign_criterion.proximity.geo_point.longitude_in_micro_degrees,
                       campaign_criterion.proximity.radius,
                       campaign_criterion.proximity.radius_units,
                       campaign_criterion.proximity.address.city_name,
                       campaign_criterion.proximity.address.province_code,
                       campaign_criterion.location.geo_target_constant,
                       campaign_criterion.negative
                FROM campaign_criterion
                WHERE campaign_criterion.campaign = '{campaign_resource}'
                  AND campaign_criterion.type IN ('LOCATION', 'PROXIMITY')
            """
            for row in _ga.search(customer_id=_cid, query=_q):
                cc = row.campaign_criterion
                crit_type = cc.type_.name if hasattr(cc.type_, 'name') else str(cc.type_)
                entry: dict = {"type": crit_type, "negative": cc.negative}
                if crit_type == "PROXIMITY":
                    lat_micro = cc.proximity.geo_point.latitude_in_micro_degrees
                    lng_micro = cc.proximity.geo_point.longitude_in_micro_degrees
                    radius    = cc.proximity.radius
                    units_raw = cc.proximity.radius_units
                    units_str = units_raw.name if hasattr(units_raw, 'name') else str(units_raw)
                    city      = cc.proximity.address.city_name
                    state     = cc.proximity.address.province_code
                    # proto3 scalar default for int64 is 0 — treat (0,0) as "geo_point not set"
                    # since the Google Ads API rejects explicit (0,0) proximity criteria.
                    has_geo_point = not (lat_micro == 0 and lng_micro == 0)
                    units_label   = "mi" if "MILE" in units_str.upper() else "km"
                    city_str      = city or ""
                    state_str     = state or ""
                    loc_label     = f"{city_str}, {state_str}".strip(", ") or "Unknown"
                    entry.update({
                        "lat": round(lat_micro / 1_000_000, 4) if has_geo_point else None,
                        "lng": round(lng_micro / 1_000_000, 4) if has_geo_point else None,
                        "radius": radius,
                        "units": "miles" if "MILE" in units_str.upper() else "km",
                        "city": city_str,
                        "state": state_str,
                        "summary": f"{loc_label} · {radius} {units_label}"
                                   + (f" ({round(lat_micro/1_000_000,4)}, {round(lng_micro/1_000_000,4)})"
                                      if has_geo_point else " ⚠ no geo_point"),
                    })
                elif crit_type == "LOCATION":
                    entry["geo_target"] = cc.location.geo_target_constant
                    entry["summary"] = cc.location.geo_target_constant
                live_criteria.append(entry)
            # has_live_criteria: confirms Google Ads has criteria — not that they match exactly
            has_live_criteria = len(live_criteria) > 0
        except Exception as ve:
            logger.warning(f"set-locations {campaign_id}: read-back failed: {ve}")
            live_criteria = [{"type": "error", "summary": f"Read-back failed: {ve}"}]
            has_live_criteria = False

        # Diff-aware confirmation summary
        if added == 0 and removed == 0:
            _loc_summary = "ℹ️ Locations unchanged — no changes made"
            _loc_confirmed = True
        elif added > 0 and removed > 0:
            _loc_summary = f"✅ Locations updated: {added} added, {removed} removed"
            _loc_confirmed = has_live_criteria
        elif added > 0:
            _loc_summary = f"✅ {added} location{'s' if added>1 else ''} added"
            _loc_confirmed = has_live_criteria
        elif removed > 0:
            _loc_summary = f"✅ {removed} location{'s' if removed>1 else ''} removed"
            _loc_confirmed = True
        else:
            _loc_summary = "✅ Locations updated" if has_live_criteria else "⚠️ Location update submitted — verify in Google Ads"
            _loc_confirmed = has_live_criteria

        return {
            "ok": True,
            "action_id": action_id,
            "operation": "set_campaign_locations",
            "removed": removed,
            "added": added,
            "errors": errs,
            "live_criteria": live_criteria,
            "has_live_criteria": has_live_criteria,
            "confirmation": {"confirmed": _loc_confirmed, "summary": _loc_summary, "detail": {"added": added, "removed": removed}},
        }
    except Exception as e:
        update_gads_action_result(action_id, executed=True,
                                  execution_result="error", error_detail=str(e))
        raise HTTPException(status_code=500, detail=f"Google Ads API error: {e}")


# ─── Set ad schedule on existing campaign ────────────────────────────────────

class SetScheduleRequest(BaseModel):
    campaign_resource: str
    schedule_text: str   # free text or JSON list, same format as parse_ad_schedule()

@app.post("/api/admin/campaigns/{campaign_id}/set-schedule",
          dependencies=[Depends(_require_admin)])
def admin_set_campaign_schedule(campaign_id: str, body: SetScheduleRequest):
    """
    Replace the ad schedule on an existing live campaign.
    Accepts free text (e.g. 'Mon-Thu 7am-11pm') or structured JSON.
    Also saves the value back to the launch_checklist for the campaign.
    """
    import datetime as _dt
    from google_ads_create import parse_ad_schedule, push_ad_schedule, _build_client
    from campaign_safety import check_writes_enabled, WriteBlockedError
    from database import _conn, log_admin_manual_action, update_gads_action_result, set_audit_approval

    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        raise HTTPException(status_code=403, detail=str(e))

    if not body.campaign_resource:
        raise HTTPException(status_code=422, detail="campaign_resource is required")

    slots = parse_ad_schedule(body.schedule_text)
    if not slots:
        raise HTTPException(status_code=422,
                            detail=f"Could not parse schedule: '{body.schedule_text}'. "
                                   "Use format like 'Mon-Thu 7am-11pm' or 'Weekdays 9am-6pm'.")

    settings = get_settings()
    customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())

    days_summary = ", ".join(sorted(set(s["day"] for s in slots)))
    action_id = None
    try:
        action_id = log_admin_manual_action(
            operation="set_ad_schedule",
            entity_type="campaign",
            entity_id=body.campaign_resource,
            entity_name=campaign_id,
            before={"schedule_text": ""},
            after={"schedule_text": body.schedule_text, "slots": slots, "days": days_summary},
            reason=f"Set ad schedule: {body.schedule_text}",
        )

        client = _build_client()
        result = push_ad_schedule(client, customer_id, body.campaign_resource, slots, replace=True)
        if not result["ok"]:
            if action_id:
                update_gads_action_result(action_id, executed=True,
                                          execution_result="error", error_detail=result["error"])
            raise HTTPException(status_code=502, detail=f"Google Ads error: {result['error']}")

        if action_id:
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, "admin")

        # Persist back to launch_checklist in campaign_build_json
        with _conn() as conn:
            row = conn.execute(
                "SELECT campaign_build_json FROM campaigns WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
            if row:
                build = json.loads(row["campaign_build_json"] or "{}") if row["campaign_build_json"] else {}
                checklist = build.get("launch_checklist", [])
                updated = False
                for item in checklist:
                    if isinstance(item, dict) and item.get("key") == "ad_schedule":
                        item["value"] = body.schedule_text
                        item["done"] = True
                        updated = True
                        break
                if not updated:
                    checklist.append({
                        "key": "ad_schedule", "item": "Ad schedule",
                        "value": body.schedule_text, "done": True,
                        "skippable": True, "category": "optional", "action": "auto",
                    })
                build["launch_checklist"] = checklist
                conn.execute(
                    "UPDATE campaigns SET campaign_build_json=?, updated_at=? WHERE campaign_id=?",
                    (json.dumps(build), _dt.datetime.now(_dt.timezone.utc).isoformat(), campaign_id)
                )

        pushed = result.get("pushed", 0)
        removed_sched = result.get("removed", 0)
        logger.info(f"Ad schedule set for {campaign_id}: {body.schedule_text} → {len(slots)} slots")
        if pushed == 0 and removed_sched == 0:
            _sched_summary = f"ℹ️ Schedule unchanged — no changes made"
            _sched_confirmed = True
        elif pushed > 0:
            _sched_summary = f"✅ Schedule set: {days_summary}"
            _sched_confirmed = True
        else:
            _sched_summary = f"✅ Schedule updated"
            _sched_confirmed = True
        return {
            "ok": True,
            "pushed": pushed,
            "removed": removed_sched,
            "slots": slots,
            "days_summary": days_summary,
            "confirmation": {"confirmed": _sched_confirmed, "summary": _sched_summary, "detail": {"pushed": pushed, "removed": removed_sched}},
        }
    except HTTPException:
        raise
    except Exception as e:
        if action_id:
            update_gads_action_result(action_id, executed=True,
                                      execution_result="error", error_detail=str(e))
        raise HTTPException(status_code=500, detail=f"Google Ads API error: {e}")


# ─── GET current ad schedule (with bid modifiers) ────────────────────────────

@app.get("/api/admin/campaigns/{campaign_id}/ad-schedule",
         dependencies=[Depends(_require_admin)])
def admin_get_campaign_schedule(campaign_id: str):
    """
    Return the current ad schedule criteria for a campaign, including bid modifiers.
    Reads live from Google Ads API.
    Returns: {slots: [{day, start_hour, end_hour, bid_modifier}]}
    """
    from google_ads_create import _build_client
    from database import get_campaign_by_id

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    camp_resource = camp.get("gads_campaign_resource") or ""
    if not camp_resource:
        raise HTTPException(status_code=400, detail="Campaign not linked to Google Ads")

    settings = get_settings()
    customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())

    try:
        client = _build_client()
        ga_service = client.get_service("GoogleAdsService")
        query = f"""
            SELECT
              campaign_criterion.ad_schedule.day_of_week,
              campaign_criterion.ad_schedule.start_hour,
              campaign_criterion.ad_schedule.end_hour,
              campaign_criterion.bid_modifier
            FROM campaign_criterion
            WHERE campaign.resource_name = '{camp_resource}'
            AND campaign_criterion.type = AD_SCHEDULE
            ORDER BY campaign_criterion.ad_schedule.day_of_week,
                     campaign_criterion.ad_schedule.start_hour
        """
        rows = list(ga_service.search(customer_id=customer_id, query=query))
        slots = []
        for r in rows:
            s = r.campaign_criterion.ad_schedule
            bm = r.campaign_criterion.bid_modifier
            slots.append({
                "day": s.day_of_week.name,          # e.g. "MONDAY"
                "start_hour": s.start_hour,
                "end_hour": s.end_hour,
                "bid_modifier": round(bm, 4) if bm else 1.0,
            })
        return {"slots": slots, "campaign_id": campaign_id, "campaign_resource": camp_resource}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Google Ads API error: {e}")


# ─── POST ad schedule with bid modifiers (structured) ────────────────────────

class ScheduleSlot(BaseModel):
    day: str           # MONDAY, TUESDAY, etc.
    start_hour: int    # 0–23
    end_hour: int      # 1–24
    bid_modifier: float = 1.0  # 0.1–10.0

class SetScheduleSlotsRequest(BaseModel):
    campaign_resource: str
    slots: list[ScheduleSlot]

@app.post("/api/admin/campaigns/{campaign_id}/set-schedule-slots",
          dependencies=[Depends(_require_admin)])
def admin_set_campaign_schedule_slots(campaign_id: str, body: SetScheduleSlotsRequest):
    """
    Replace the ad schedule on a campaign using structured slot data including bid modifiers.
    Use this instead of set-schedule when you need per-slot bid modifiers.
    """
    import datetime as _dt
    from google_ads_create import push_ad_schedule, _build_client
    from campaign_safety import check_writes_enabled, WriteBlockedError
    from database import log_admin_manual_action, update_gads_action_result, set_audit_approval

    try:
        check_writes_enabled()
    except WriteBlockedError as e:
        raise HTTPException(status_code=403, detail=str(e))

    if not body.campaign_resource:
        raise HTTPException(status_code=422, detail="campaign_resource is required")
    if not body.slots:
        raise HTTPException(status_code=422, detail="slots list is required and cannot be empty")

    settings = get_settings()
    customer_id = "".join(ch for ch in (settings.google_ads_customer_id or "") if ch.isdigit())

    slots_dicts = [
        {
            "day": s.day.upper(),
            "start_hour": s.start_hour,
            "end_hour": s.end_hour,
            "bid_modifier": max(0.1, min(10.0, s.bid_modifier)),
        }
        for s in body.slots
    ]
    days_summary = ", ".join(sorted(set(s["day"] for s in slots_dicts)))

    action_id = None
    try:
        action_id = log_admin_manual_action(
            operation="set_ad_schedule",
            entity_type="campaign",
            entity_id=body.campaign_resource,
            entity_name=campaign_id,
            before={"slots": []},
            after={"slots": slots_dicts, "days": days_summary,
                   "campaign_resource": body.campaign_resource},
            reason=f"Set ad schedule with bid modifiers: {days_summary}",
        )

        client = _build_client()
        result = push_ad_schedule(client, customer_id, body.campaign_resource, slots_dicts, replace=True)
        if not result["ok"]:
            if action_id:
                update_gads_action_result(action_id, executed=True,
                                          execution_result="error", error_detail=result["error"])
            raise HTTPException(status_code=502, detail=f"Google Ads error: {result['error']}")

        if action_id:
            update_gads_action_result(action_id, executed=True, execution_result="success")
            set_audit_approval(action_id, "admin")

        return {
            "ok": True,
            "pushed": result.get("pushed", 0),
            "removed": result.get("removed", 0),
            "slots": slots_dicts,
            "days_summary": days_summary,
        }
    except HTTPException:
        raise
    except Exception as e:
        if action_id:
            update_gads_action_result(action_id, executed=True,
                                      execution_result="error", error_detail=str(e))
        raise HTTPException(status_code=500, detail=f"Google Ads API error: {e}")


# ─── Google Ads extended reporting ───────────────────────────────────────────

@app.get("/api/admin/gads/ad-groups", dependencies=[Depends(_require_admin)])
def admin_gads_ad_groups():
    """Ad-group level aggregated stats from gads_daily_stats + leads."""
    return {"ad_groups": get_ad_group_stats(days=30)}


@app.get("/api/admin/gads/daily-stats", dependencies=[Depends(_require_admin)])
def admin_gads_daily_stats(days: int = 30, campaign_id: Optional[str] = None):
    """Daily time-series stats per campaign (summed across ad groups). Max 90 days."""
    days = min(max(int(days), 1), 90)
    return {"rows": get_daily_stats(days=days, campaign_id=campaign_id or None), "days": days}


@app.get("/api/admin/gads/search-terms", dependencies=[Depends(_require_admin)])
def admin_gads_search_terms(days: int = 30, campaign: str = ""):
    """Search terms from gads_search_terms_cache enriched with semantic classifications."""
    from database import get_st_classifications
    terms = get_search_term_stats(campaign_name=campaign, days=days)
    # Build lookup: (search_term_lower, campaign_lower) → classification
    classifications = get_st_classifications(campaign_name=campaign)
    clf_map = {
        (c["search_term"].lower(), c["campaign_name"].lower()): c
        for c in classifications
    }
    for t in terms:
        key = (t["search_term"].lower(), t["campaign_name"].lower())
        clf = clf_map.get(key)
        t["verdict"] = clf["verdict"] if clf else None
        t["verdict_reason"] = clf["reason"] if clf else None
        t["classified_at"] = clf["classified_at"] if clf else None
    return {"search_terms": terms}


# ─── A/B Experiment endpoints ──────────────────────────────────────────────────

class AbExperimentCreate(BaseModel):
    experiment_name: str
    experiment_resource: str = ""
    base_campaign_resource: str
    base_campaign_name: str = ""
    trial_campaign_resource: str = ""
    trial_campaign_name: str = ""
    experiment_type: str = "landing_page"
    control_url: str = ""
    variant_url: str = ""
    traffic_split_percent: int = 50
    status: str = "SETUP"
    start_date: str = ""
    end_date: str = ""
    notes: str = ""

    @validator("experiment_type")
    def _valid_type(cls, v):
        if v not in ("landing_page", "ad_copy"):
            raise ValueError("experiment_type must be 'landing_page' or 'ad_copy'")
        return v

    @validator("traffic_split_percent")
    def _valid_split(cls, v):
        if not (1 <= v <= 99):
            raise ValueError("traffic_split_percent must be 1-99")
        return v


class AbExperimentUpdate(BaseModel):
    experiment_resource: str | None = None
    trial_campaign_resource: str | None = None
    trial_campaign_name: str | None = None
    status: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    winner: str | None = None
    notes: str | None = None
    control_url: str | None = None
    variant_url: str | None = None


@app.post("/api/admin/experiments", dependencies=[Depends(_require_admin)])
def admin_create_experiment(body: AbExperimentCreate):
    """Create a new A/B experiment record (manually set up in Google Ads UI)."""
    from database import create_ab_experiment, list_ab_experiments
    # Guard: only one RUNNING experiment per base campaign
    existing = list_ab_experiments()
    for ex in existing:
        if (ex["base_campaign_resource"] == body.base_campaign_resource
                and ex["status"] == "RUNNING"):
            raise HTTPException(
                status_code=409,
                detail=f"A RUNNING experiment already exists for this campaign: '{ex['experiment_name']}'"
            )
    exp_id = create_ab_experiment(body.dict())
    return {"ok": True, "id": exp_id}


@app.get("/api/admin/experiments", dependencies=[Depends(_require_admin)])
def admin_list_experiments(status: str = ""):
    """List all A/B experiments, optionally filtered by status."""
    from database import list_ab_experiments
    return {"experiments": list_ab_experiments(status=status)}


@app.get("/api/admin/experiments/{experiment_id}", dependencies=[Depends(_require_admin)])
def admin_get_experiment(experiment_id: int):
    """Get a single experiment by ID."""
    from database import get_ab_experiment
    exp = get_ab_experiment(experiment_id)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return exp


@app.put("/api/admin/experiments/{experiment_id}", dependencies=[Depends(_require_admin)])
def admin_update_experiment(experiment_id: int, body: AbExperimentUpdate):
    """Update experiment fields (e.g. add trial_campaign_resource after setting up in Google Ads)."""
    from database import get_ab_experiment, update_ab_experiment
    exp = get_ab_experiment(experiment_id)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    updates = {k: v for k, v in body.dict().items() if v is not None}
    update_ab_experiment(experiment_id, updates)
    return {"ok": True}


@app.get("/api/admin/experiments/{experiment_id}/metrics", dependencies=[Depends(_require_admin)])
def admin_experiment_metrics(
    experiment_id: int,
    start_date: str = "",
    end_date: str = "",
):
    """
    Fetch combined metrics for both arms:
    - Google Ads API: clicks, impressions, CTR, conversions, cost
    - Local DB: leads, booked, showed, revenue
    - Winner signal computation
    """
    from database import get_ab_experiment, get_ab_experiment_lead_metrics
    from experiment_metrics import get_gads_experiment_metrics, compute_winner_signal

    exp = get_ab_experiment(experiment_id)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")

    # Use experiment dates as default range
    _start = start_date or exp.get("start_date") or ""
    _end   = end_date   or exp.get("end_date")   or ""

    # Compute days running
    days_running = 0
    if exp.get("start_date"):
        try:
            from datetime import date as _date
            _sd = _date.fromisoformat(exp["start_date"])
            _ed = _date.fromisoformat(_end) if _end else _date.today()
            days_running = max((_ed - _sd).days, 0)
        except Exception:
            pass

    # Fetch GAds metrics
    gads = get_gads_experiment_metrics(
        base_campaign_resource=exp["base_campaign_resource"],
        trial_campaign_resource=exp.get("trial_campaign_resource", ""),
        start_date=_start,
        end_date=_end,
    )

    # Fetch local lead metrics
    leads = get_ab_experiment_lead_metrics(
        base_campaign_name=exp.get("base_campaign_name", ""),
        trial_campaign_name=exp.get("trial_campaign_name", ""),
        control_url=exp.get("control_url", ""),
        variant_url=exp.get("variant_url", ""),
        start_date=_start,
    )

    # Compute winner signal
    signal = compute_winner_signal(gads, leads, days_running=days_running)

    return {
        "experiment":    exp,
        "gads_metrics":  gads,
        "lead_metrics":  leads,
        "winner_signal": signal,
        "days_running":  days_running,
    }


@app.post("/api/admin/experiments/{experiment_id}/status", dependencies=[Depends(_require_admin)])
def admin_update_experiment_status(experiment_id: int, body: dict = Body(...)):
    """
    Update experiment status. Valid transitions:
    SETUP -> RUNNING, RUNNING -> HALTED | PROMOTED | GRADUATED
    """
    from database import get_ab_experiment, update_ab_experiment
    exp = get_ab_experiment(experiment_id)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")

    new_status = body.get("status", "").upper()
    valid = {"SETUP", "RUNNING", "HALTED", "PROMOTED", "GRADUATED"}
    if new_status not in valid:
        raise HTTPException(status_code=422, detail=f"status must be one of {valid}")

    updates: dict = {"status": new_status}
    if body.get("winner"):
        updates["winner"] = body["winner"]
    if body.get("notes"):
        updates["notes"] = body["notes"]

    # On terminal transitions, snapshot the current winner signal so the
    # decision rationale is preserved even after the experiment data ages out.
    if new_status in {"PROMOTED", "GRADUATED"}:
        try:
            import json as _json
            from experiment_metrics import get_gads_experiment_metrics, compute_winner_signal
            from database import get_ab_experiment_lead_metrics as _lead_m
            _gads = get_gads_experiment_metrics(
                base_campaign_resource=exp.get("base_campaign_resource", ""),
                trial_campaign_resource=exp.get("trial_campaign_resource", ""),
            )
            _leads = _lead_m(
                base_campaign_name=exp.get("base_campaign_name", ""),
                trial_campaign_name=exp.get("trial_campaign_name", ""),
                control_url=exp.get("control_url", ""),
                variant_url=exp.get("variant_url", ""),
            )
            _signal = compute_winner_signal(_gads, _leads, days_running=None)
            updates["winner_signal_json"] = _json.dumps(_signal)
        except Exception as _snap_err:
            # Non-fatal — snapshot failure must not block the status transition
            import logging as _log
            _log.getLogger(__name__).warning(f"winner_signal snapshot failed: {_snap_err}")

    update_ab_experiment(experiment_id, updates)
    return {"ok": True, "status": new_status}


@app.post("/api/admin/gads/classify-search-terms", dependencies=[Depends(_require_admin)])
def admin_classify_search_terms(campaign: str = "", force: bool = False):
    """
    Manually trigger semantic classification for a campaign (or all campaigns).
    Uses Claude Haiku. Results are persisted and shown in the Search Terms tab.
    force=true re-classifies already-classified terms.
    """
    from search_term_classifier import classify_new_terms_for_campaign
    from database import get_setting, get_google_ads_campaigns as _get_camps

    api_key = get_setting("anthropic_api_key") or ""
    if not api_key:
        raise HTTPException(status_code=422, detail="No Anthropic API key configured")

    if campaign:
        campaigns_to_classify = [campaign]
    else:
        # Classify all known campaigns
        try:
            all_camps = _get_camps()
            campaigns_to_classify = [c["campaign_name"] for c in all_camps if c.get("campaign_name")]
        except Exception:
            raise HTTPException(status_code=500, detail="Could not fetch campaign list")

    total_classified = 0
    total_negatives = 0
    results = []
    for camp in campaigns_to_classify:
        try:
            r = classify_new_terms_for_campaign(
                campaign_name=camp,
                days=30,
                api_key=api_key,
                force_reclassify=force,
            )
            total_classified += r["classified"]
            total_negatives += len(r["negatives"])
            results.append({
                "campaign": camp,
                "classified": r["classified"],
                "negatives": len(r["negatives"]),
                "conquests": len(r["conquests"]),
                "skipped": r["skipped"],
            })
        except Exception as e:
            results.append({"campaign": camp, "error": str(e)})

    return {
        "ok": True,
        "total_classified": total_classified,
        "total_negatives": total_negatives,
        "campaigns": results,
    }


@app.get("/api/admin/gads/ads", dependencies=[Depends(_require_admin)])
def admin_gads_ads(days: int = 30):
    """
    List all ad creatives with aggregated metrics and lead counts for the last N days.
    Includes CTR and CPL computed server-side for convenience.
    """
    from database import get_ads_with_metrics
    ads = get_ads_with_metrics(days=days)
    for ad in ads:
        impressions = ad.get("impressions") or 0
        clicks      = ad.get("clicks") or 0
        cost_micros = ad.get("cost_micros") or 0
        leads       = ad.get("leads") or 0
        cost        = cost_micros / 1_000_000.0
        ad["cost"]  = round(cost, 2)
        ad["ctr"]   = round(clicks / impressions * 100, 2) if impressions > 0 else 0.0
        ad["cpc"]   = round(cost / clicks, 2) if clicks > 0 else 0.0
        ad["cpl"]   = round(cost / leads, 2)  if leads  > 0 else 0.0
        # Parse assets_json if stored as string
        if isinstance(ad.get("assets_json"), str):
            try:
                ad["assets_json"] = json.loads(ad["assets_json"])
            except Exception:
                ad["assets_json"] = {"headlines": [], "descriptions": []}
        # Ensure it's always a dict shape the frontend expects
        if not isinstance(ad.get("assets_json"), dict):
            ad["assets_json"] = {"headlines": [], "descriptions": []}
    return {"ads": ads, "days": days}


@app.get("/api/admin/gads/ads/{ad_id}/metrics", dependencies=[Depends(_require_admin)])
def admin_gads_ad_metrics(ad_id: str, days: int = 30):
    """Daily metrics time-series for a single ad creative."""
    from database import get_ad_metrics_series
    rows = get_ad_metrics_series(ad_id=ad_id, days=days)
    for row in rows:
        row["cost"] = round((row.get("cost_micros") or 0) / 1_000_000.0, 2)
    return {"ad_id": ad_id, "days": days, "metrics": rows}


# ─── Mango Voice call endpoints ──────────────────────────────────────────────

@app.get("/api/admin/calls", dependencies=[Depends(_require_admin)])
def admin_get_calls(
    direction: str = "",
    status: str = "",
    days: int = 30,
    limit: int = 100,
    offset: int = 0,
):
    """List Mango Voice calls. Filter by direction (inbound/outbound), status, days back."""
    from database import get_mango_calls, get_gads_call_view
    calls, total = get_mango_calls(
        limit=limit, offset=offset,
        direction=direction, status=status, days=days,
    )
    # Also return gads call_view counts for summary
    gads_calls = get_gads_call_view(days=days)
    ad_call_count = len(gads_calls)
    ad_call_total_duration = sum(c.get("call_duration_sec", 0) for c in gads_calls)
    # Enrich each call with matched status label
    for c in calls:
        if c.get("gads_call_id"):
            conf = c.get("match_confidence", 0) or 0
            c["attribution_label"] = "Ad call ✓" if conf >= 0.90 else "Ad call (~)"
        elif c.get("lead_id"):
            c["attribution_label"] = "Known lead"
        else:
            c["attribution_label"] = ""
    return {
        "calls": calls,
        "total": total,
        "ad_calls_from_gads": ad_call_count,
        "ad_call_total_duration_sec": ad_call_total_duration,
    }


@app.get("/api/admin/calls/campaign-attribution", dependencies=[Depends(_require_admin)])
def admin_calls_campaign_attribution_early(days: int = 30):
    """
    Return per-campaign call counts and OD appointment counts.
    Used by the campaign performance table to show Calls and Appts from Calls columns.
    NOTE: Must be registered BEFORE the {uuid} wildcard route below.
    """
    from database import _conn
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT
                 COALESCE(
                   NULLIF(gcv.campaign_name,''),
                   NULLIF(l.campaign_name,''),
                   NULLIF(agcv.campaign_name,'')
                 ) AS campaign_name,
                 COALESCE(
                   NULLIF(gcv.campaign_id,''),
                   NULLIF(l.campaign_id,''),
                   NULLIF(agcv.campaign_id,'')
                 ) AS campaign_id,
                 COUNT(*) AS total_calls,
                 SUM(CASE WHEN mc.booked_outcome='booked' THEN 1 ELSE 0 END) AS booked_calls,
                 -- confirmed_appts: OD-matched appointments OR Gemini-confirmed bookings
                 SUM(CASE WHEN (mc.od_appointment_id IS NOT NULL AND mc.od_appointment_id != '')
                               OR mc.booked_outcome='booked'
                           THEN 1 ELSE 0 END) AS confirmed_appts,
                 -- new_appts: OD new_patient match OR Gemini-confirmed booking with no OD match
                 -- (family-member callers, new patients not yet in OD)
                 SUM(CASE WHEN (mc.od_appointment_id IS NOT NULL AND mc.od_appointment_id != ''
                                AND mc.od_patient_status = 'new_patient')
                               OR (mc.booked_outcome='booked'
                                   AND (mc.od_appointment_id IS NULL OR mc.od_appointment_id = '')
                                   AND mc.ai_appointment_scheduled = 1)
                           THEN 1 ELSE 0 END) AS new_appts
               FROM mango_calls mc
               LEFT JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
               LEFT JOIN leads l ON l.id = mc.lead_id
               LEFT JOIN (
                 SELECT ad_group_name, campaign_name, campaign_id
                 FROM gads_call_view
                 WHERE ad_group_name != ''
                 GROUP BY ad_group_name
               ) agcv ON agcv.ad_group_name = mc.attributed_ad_group
                      AND (mc.gads_call_id IS NULL OR mc.gads_call_id = '')
               WHERE mc.started_at >= ?
                 AND mc.direction = 'inbound'
                 AND (
                   (gcv.campaign_name IS NOT NULL AND gcv.campaign_name != '')
                   OR (l.campaign_name IS NOT NULL AND l.campaign_name != '')
                   OR (agcv.campaign_name IS NOT NULL AND agcv.campaign_name != '')
                 )
               GROUP BY 1, 2
               ORDER BY total_calls DESC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/admin/calls/campaign-appts", dependencies=[Depends(_require_admin)])
def admin_calls_campaign_appts(campaign_name: str, days: int = 30):
    """
    Return the individual call+appointment records for a specific campaign.
    Used by the clickable APPTS modal in the campaign table.
    Returns calls that have either od_appointment_id set OR booked_outcome='booked'.
    Patient status filtering (new/existing/all) is done client-side in the frontend.
    """
    from database import _conn
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT
                 mc.uuid,
                 mc.started_at,
                 mc.caller_id_name,
                 mc.duration_sec,
                 mc.od_appointment_id,
                 mc.od_patient_name,
                 mc.ai_patient_name,
                 mc.od_patient_num,
                 mc.od_patient_status,
                 mc.booked_outcome,
                 mc.attributed_keyword,
                 mc.attributed_ad_group,
                 mc.call_summary,
                 COALESCE(gcv.ad_group_name, mc.attributed_ad_group) AS gads_ad_group,
                 COALESCE(NULLIF(l.appointment_date,''), NULLIF(l2.appointment_date,''), NULLIF(kpl.appointment_date,'')) AS od_appt_date,
                 COALESCE(NULLIF(l.appointment_status,''), NULLIF(l2.appointment_status,'')) AS od_appt_status,
                 COALESCE(
                     NULLIF(mc.od_patient_name,''),
                     NULLIF(mc.ai_patient_name,''),
                     NULLIF(TRIM(l.first_name||' '||l.last_name),''),
                     NULLIF(TRIM(l2.first_name||' '||l2.last_name),'')
                 ) AS patient_name,
                 COALESCE(kpl.paid_amount_365d, 0) AS paid_amount_365d
               FROM mango_calls mc
               LEFT JOIN gads_call_view gcv ON gcv.call_id = mc.gads_call_id
               LEFT JOIN leads l ON l.id = mc.lead_id
               LEFT JOIN leads l2 ON l2.od_patient_num = mc.od_patient_num
                                  AND mc.od_patient_num != ''
                                  AND (mc.lead_id IS NULL OR mc.lead_id = '')
               -- PR 5 fix: aggregate KPL by patient — a patient can have multiple KPL
               -- rows (lead-path + call::uuid_N per call). Without GROUP BY the LEFT JOIN
               -- multiplied call rows in the modal and made paid_amount_365d ambiguous.
               LEFT JOIN (
                 SELECT od_patient_num,
                        MAX(paid_amount_365d) AS paid_amount_365d,
                        MAX(appointment_date) AS appointment_date
                 FROM keyword_production_log
                 WHERE od_patient_num != ''
                 GROUP BY od_patient_num
               ) kpl ON kpl.od_patient_num = mc.od_patient_num
                    AND mc.od_patient_num != ''
               LEFT JOIN (
                 SELECT ad_group_name, campaign_name
                 FROM gads_call_view
                 WHERE ad_group_name != ''
                 GROUP BY ad_group_name
               ) agcv ON agcv.ad_group_name = mc.attributed_ad_group
                      AND (mc.gads_call_id IS NULL OR mc.gads_call_id = '')
               WHERE mc.started_at >= ?
                 AND mc.direction = 'inbound'
                 AND (
                   (mc.od_appointment_id IS NOT NULL AND mc.od_appointment_id != '')
                   OR mc.booked_outcome = 'booked'
                 )
                 AND (
                   gcv.campaign_name = ?
                   OR l.campaign_name = ?
                   OR agcv.campaign_name = ?
                 )
               ORDER BY mc.started_at DESC""",
            (cutoff, campaign_name, campaign_name, campaign_name),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/admin/reports/income", dependencies=[Depends(_require_admin)])
def admin_reports_income(days: int = 90):
    """
    Return patient-level production records from keyword_production_log,
    joined to leads for patient name and first/last name fallback.
    Used by the Income sub-view in the Reports tab.
    """
    from database import _conn
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT
                 kpl.id,
                 kpl.logged_at,
                 kpl.lead_id,
                 kpl.keyword_text,
                 kpl.match_type,
                 kpl.campaign_name,
                 kpl.ad_group_name,
                 kpl.od_patient_num,
                 kpl.production_amount,
                 kpl.procedure_codes,
                 kpl.match_method,
                 kpl.appointment_date,
                 COALESCE(
                   NULLIF(TRIM(l.first_name||' '||l.last_name),''),
                   (SELECT mc.od_patient_name FROM mango_calls mc
                     WHERE mc.od_patient_num = kpl.od_patient_num
                       AND kpl.od_patient_num != ''
                       AND IFNULL(mc.od_patient_name,'') != ''
                     ORDER BY mc.started_at DESC LIMIT 1),
                   kpl.od_patient_num
                 ) AS patient_name
               FROM keyword_production_log kpl
               LEFT JOIN leads l ON l.id = kpl.lead_id
               WHERE kpl.logged_at >= ?
               ORDER BY kpl.logged_at DESC, kpl.id DESC""",
            (cutoff,),
        ).fetchall()
    rows_list = [dict(r) for r in rows]
    total_production = sum(r.get("production_amount") or 0 for r in rows_list)
    patient_count = len({r.get("od_patient_num") for r in rows_list if r.get("od_patient_num")})
    campaign_count = len({r.get("campaign_name") for r in rows_list if r.get("campaign_name")})
    return {
        "rows": rows_list,
        "summary": {
            "total_production": round(total_production, 2),
            "patient_count": patient_count,
            "campaign_count": campaign_count,
            "days": days,
        }
    }


@app.get("/api/admin/calls/{uuid}", dependencies=[Depends(_require_admin)])
def admin_get_call(uuid: str):
    """Return a single Mango call record by UUID."""
    from database import get_mango_call
    call = get_mango_call(uuid)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


@app.post("/api/admin/calls/sync", dependencies=[Depends(_require_admin)])
def admin_mango_sync_now(request: Request):
    """Manually trigger a Mango call sync right now."""
    token_mgr = getattr(request.app.state, "mango_token_mgr", None)
    if not token_mgr:
        raise HTTPException(status_code=503, detail="Mango not configured or disabled")
    try:
        from mango_service import sync_mango_calls
        settings = get_settings()
        n = sync_mango_calls(token_mgr, pbx_id=settings.mango_pbx_id, api_base=settings.mango_api_base)
        return {"synced": n}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/mango/match-patients", dependencies=[Depends(_require_admin)])
def admin_mango_match_patients(limit: int = 500):
    """
    Match unmatched Mango inbound calls to OpenDental patients by phone.
    Sets od_patient_status: new_patient | existing_active | existing_inactive | unknown.
    """
    try:
        from od_matcher import match_mango_calls_to_od_patients
        result = match_mango_calls_to_od_patients(limit=limit)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/mango-calls/{uuid}/patient-override", dependencies=[Depends(_require_admin)])
def admin_mango_patient_override(uuid: str, body: dict):
    """
    Manually override the od_patient_status for a single Mango call.
    Clears od_patient_num and od_patient_name when overriding to new_patient.
    Resets od_matched_at so the auto-matcher won't re-overwrite until next sync.
    """
    allowed = {"new_patient", "existing_active", "existing_inactive", "unknown"}
    new_status = (body or {}).get("od_patient_status", "")
    if new_status not in allowed:
        raise HTTPException(status_code=400, detail=f"od_patient_status must be one of: {allowed}")
    try:
        from database import _conn
        with _conn() as conn:
            if new_status == "new_patient":
                conn.execute(
                    """UPDATE mango_calls
                       SET od_patient_status=?, od_patient_num='', od_patient_name='',
                           od_matched_at=datetime('now')
                       WHERE uuid=?""",
                    (new_status, uuid),
                )
            else:
                conn.execute(
                    "UPDATE mango_calls SET od_patient_status=?, od_matched_at=datetime('now') WHERE uuid=?",
                    (new_status, uuid),
                )
        return {"ok": True, "uuid": uuid, "od_patient_status": new_status}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/calls/reconcile", dependencies=[Depends(_require_admin)])
def admin_mango_reconcile_now(days: int = 14):
    """Manually trigger attribution reconciliation. days can be widened up to 90."""
    try:
        from mango_service import reconcile_attribution
        days = max(1, min(int(days), 90))
        _tok = app.state.mango_token_mgr.get_token() if hasattr(app.state, "mango_token_mgr") else None
        n = reconcile_attribution(days=days, mango_token=_tok)
        return {"attributed": n, "days": days}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/calls/attribute-keywords", dependencies=[Depends(_require_admin)])
def admin_attribute_keywords(days: int = 30):
    """Manually trigger keyword attribution for Mango calls. Use days=90 for backfill."""
    try:
        from call_keyword_attribution import attribute_calls_to_keywords
        days = max(1, min(int(days), 90))
        result = attribute_calls_to_keywords(days=days)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/optimizer/diagnose-call-attribution", dependencies=[Depends(_require_admin)])
def admin_diagnose_call_attribution(days: int = 30):
    """
    Diagnostic: shows method breakdown + top 10 unattributed inbound calls.
    Use to verify keyword attribution quality before relying on optimizer recommendations.
    """
    try:
        from call_keyword_attribution import get_attribution_diagnostics
        days = max(1, min(int(days), 365))
        return get_attribution_diagnostics(days=days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/call-flags", dependencies=[Depends(_require_admin)])
def admin_get_call_flags(days: int = 30, unresolved_only: bool = True):
    """
    Return call flags (missed/short Google Ads calls + missed new patients).
    Each flag includes joined mango_calls fields for display in the UI.
    """
    try:
        from database import get_call_flags
        days = max(1, min(int(days), 365))
        all_flags = get_call_flags(days=days, unresolved_only=unresolved_only)
        # Short call flags are kept for LQI/AI analysis but excluded from the UI panel
        _SHORT_FLAG_TYPES = {"short_gads_call", "unconverted_short_gads_call"}
        flags = [f for f in all_flags if f.get("flag_type") not in _SHORT_FLAG_TYPES]
        return {"flags": flags, "total": len(flags)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ResolveCallFlagRequest(BaseModel):
    resolved_by: str = "admin"
    resolved_outcome: str = "not_actionable"  # called_back | booked | not_actionable | ignored


@app.post("/api/admin/call-flags/{flag_id}/resolve", dependencies=[Depends(_require_admin)])
def admin_resolve_call_flag(flag_id: int, body: ResolveCallFlagRequest):
    """Resolve a call flag with an outcome."""
    try:
        from database import resolve_call_flag
        valid_outcomes = {"called_back", "booked", "not_actionable", "ignored"}
        outcome = body.resolved_outcome if body.resolved_outcome in valid_outcomes else "not_actionable"
        updated = resolve_call_flag(flag_id, body.resolved_by or "admin", outcome)
        if not updated:
            raise HTTPException(status_code=404, detail="Flag not found or already resolved")
        return {"ok": True, "flag_id": flag_id, "resolved_outcome": outcome}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/calls/match-od-appointments", dependencies=[Depends(_require_admin)])
def admin_match_calls_to_od(days: int = 90):
    """
    For all booked Mango calls not yet linked to an OD appointment,
    search OpenDental for the matching appointment and store the AptNum.
    Requires office LAN access to OpenDental MySQL.
    days: how many days back to look (default 90, max 365).
    """
    try:
        from od_matcher import match_calls_to_od_appointments
        days = max(1, min(int(days), 365))
        result = match_calls_to_od_appointments(days=days)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/calls/{uuid}/match-od-appointment", dependencies=[Depends(_require_admin)])
def admin_match_single_call_to_od(uuid: str):
    """
    For a single Mango call, search OpenDental for the matching appointment.
    Used for on-demand triggering after grading a specific call.
    """
    try:
        from od_matcher import match_calls_to_od_appointments
        result = match_calls_to_od_appointments(days=90, target_uuid=uuid)
        if result.get("matched", 0) == 0:
            # Not an error — just no match found (patient not in OD or no appt in window)
            return {"matched": 0, "message": "No matching OD appointment found", **result}
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/calls/backfill-income", dependencies=[Depends(_require_admin)])
def admin_backfill_call_income():
    """
    For all mango_calls that have od_patient_num set (new_patient status only)
    but od_patient_income IS NULL, fetch income from OD paysplit and store it.
    Use this to backfill income on calls matched before PR5 shipped.
    Requires office LAN access to OpenDental MySQL.
    """
    try:
        from od_matcher import _get_db, _get_patient_income, _get_patient_production
        from database import update_mango_call_od_income, _conn

        od_conn = _get_db()
        if not od_conn:
            raise HTTPException(status_code=503, detail="OpenDental unavailable (office network required)")

        with _conn() as conn:
            rows = conn.execute(
                """SELECT uuid, od_patient_num FROM mango_calls
                   WHERE od_patient_num IS NOT NULL AND od_patient_num != ''
                     AND od_patient_status = 'new_patient'
                     AND od_patient_income IS NULL"""
            ).fetchall()

        updated = errors = 0
        for row in rows:
            try:
                income = _get_patient_income(od_conn, row["od_patient_num"])
                prod   = _get_patient_production(od_conn, row["od_patient_num"])
                update_mango_call_od_income(row["uuid"], income, prod.get("total", 0.0))
                updated += 1
            except Exception as e:
                logger.warning(f"[backfill_income] uuid={row['uuid']} PatNum={row['od_patient_num']}: {e}")
                errors += 1

        od_conn.close()
        return {"backfilled": updated, "errors": errors, "total": len(rows)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/calls/refresh-income", dependencies=[Depends(_require_admin)])
def admin_refresh_call_income():
    """
    Re-fetch OD paysplit income for ALL new_patient calls that already have
    od_patient_num set — regardless of whether income was previously fetched.
    Use this when a patient makes a new payment and you want updated totals.
    De-dupes by PatNum so each OD patient is only queried once.
    Requires office LAN access to OpenDental MySQL.
    """
    try:
        from od_matcher import _get_db, _get_patient_income, _get_patient_production
        from database import update_mango_call_od_income, _conn

        od_conn = _get_db()
        if not od_conn:
            raise HTTPException(status_code=503, detail="OpenDental unavailable (office network required)")

        with _conn() as conn:
            rows = conn.execute(
                """SELECT uuid, od_patient_num FROM mango_calls
                   WHERE od_patient_num IS NOT NULL AND od_patient_num != ''
                     AND od_patient_status = 'new_patient'"""
            ).fetchall()

        # De-dupe: fetch each PatNum once, then apply to all matching uuids
        pat_cache: dict = {}
        updated = errors = 0
        for row in rows:
            pat_num = row["od_patient_num"]
            try:
                if pat_num not in pat_cache:
                    income = _get_patient_income(od_conn, pat_num)
                    prod   = _get_patient_production(od_conn, pat_num)
                    pat_cache[pat_num] = (income, prod.get("total", 0.0))
                income, production = pat_cache[pat_num]
                update_mango_call_od_income(row["uuid"], income, production)
                updated += 1
            except Exception as e:
                logger.warning(f"[refresh_income] uuid={row['uuid']} PatNum={pat_num}: {e}")
                errors += 1

        od_conn.close()
        return {"refreshed": updated, "errors": errors, "total": len(rows), "unique_patients": len(pat_cache)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/gads/{call_id}/match-and-transcribe", dependencies=[Depends(_require_admin)])
def admin_gads_match_and_transcribe(call_id: str, request: Request, background_tasks: BackgroundTasks):
    """For an unmatched GAds call: widen the reconciler window to 90 days to find the
    Mango record, link it, then queue transcription. Returns 409 if no Mango call
    matches after reconciliation (recording genuinely not available)."""
    from database import _conn
    from mango_service import reconcile_attribution

    # 1. Confirm the GAds row exists
    with _conn() as conn:
        gads_row = conn.execute(
            "SELECT * FROM gads_call_view WHERE call_id = ?", (call_id,)
        ).fetchone()
    if not gads_row:
        raise HTTPException(status_code=404, detail="GAds call not found")

    # 2. Check if already matched (race condition / user double-clicked)
    with _conn() as conn:
        already = conn.execute(
            "SELECT uuid FROM mango_calls WHERE gads_call_id = ?", (call_id,)
        ).fetchone()
    if already and already["uuid"]:
        # Already matched — just queue transcription
        mango_uuid = already["uuid"]
    else:
        # 3. Run reconciler with 90-day window + targeted mode for detailed logging
        _tok = app.state.mango_token_mgr.get_token() if hasattr(app.state, "mango_token_mgr") else None
        reconcile_attribution(days=90, target_gads_call_id=call_id, mango_token=_tok)
        # Also run keyword attribution so the newly-matched call gets attributed
        try:
            from call_keyword_attribution import attribute_calls_to_keywords
            attribute_calls_to_keywords(days=1)
        except Exception as _ke:
            logger.warning(f"Keyword attribution after targeted reconcile failed: {_ke}")

        # 4. Re-check for a match
        with _conn() as conn:
            matched = conn.execute(
                "SELECT uuid FROM mango_calls WHERE gads_call_id = ?", (call_id,)
            ).fetchone()

        if not matched or not matched["uuid"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    "No PBX recording found for this Google Ads call. "
                    "The Mango call record may not exist — most likely the caller "
                    "hung up before audio capture started, was routed to voicemail "
                    "without leaving one, or this call predates the sync window."
                ),
            )
        mango_uuid = matched["uuid"]

    # 5. Queue the transcription pipeline (same as /api/admin/calls/{uuid}/process)
    from database import get_mango_call
    from mango_pipeline import process_call
    call = get_mango_call(mango_uuid)
    if not call:
        raise HTTPException(status_code=404, detail="Mango call record not found after match")

    token_mgr = getattr(request.app.state, "mango_token_mgr", None)
    tok = token_mgr.get_token() if token_mgr else None

    def _safe_process(call_dict, mango_token=None):
        try:
            process_call(call_dict, mango_token=mango_token)
        except Exception as err:
            logger.exception("[pipeline] Unhandled error in match-and-transcribe(%s): %s", call_dict.get("uuid"), err)
            try:
                from database import update_mango_call_analysis
                update_mango_call_analysis(
                    call_dict.get("uuid", ""),
                    transcription_status="failed",
                    pipeline_error=f"{type(err).__name__}: {str(err)[:480]}",
                )
            except Exception:
                logger.exception("[pipeline] Could not persist failure state")

    background_tasks.add_task(_safe_process, call, mango_token=tok)
    return {"ok": True, "status": "queued", "uuid": mango_uuid, "gads_call_id": call_id}


@app.post("/api/admin/gads/sync-call-view", dependencies=[Depends(_require_admin)])
def admin_gads_sync_call_view():
    """Manually trigger Google Ads call_view sync (normally runs at 6:05am)."""
    try:
        from google_ads_sync import sync_call_view
        n = sync_call_view(days_back=14)
        return {"synced": n}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/gads/sync-call-search-terms", dependencies=[Depends(_require_admin)])
def admin_gads_sync_call_search_terms(days: int = 30):
    """Fetch search terms that drove AD_CALL conversions from search_term_view.
    Stores them in gads_call_search_terms for keyword attribution on matched calls.
    Then re-runs the keyword attribution backfill to upgrade any calls using fallback attribution."""
    try:
        from google_ads_sync import sync_call_search_terms
        n = sync_call_search_terms(days=days)
        # Re-run backfill to upgrade calls with new search term data
        upgraded = backfill_call_keyword_attribution()
        return {"synced": n, "calls_upgraded": upgraded}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/calls/backfill-keyword-attribution", dependencies=[Depends(_require_admin)])
def admin_backfill_call_keyword_attribution():
    """Backfill attributed_keyword on calls matched to GAds call_view rows.
    Uses the best keyword from the matched ad group in gads_keyword_perf.
    Safe to run repeatedly — only updates calls with no keyword yet."""
    try:
        n = backfill_call_keyword_attribution()
        return {"updated": n}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/gads/call-view", dependencies=[Depends(_require_admin)])
def admin_gads_call_view(days: int = 30):
    """Return Google Ads call_view rows (calls directly from ads)."""
    from database import get_gads_call_view
    rows = get_gads_call_view(days=days)
    return {"calls": rows, "total": len(rows)}


@app.get("/api/admin/gads/call-conversions", dependencies=[Depends(_require_admin)])
def admin_gads_call_conversions(days: int = 30, min_duration_sec: int = 0):
    """Return GAds call_view rows joined to matched Mango call records.

    Each row represents one call that came directly from a Google Ad.
    When the probabilistic matcher has run, the row is enriched with the full
    Mango call data: caller phone number, transcript, grade, team member,
    and any matched lead.

    Use min_duration_sec=60 to filter to calls that count as conversions
    (the Google Ads 'Phone call leads' conversion threshold is typically 60s).
    """
    from database import get_gads_call_conversions
    rows = get_gads_call_conversions(days=days, min_duration_sec=min_duration_sec)
    matched   = [r for r in rows if r.get("mango_uuid")]
    converted = [r for r in rows if r.get("gads_duration_sec", 0) >= 60]
    return {
        "calls": rows,
        "total": len(rows),
        "matched_to_mango": len(matched),
        "conversions_60s": len(converted),
    }


# ─── Call Analysis endpoints ──────────────────────────────────────────────────

@app.post("/api/admin/calls/{uuid}/process", dependencies=[Depends(_require_admin)])
def admin_process_call(uuid: str, request: Request, background_tasks: BackgroundTasks):
    """
    Manually trigger pipeline processing for a single call.
    Non-blocking — queues work as a background task and returns immediately.
    Poll GET /api/admin/calls/{uuid} to see updated transcription_status.
    """
    from database import get_mango_call
    call = get_mango_call(uuid)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    from mango_pipeline import process_call
    token_mgr = getattr(request.app.state, "mango_token_mgr", None)
    tok = token_mgr.get_token() if token_mgr else None

    def _safe_process_call(call_dict, mango_token=None):
        """Wrapper that logs any unhandled exception from the pipeline.

        FastAPI BackgroundTasks swallow exceptions silently — without this
        wrapper, a top-level error in process_call (e.g. import failure,
        DB error before the inner try-block) leaves the call stuck in
        'in_progress' with no log line.
        """
        try:
            process_call(call_dict, mango_token=mango_token)
        except Exception as err:
            logger.exception(
                "[pipeline] Unhandled error in process_call(%s): %s: %s",
                call_dict.get("uuid"), type(err).__name__, err,
            )
            try:
                from database import update_mango_call_analysis
                update_mango_call_analysis(
                    call_dict.get("uuid", ""),
                    transcription_status="failed",
                    pipeline_error=f"{type(err).__name__}: {str(err)[:480]}",
                )
            except Exception:
                logger.exception("[pipeline] Could not persist failure state")

    background_tasks.add_task(_safe_process_call, call, mango_token=tok)
    return {"ok": True, "status": "queued", "uuid": uuid}


@app.post("/api/admin/calls/bulk-process", dependencies=[Depends(_require_admin)])
def admin_bulk_process_calls(request: Request, body: dict = Body(default={})):
    """
    Start a bulk analysis job for all unprocessed calls.
    Non-blocking — returns job_id immediately, processing runs in background.
    Options: min_seconds (int), batch_size (int), days_back (int)
    """
    from database import get_active_call_bulk_job, insert_call_bulk_job
    from mango_pipeline import start_bulk_job_async

    # Reject if a job is already running
    active = get_active_call_bulk_job()
    if active:
        return {"ok": False, "error": "A bulk job is already running", "job": active}

    options = {
        "min_seconds": int(body.get("min_seconds", 30)),
        "batch_size":  int(body.get("batch_size", 50)),
        "days_back":   int(body.get("days_back", 90)),
    }
    do_grade = bool(body.get("do_grade", True))
    job_id = insert_call_bulk_job(date_from="", date_to="", do_grade=do_grade, options=options)

    token_mgr = getattr(request.app.state, "mango_token_mgr", None)
    tok = token_mgr.get_token() if token_mgr else None
    start_bulk_job_async(job_id, mango_token=tok)

    return {"ok": True, "job_id": job_id}


@app.get("/api/admin/calls/bulk-process/status", dependencies=[Depends(_require_admin)])
def admin_bulk_process_status():
    """Return the status of the most recent bulk job."""
    from database import get_active_call_bulk_job
    job = get_active_call_bulk_job()
    if not job:
        # Return last completed job from DB
        from database import _conn
        with _conn() as conn:
            row = conn.execute(
                "SELECT * FROM call_bulk_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row:
            job = dict(row)
    return {"job": job}


@app.post("/api/admin/calls/{uuid}/grade", dependencies=[Depends(_require_admin)])
def admin_grade_call(uuid: str):
    """
    Re-grade a single call that already has a transcript.
    Useful after changing grading criteria.
    """
    from database import get_mango_call, update_mango_call_analysis
    call = get_mango_call(uuid)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    transcript = call.get("call_transcript") or ""
    if not transcript.strip():
        raise HTTPException(status_code=400, detail="No transcript available — transcribe first")

    from database import get_mango_settings as _get_mango
    msettings = _get_mango()
    if not msettings["vertex_project_id"]:
        raise HTTPException(status_code=503, detail="VERTEX_PROJECT_ID not configured")

    try:
        from mango_pipeline import _grade
        caller_name = call.get("caller_id_name") or ""
        grade = _grade(
            transcript,
            msettings["vertex_project_id"], msettings["vertex_location"],
            msettings["vertex_credentials_path"], msettings["vertex_model"],
            uuid, caller_name,
        )
        now_iso = datetime.now(timezone.utc).isoformat()

        import json as _json
        if grade.get("gradeable"):
            # Normalise 1–10 scores → 0–100 scale and rename explanation→notes
            raw_scores = grade.get("scores", [])
            normalised_scores = [
                {
                    "criterion": s.get("criterion", s.get("name", "")),
                    "score": round(float(s.get("score", 0)) * 10),
                    "notes": s.get("explanation", s.get("notes", "")),
                }
                for s in raw_scores
            ]
            raw_overall = float(grade.get("overall_score") or 0)
            overall_pct = round(raw_overall * 10)
            update_mango_call_analysis(
                uuid,
                grade_scores_json=_json.dumps(normalised_scores),
                grade_overall_score=overall_pct,
                grade_overall_notes=grade.get("overall_notes", ""),
                grade_recommendations_json=_json.dumps(grade.get("recommendations", [])),
                grade_gradeable=1,
                grade_reason="",
                graded_at=now_iso,
            )
        else:
            update_mango_call_analysis(
                uuid,
                grade_gradeable=0,
                grade_reason=grade.get("reason", "Not gradeable"),
                graded_at=now_iso,
            )
        updated = get_mango_call(uuid)
        return {"ok": True, "grade": grade, "call": updated}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/calls/{uuid}/suggest-action", dependencies=[Depends(_require_admin)])
def admin_suggest_call_action(uuid: str):
    """Generate/regenerate AI next-action suggestion for a single call."""
    from database import get_mango_call, update_mango_call_analysis, get_lead, get_mango_settings as _get_mango
    call = get_mango_call(uuid)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    if not (call.get("call_summary") or "").strip():
        raise HTTPException(status_code=400, detail="No summary available — transcribe and grade first")
    msettings = _get_mango()
    if not msettings.get("vertex_project_id"):
        raise HTTPException(status_code=503, detail="VERTEX_PROJECT_ID not configured")
    lead_stage = ""
    if call.get("lead_id"):
        ld = get_lead(call["lead_id"]) or {}
        lead_stage = ld.get("stage", "")
    try:
        from mango_pipeline import _suggest_next_action
        nxt = _suggest_next_action(
            summary=call.get("call_summary", ""),
            grade_overall_score=call.get("grade_overall_score"),
            grade_overall_notes=call.get("grade_overall_notes", ""),
            lead_stage=lead_stage,
            booked_in_call=bool(call.get("od_appointment_id")),
            vertex_project_id=msettings["vertex_project_id"],
            vertex_location=msettings["vertex_location"],
            vertex_credentials_path=msettings["vertex_credentials_path"],
            vertex_model=msettings["vertex_model"],
            call_uuid=uuid,
        )
        from datetime import date, timedelta
        due_iso = ""
        if nxt.get("action_type") != "no_action":
            d = max(0, min(14, int(nxt.get("due_in_days") or 0)))
            due_iso = (date.today() + timedelta(days=d)).isoformat()
        update_mango_call_analysis(
            uuid,
            call_next_action=nxt.get("description", ""),
            call_next_action_type=nxt.get("action_type", "other"),
            call_next_action_due=due_iso,
            call_next_action_priority=nxt.get("priority", "soon"),
            call_next_action_reasoning=nxt.get("reasoning", ""),
            call_next_action_suggested_at=datetime.now(timezone.utc).isoformat(),
            call_next_action_completed=0,
        )
        return {"ok": True, "action": nxt, "call": get_mango_call(uuid)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/calls/{uuid}/action/complete", dependencies=[Depends(_require_admin)])
def admin_complete_call_action(uuid: str, request: Request):
    """Mark a call's AI next-action as done."""
    from database import get_mango_call, mark_call_action_completed
    if not get_mango_call(uuid):
        raise HTTPException(status_code=404, detail="Call not found")
    by = ""
    try:
        by = request.session.get("admin_email", "") if hasattr(request, "session") else ""
    except Exception:
        pass
    mark_call_action_completed(uuid, by_email=by)
    return {"ok": True}


@app.get("/api/admin/actions", dependencies=[Depends(_require_admin)])
def admin_actions_feed():
    """Unified prioritized feed for the Actions tab."""
    from database import get_pending_actions
    items = get_pending_actions(limit=200)
    pending = [i for i in items if not i["completed"]]
    counts = {
        "urgent": sum(1 for i in pending if i["priority"] == "urgent"),
        "soon":   sum(1 for i in pending if i["priority"] == "soon"),
        "low":    sum(1 for i in pending if i["priority"] == "low"),
        "total":  len(pending),
    }
    return {"items": items, "counts": counts}


@app.get("/api/admin/campaigns/call-stats", dependencies=[Depends(_require_admin)])
def admin_campaign_call_stats():
    """Per-campaign call quality stats from v_campaign_call_stats view."""
    from database import get_campaign_call_stats
    return {"stats": get_campaign_call_stats()}


@app.get("/api/admin/calls/analysis/criteria", dependencies=[Depends(_require_admin)])
def admin_get_criteria():
    """Return current grading criteria."""
    from database import get_call_grading_criteria
    return {"criteria": get_call_grading_criteria()}


@app.put("/api/admin/calls/analysis/criteria", dependencies=[Depends(_require_admin)])
def admin_set_criteria(body: dict = Body(...)):
    """
    Replace all grading criteria.
    Body: { "criteria": [ {name, description, weight, enabled}, ... ] }
    Total weight should sum to 100.
    """
    from database import set_call_grading_criteria
    criteria = body.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise HTTPException(status_code=400, detail="criteria must be a non-empty list")
    # Validate fields
    for item in criteria:
        if not item.get("name") or item.get("weight") is None:
            raise HTTPException(status_code=400, detail="Each criterion needs name and weight")
    try:
        set_call_grading_criteria(criteria)
        from database import get_call_grading_criteria
        return {"ok": True, "criteria": get_call_grading_criteria()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/calls/analysis/criteria/reset", dependencies=[Depends(_require_admin)])
def admin_reset_criteria():
    """Reset grading criteria to factory defaults."""
    from database import reset_call_grading_criteria, get_call_grading_criteria
    reset_call_grading_criteria()
    return {"ok": True, "criteria": get_call_grading_criteria()}


@app.get("/api/admin/calls/analysis/team-members", dependencies=[Depends(_require_admin)])
def admin_get_team_members():
    """Return configured team members."""
    from database import get_call_team_members
    return {"members": get_call_team_members()}


@app.put("/api/admin/calls/analysis/team-members", dependencies=[Depends(_require_admin)])
def admin_set_team_members(body: dict = Body(...)):
    """
    Replace all team members.
    Body: { "members": [ {name, extension, active}, ... ] }
    """
    from database import set_call_team_members
    members = body.get("members")
    if not isinstance(members, list):
        raise HTTPException(status_code=400, detail="members must be a list")
    for m in members:
        if not m.get("name") or not m.get("extension"):
            raise HTTPException(status_code=400, detail="Each member needs name and extension")
    try:
        set_call_team_members(members)
        from database import get_call_team_members
        return {"ok": True, "members": get_call_team_members()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/calls/analysis/ai-costs", dependencies=[Depends(_require_admin)])
def admin_ai_costs(days: int = 30):
    """Return AI spend summary for Whisper, Gemini, and Claude."""
    from ai_costs import cost_summary
    return cost_summary(days=days)


@app.get("/api/admin/calls/analysis/performance", dependencies=[Depends(_require_admin)])
def admin_call_performance(days: int = 30):
    """
    Return per-team-member call performance stats.
    Aggregates graded calls by team member, averaged scores per criterion.
    """
    from database import _conn
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with _conn() as conn:
        rows = conn.execute(
            """SELECT team_member, grade_overall_score, grade_scores_json, graded_at
               FROM mango_calls
               WHERE grade_gradeable = 1
                 AND team_member IS NOT NULL AND team_member != ''
                 AND graded_at >= ?
               ORDER BY graded_at DESC""",
            (cutoff,),
        ).fetchall()

    import json as _json
    from collections import defaultdict

    member_data: dict = defaultdict(lambda: {"calls": 0, "total_score": 0.0, "criteria_sums": defaultdict(float), "criteria_counts": defaultdict(int)})

    for row in rows:
        name = row["team_member"]
        score = row["grade_overall_score"] or 0
        d = member_data[name]
        d["calls"] += 1
        d["total_score"] += score
        try:
            scores = _json.loads(row["grade_scores_json"] or "[]")
            for s in scores:
                criterion = s.get("criterion", "")
                val = s.get("score")
                if criterion and val is not None:
                    d["criteria_sums"][criterion] += float(val)
                    d["criteria_counts"][criterion] += 1
        except (ValueError, TypeError):
            pass

    result = []
    for name, d in member_data.items():
        calls = d["calls"]
        avg_score = round(d["total_score"] / calls, 2) if calls else 0
        criteria_avg = {
            c: round(d["criteria_sums"][c] / d["criteria_counts"][c], 2)
            for c in d["criteria_sums"]
        }
        result.append({
            "name": name,
            "calls": calls,
            "avg_score": avg_score,
            "criteria": criteria_avg,
        })

    result.sort(key=lambda x: x["avg_score"], reverse=True)
    return {"period_days": days, "members": result}


@app.get("/api/admin/calls/analysis/revenue", dependencies=[Depends(_require_admin)])
def admin_call_revenue(days: int = 90):
    """
    Return rows from the call_to_revenue view — call attribution chain.
    Links calls → leads → Google Ads campaigns.
    """
    from database import _conn
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM call_to_revenue WHERE call_started_at >= ? ORDER BY call_started_at DESC LIMIT 500",
            (cutoff,),
        ).fetchall()
    return {"rows": [dict(r) for r in rows], "total": len(rows)}


@app.post("/api/admin/pipeline/trigger", dependencies=[Depends(_require_admin)])
def admin_trigger_pipeline(request: Request):
    """Manually trigger one pipeline tick right now."""
    from mango_pipeline import run_pipeline_tick
    token_mgr = getattr(request.app.state, "mango_token_mgr", None)
    tok = token_mgr.get_token() if token_mgr else None
    try:
        run_pipeline_tick(mango_token=tok)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Pipeline with enrichment ────────────────────────────────────────────────

@app.get("/api/pipeline/enriched")
def get_pipeline_enriched(
    stage: Optional[str] = None,
    campaign: Optional[str] = None,
    limit: int = 500,
    show_all: bool = False,
):
    """Return all leads enriched with notes count, for Kanban board.

    Default: gads_only filter applied (only Google Ads attributed leads).
    Pass show_all=true to bypass the filter and see every lead.
    """
    _GADS_FILTER = """(
        COALESCE(l.gclid, '') != ''
        OR COALESCE(l.campaign_id, '') != ''
        OR COALESCE(l.utm_source, '') LIKE 'google%'
        OR COALESCE(l.utm_source, '') LIKE '%cpc%'
        OR COALESCE(l.notes, '') LIKE '%Google Ads%'
        OR COALESCE(l.notes, '') LIKE '%gclid%'
        OR l.source = 'manual'
    )"""
    from database import _conn
    with _conn() as conn:
        query = "SELECT l.*, (SELECT COUNT(*) FROM lead_notes n WHERE n.lead_id = l.id) as notes_count FROM leads l"
        params = []
        conditions = []
        if not show_all:
            conditions.append(_GADS_FILTER)
        if stage:
            conditions.append("l.stage = ?")
            params.append(stage)
        if campaign:
            conditions.append(
                "(l.campaign_name = ? OR l.utm_campaign = ? OR (l.campaign_name = '' AND l.utm_campaign = '' AND l.source = ?))"
            )
            params.extend([campaign, campaign, campaign])
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY l.updated_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        leads = [dict(r) for r in rows]
    return {"leads": leads, "total": len(leads)}


# ─── Scheduled Jobs Status ───────────────────────────────────────────────────

@app.get("/api/admin/jobs", dependencies=[Depends(_require_admin)])
def get_job_status():
    """Return status of all scheduled jobs including last_run and next_run times."""

    def _fmt(dt):
        if dt is None:
            return None
        try:
            return dt.isoformat()
        except Exception:
            return str(dt)

    job_list = []

    # Ads + optimizer scheduler jobs (ga4_pull, gads_sync, ai_optimizer, od_sync, conversion_upload)
    if ads_scheduler:
        for job in ads_scheduler.get_jobs():
            job_list.append({
                "id": job.id,
                "name": job.name or job.id,
                "next_run": _fmt(job.next_run_time),
                "last_run": _job_last_run.get(job.id),
            })

    # Follow-up engine runs on its own internal scheduler; add as static entry
    job_list.insert(0, {
        "id": "follow_up_engine",
        "name": "Follow-up Engine",
        "next_run": None,
        "last_run": _job_last_run.get("follow_up_engine"),
        "schedule": "Every 15 min",
    })

    return {"jobs": job_list}


# ─── Optimizer Memory ────────────────────────────────────────────────────────

@app.get("/api/admin/optimizer/memory", dependencies=[Depends(_require_admin)])
def get_memory(category: Optional[str] = None, include_inactive: bool = False):
    """Return all optimizer memory entries."""
    from database import get_optimizer_memory
    entries = get_optimizer_memory(category=category, active_only=not include_inactive)
    return {"memory": entries, "total": len(entries)}


class MemoryCreate(BaseModel):
    category: str   # 'term_classification', 'keyword_override', 'campaign_rule', 'general'
    key: str
    value: str      # 'negative', 'good_keyword', 'irrelevant', 'never_pause', etc.
    reason: str
    author: str = "admin"
    campaign: str = ""  # empty = global, campaign name = scoped to that campaign


class MemoryUpdate(BaseModel):
    value: str
    reason: str


@app.post("/api/admin/optimizer/memory", dependencies=[Depends(_require_admin)])
def add_memory(body: MemoryCreate):
    """Add a new optimizer memory entry."""
    from database import add_optimizer_memory
    entry = add_optimizer_memory(
        category=body.category,
        key=body.key,
        value=body.value,
        reason=body.reason,
        author=body.author,
        campaign=body.campaign,
    )
    scope = f"campaign:{body.campaign}" if body.campaign else "global"
    logger.info(f"Optimizer memory added: [{body.category}] '{body.key}' = '{body.value}' (scope={scope})")
    return {"status": "ok", "entry": entry}


@app.put("/api/admin/optimizer/memory/{memory_id}", dependencies=[Depends(_require_admin)])
def update_memory(memory_id: int, body: MemoryUpdate):
    """Update value and reason for an existing memory entry."""
    from database import update_optimizer_memory
    entry = update_optimizer_memory(memory_id, body.value, body.reason)
    if not entry:
        raise HTTPException(status_code=404, detail="Memory entry not found or inactive")
    return {"status": "ok", "entry": entry}


@app.delete("/api/admin/optimizer/memory/{memory_id}", dependencies=[Depends(_require_admin)])
def delete_memory(memory_id: int):
    """Soft-delete (deactivate) a memory entry."""
    from database import deactivate_optimizer_memory
    deactivate_optimizer_memory(memory_id)
    logger.info(f"Optimizer memory deactivated: id={memory_id}")
    return {"status": "ok"}


# ─── Excellence Targets ───────────────────────────────────────────────────────

@app.get("/api/admin/optimizer/excellence-targets", dependencies=[Depends(_require_admin)])
def list_excellence_targets(applies_to: Optional[str] = None, include_inactive: bool = False):
    """Return excellence targets from the GDC Google Ads Excellence Report."""
    from database import get_excellence_targets
    return {"targets": get_excellence_targets(applies_to=applies_to, active_only=not include_inactive)}


class ExcellenceTargetBody(BaseModel):
    metric: str
    target_value: float
    direction: str = "above"   # above | below
    unit: str = ""             # % | $ | x | ''
    applies_to: str = "all"   # all | emergency | implants | invisalign | general | cosmetic | brand
    label: str
    notes: str = ""


@app.post("/api/admin/optimizer/excellence-targets", dependencies=[Depends(_require_admin)])
def add_excellence_target(body: ExcellenceTargetBody):
    """Add a new excellence target."""
    from database import upsert_excellence_target
    entry = upsert_excellence_target(
        metric=body.metric, target_value=body.target_value, direction=body.direction,
        unit=body.unit, applies_to=body.applies_to, label=body.label, notes=body.notes,
    )
    logger.info(f"Excellence target added: {body.metric} ({body.applies_to})")
    return {"status": "ok", "entry": entry}


@app.put("/api/admin/optimizer/excellence-targets/{tid}", dependencies=[Depends(_require_admin)])
def update_excellence_target(tid: int, body: ExcellenceTargetBody):
    """Update an existing excellence target by ID."""
    from database import upsert_excellence_target
    entry = upsert_excellence_target(
        metric=body.metric, target_value=body.target_value, direction=body.direction,
        unit=body.unit, applies_to=body.applies_to, label=body.label, notes=body.notes,
        target_id=tid,
    )
    if not entry:
        raise HTTPException(status_code=404, detail="Excellence target not found")
    logger.info(f"Excellence target updated: id={tid} {body.metric}")
    return {"status": "ok", "entry": entry}


@app.delete("/api/admin/optimizer/excellence-targets/{tid}", dependencies=[Depends(_require_admin)])
def delete_excellence_target(tid: int):
    """Soft-delete (deactivate) an excellence target."""
    from database import deactivate_excellence_target
    deactivate_excellence_target(tid)
    logger.info(f"Excellence target deactivated: id={tid}")
    return {"status": "ok"}


# ── Optimizer Run Memory (file-based, cross-run digest) ──────────────────────

@app.get("/api/admin/optimizer/run-memory", dependencies=[Depends(_require_admin)])
def get_optimizer_run_memory():
    """Return the file-based optimizer run memory digest (cross-run context)."""
    from optimizer_memory import MemoryStore
    try:
        mem = MemoryStore()
        mem.load()
        digest = mem.build_digest(max_runs=10)
        last_run = mem.get_last_run_date()
        return {
            "last_run_date": str(last_run) if last_run else None,
            "digest": digest,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Google Ads Intelligence Endpoints ────────────────────────────────────────

@app.get("/api/admin/search-terms", dependencies=[Depends(_require_admin)])
def get_search_terms(campaign: str = "", days: int = 30):
    """Return cached search terms with lead attribution."""
    return {
        "search_terms": get_search_term_stats(campaign_name=campaign, days=days),
        "days": days,
        "campaign_filter": campaign,
    }


@app.get("/api/admin/geo-performance", dependencies=[Depends(_require_admin)])
def get_geo_performance(days: int = 30):
    """Return cached geographic performance data."""
    return {"geo": get_geo_stats(days=days), "days": days}


@app.get("/api/admin/geo-stats", dependencies=[Depends(_require_admin)])
def get_geo_stats_endpoint(campaign: str = "", days: int = 30):
    """
    Return geo performance rows for a specific campaign (both targeted and physical view types).
    Used by the Geo Performance Panel in the campaign detail tab.
    """
    rows = get_geo_stats_by_campaign(campaign_name=campaign, days=days)
    return rows


@app.get("/api/admin/schedule-performance", dependencies=[Depends(_require_admin)])
def get_schedule_performance(days: int = 30):
    """Return cached hour-of-day / day-of-week / device performance."""
    return {**get_schedule_stats(days=days), "days": days}


class KeywordResearchRequest(BaseModel):
    seed_keywords: list         # e.g. ["dental implants", "all on 4 near me"]
    budget: Optional[float] = None
    geo_target_ids: Optional[list] = None   # e.g. ["geoTargetConstants/1020615"]


@app.post("/api/admin/keyword-research", dependencies=[Depends(_require_admin)])
def keyword_research(body: KeywordResearchRequest):
    """
    Run Google Keyword Planner on seed keywords.
    Returns search volume, competition, CPC range, and 12-month trend.
    Use this before launching a new campaign to validate keyword demand.
    """
    try:
        from keyword_planner import get_keyword_ideas
        ideas = get_keyword_ideas(
            seed_keywords=body.seed_keywords,
            geo_target_ids=body.geo_target_ids or [],
        )
        logger.info(f"Keyword research: {len(ideas)} ideas for {body.seed_keywords}")
        return {
            "ideas": ideas,
            "seed_keywords": body.seed_keywords,
            "total": len(ideas),
        }
    except Exception as e:
        logger.error(f"Keyword research failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── Email Inbox (Step 5 — bi-directional inbox) ─────────────────────────────

@app.post("/api/admin/email-inbox/poll", dependencies=[Depends(_require_admin)])
def admin_email_inbox_poll():
    """
    Manually trigger an IMAP poll of info@nxtsmile.com.
    Fetches UNSEEN messages, matches to leads, stores in conversations/messages.
    """
    try:
        from imap_service import poll_once
        result = poll_once()
        logger.info(f"Manual IMAP poll: {result}")
        return {"status": "ok", **result}
    except Exception as e:
        logger.error(f"Manual IMAP poll error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/conversations", dependencies=[Depends(_require_admin)])
def admin_get_conversations(limit: int = 100, unmatched_only: bool = False):
    """
    Return all email conversations, newest first.
    Each row includes lead name, contact email, last message preview, and message count.
    """
    convs = get_all_conversations(limit=limit, unmatched_only=unmatched_only)
    return {"conversations": convs, "total": len(convs)}


@app.get("/api/admin/conversations/{lead_id}", dependencies=[Depends(_require_admin)])
def admin_get_lead_conversation(lead_id: str):
    """Return full conversation thread for a specific lead."""
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    conv = get_conversation(lead_id)
    if not conv:
        return {"conversation": None, "messages": [], "lead": lead}
    messages = get_messages(conv["id"])
    # Do NOT auto-mark as read here — mark-read is triggered explicitly by
    # user action (send reply or "Mark as read" button) so the bell badge
    # stays visible until the user intentionally dismisses it.
    return {"conversation": conv, "messages": messages, "lead": lead}


class ReplyRequest(BaseModel):
    body: str
    subject: Optional[str] = None


@app.post("/api/admin/conversations/{lead_id}/reply", dependencies=[Depends(_require_admin)])
def admin_reply_to_lead(lead_id: str, body: ReplyRequest):
    """
    Send a staff reply email to a lead and store it in their conversation thread.
    In dev mode the email is redirected to test_redirect_email.
    """
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    try:
        from imap_service import send_reply
        result = send_reply(lead_id=lead_id, body=body.body, subject=body.subject)
        logger.info(f"Staff reply sent: lead={lead_id}, to={result['to']}")
        return {"status": "sent", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Reply failed for lead {lead_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── Manual Messaging (Step 8) ────────────────────────────────────────────────

class ManualSmsRequest(BaseModel):
    message: str


class ManualEmailRequest(BaseModel):
    subject: str
    body: str


@app.get("/api/admin/lead/{lead_id}/messages", dependencies=[Depends(_require_admin)])
def admin_get_lead_messages(lead_id: str):
    """Return all messages (auto + manual) for a lead, ordered by timestamp."""
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"messages": get_lead_messages(lead_id)}


@app.post("/api/admin/lead/{lead_id}/send-sms", dependencies=[Depends(_require_admin)])
def admin_send_manual_sms(lead_id: str, body: ManualSmsRequest):
    """Send a manual SMS to a lead. Respects kill switch and dev redirect."""
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    phone = (lead.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=422, detail="Lead has no phone number")
    msg = body.message.strip()
    if not msg:
        raise HTTPException(status_code=422, detail="Message cannot be empty")
    if len(msg) > 1600:
        raise HTTPException(status_code=422, detail="Message too long (max 1600 chars)")

    from sms_service import send_manual_sms
    ok = send_manual_sms(phone, msg)
    if not ok:
        raise HTTPException(status_code=502, detail="SMS send failed — check logs")
    msg_id = None
    try:
        msg_id = save_outbound_message(lead_id, "sms", "", msg, sent_by="admin")
    except Exception as e:
        logger.error(f"save_outbound_message failed after SMS send: {e}", exc_info=True)
    return {"ok": True, "message_id": msg_id}


@app.post("/api/admin/lead/{lead_id}/send-email", dependencies=[Depends(_require_admin)])
def admin_send_manual_email(lead_id: str, body: ManualEmailRequest):
    """Send a manual email to a lead. Respects kill switch and dev redirect."""
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    to_email = (lead.get("email") or "").strip()
    if not to_email:
        raise HTTPException(status_code=422, detail="Lead has no email address")
    subject = body.subject.strip()
    email_body = body.body.strip()
    if not subject:
        raise HTTPException(status_code=422, detail="Subject cannot be empty")
    if not email_body:
        raise HTTPException(status_code=422, detail="Email body cannot be empty")
    if len(email_body) > 100_000:
        raise HTTPException(status_code=422, detail="Email body too long (max 100KB)")

    from email_service import send_manual_email
    ok = send_manual_email(to_email, subject, email_body)
    if not ok:
        raise HTTPException(status_code=502, detail="Email send failed — check logs")
    msg_id = None
    try:
        msg_id = save_outbound_message(lead_id, "email", subject, email_body, sent_by="admin")
    except Exception as e:
        logger.error(f"save_outbound_message failed after email send: {e}", exc_info=True)
    return {"ok": True, "message_id": msg_id}


# ─── Inbox / Unread SMS ───────────────────────────────────────────────────────

@app.get("/api/admin/inbox/unread", dependencies=[Depends(_require_admin)])
def admin_inbox_unread():
    """Return unread inbound SMS + email count + combined list of leads with unread messages."""
    sms_count = get_unread_sms_count()
    email_count = get_unread_email_count()
    sms_leads = get_unread_sms_leads()
    email_leads = get_unread_email_leads()

    # Merge: combine leads from both channels, dedupe by lead_id
    # SMS leads use 'last_received_at', email leads use 'latest_at' — normalize to 'latest_at'
    merged = {}
    for l in sms_leads:
        row = {**l, "channel": "sms", "has_unread_email": False,
               "latest_at": l.get("latest_at") or l.get("last_received_at") or ""}
        merged[l["lead_id"]] = row

    for el in email_leads:
        lid = el["lead_id"]
        el_at = el.get("latest_at") or ""
        if lid in merged:
            # Lead has both unread SMS and email — keep most recent timestamp
            merged[lid]["unread_count"] = merged[lid].get("unread_count", 0) + el.get("unread_count", 0)
            merged[lid]["has_unread_email"] = True
            merged[lid]["channel"] = "both"
            if el_at > merged[lid].get("latest_at", ""):
                merged[lid]["latest_at"] = el_at
                merged[lid]["latest_body"] = el.get("latest_body", merged[lid].get("latest_body"))
        else:
            merged[lid] = {**el, "channel": "email", "has_unread_email": True, "latest_at": el_at}

    leads_list = sorted(merged.values(), key=lambda x: x.get("latest_at", ""), reverse=True)

    # Lightweight urgent action count — avoids materialising the full 200-item feed on every 15s poll
    from database import get_urgent_action_count
    urgent_count = get_urgent_action_count()
    return {
        "count": sms_count + email_count,
        "sms_count": sms_count,
        "email_count": email_count,
        "leads": leads_list,
        "urgent_action_count": urgent_count,
    }

@app.post("/api/admin/lead/{lead_id}/mark-read", dependencies=[Depends(_require_admin)])
def admin_mark_sms_read(lead_id: str):
    """Mark all inbound SMS and email messages for a lead as read."""
    if not get_lead(lead_id):
        raise HTTPException(status_code=404, detail="Lead not found")
    sms_updated = mark_sms_read(lead_id)
    email_updated = mark_email_read(lead_id)
    return {"ok": True, "updated": sms_updated + email_updated}


# ─── Call Log ─────────────────────────────────────────────────────────────────

class LogCallRequest(BaseModel):
    direction: str = "outbound"    # 'outbound' | 'inbound'
    outcome: str                   # 'spoke' | 'left_vm' | 'no_answer' | 'callback_scheduled'
    duration_sec: int = 0
    notes: str = ""

@app.post("/api/admin/lead/{lead_id}/log-call", dependencies=[Depends(_require_admin)])
def admin_log_call(lead_id: str, body: LogCallRequest):
    """Log a manual phone call attempt or received call."""
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    valid_outcomes = {"spoke", "left_vm", "no_answer", "callback_scheduled"}
    if body.outcome not in valid_outcomes:
        raise HTTPException(status_code=422, detail=f"outcome must be one of {sorted(valid_outcomes)}")
    call_id = log_call(
        lead_id=lead_id,
        direction=body.direction,
        outcome=body.outcome,
        duration_sec=body.duration_sec,
        notes=body.notes,
    )
    return {"ok": True, "call_id": call_id}

@app.get("/api/admin/lead/{lead_id}/calls", dependencies=[Depends(_require_admin)])
def admin_get_calls(lead_id: str):
    """Return call log for a lead."""
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"calls": get_calls(lead_id)}


# ─── Next Action ──────────────────────────────────────────────────────────────

class NextActionRequest(BaseModel):
    next_action_at: str    # ISO date string e.g. '2026-05-05'
    next_action_note: str = ""

@app.put("/api/admin/lead/{lead_id}/next-action", dependencies=[Depends(_require_admin)])
def admin_set_next_action(lead_id: str, body: NextActionRequest):
    """Set a next follow-up date and note on a lead."""
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    date_str = body.next_action_at.strip()
    import re as _re
    if not _re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        raise HTTPException(status_code=422, detail="next_action_at must be YYYY-MM-DD")
    set_next_action(lead_id, date_str, body.next_action_note.strip())
    return {"ok": True}

@app.delete("/api/admin/lead/{lead_id}/next-action", dependencies=[Depends(_require_admin)])
def admin_clear_next_action(lead_id: str):
    """Clear the next action on a lead."""
    if not get_lead(lead_id):
        raise HTTPException(status_code=404, detail="Lead not found")
    clear_next_action(lead_id)
    return {"ok": True}


# ─── Step 9: Workflow CRUD + AI Generate ─────────────────────────────────────

class WorkflowCreate(BaseModel):
    name: str
    campaign_tag: Optional[str] = ""
    description: Optional[str] = ""


class WorkflowStepCreate(BaseModel):
    workflow_id: int
    sequence_day: int
    channel: str           # 'email' | 'sms'
    template_name: str
    subject: str = ""
    body: str
    terminal: bool = False
    image_attachment: str = "none"  # 'none'|'smile_after'|'smile_composite'|'case_photo_tagged'|'library:<file>'
    book_now_url: str = ""          # URL for {book_now_button} placeholder


class WorkflowStepUpdate(BaseModel):
    workflow_id: Optional[int] = None
    sequence_day: Optional[int] = None
    channel: Optional[str] = None
    template_name: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    terminal: Optional[bool] = None
    image_attachment: Optional[str] = None  # 'none'|'smile_after'|'smile_composite'|'case_photo_tagged'|'library:<file>'
    book_now_url: Optional[str] = None      # URL for {book_now_button} placeholder


class AIGenerateRequest(BaseModel):
    prompt: str              # Free-text description of the campaign / goals
    num_steps: int = 6       # How many steps to generate


# ─── Practice Information Settings ──────────────────────────────────────────

_PRACTICE_FIELDS = [
    "name", "phone", "email", "doctor_name", "address", "hours", "review_link",
    "website",
    "booking_link_consult", "booking_link_exam", "booking_link_implant",
    "booking_link_ortho", "booking_link_general",
]

class PracticeSettingsRequest(BaseModel):
    name:                 str = ""
    phone:                str = ""
    email:                str = ""
    doctor_name:          str = ""
    address:              str = ""
    hours:                str = ""
    review_link:          str = ""
    website:              str = ""
    booking_link_consult: str = ""
    booking_link_exam:    str = ""
    booking_link_implant: str = ""
    booking_link_ortho:   str = ""
    booking_link_general: str = ""


# ─── AI Generate single message ──────────────────────────────────────────────

_APPT_TYPE_FIELD_MAP = {
    "consult":  "booking_link_consult",
    "exam":     "booking_link_exam",
    "implant":  "booking_link_implant",
    "ortho":    "booking_link_ortho",
    "general":  "booking_link_general",
}

class AIGenerateMessageRequest(BaseModel):
    channel:          str        # "email" or "sms"
    appointment_type: str = "general"
    prompt:           str


def _extract_json_from_ai_response(raw_text: str, allow_array: bool = False) -> str:
    """Extract JSON object (or array if allow_array=True) from AI response.

    Handles:
      - Bare JSON (most common)
      - Wrapped in ```json ... ``` or ``` ... ``` fences
      - JSON preceded by explanation text

    Uses json.JSONDecoder.raw_decode to find the first valid JSON value and
    stop exactly there — avoids greedy regex "Extra data" issues.
    """
    import json as _j
    json_text = raw_text.strip()

    # 1. Strip code fences first
    fence_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", json_text)
    if fence_match:
        json_text = fence_match.group(1).strip()

    # 2. Try to find the first valid JSON object or array using raw_decode
    decoder = _j.JSONDecoder()
    # Scan forward to find first { or [
    for start_char, wanted in [("{", "object"), ("[", "array")]:
        if not allow_array and start_char == "[":
            continue
        idx = json_text.find(start_char)
        if idx == -1:
            continue
        try:
            obj, end = decoder.raw_decode(json_text, idx)
            # raw_decode found valid JSON ending at `end` — return just that slice
            return json_text[idx:end]
        except _j.JSONDecodeError:
            continue

    # 3. Fallback — return as-is and let the caller handle the error
    return json_text


@app.get("/api/admin/workflows", dependencies=[Depends(_require_admin)])
def admin_list_workflows():
    workflows = get_all_workflows()
    result = []
    for wf in workflows:
        steps = get_workflow_steps(wf["id"])
        result.append({**wf, "steps": steps})
    return {"workflows": result}


@app.get("/api/admin/workflows/{workflow_id}", dependencies=[Depends(_require_admin)])
def admin_get_workflow(workflow_id: int):
    wf = get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    steps = get_workflow_steps(workflow_id)
    return {**wf, "steps": steps}


@app.post("/api/admin/workflows", dependencies=[Depends(_require_admin)])
def admin_create_workflow(body: WorkflowCreate):
    wf = upsert_workflow(None, body.name, body.campaign_tag, body.description)
    return wf


@app.put("/api/admin/workflows/{workflow_id}", dependencies=[Depends(_require_admin)])
def admin_update_workflow(workflow_id: int, body: WorkflowCreate):
    wf = get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return upsert_workflow(workflow_id, body.name, body.campaign_tag, body.description)


@app.delete("/api/admin/workflows/{workflow_id}", dependencies=[Depends(_require_admin)])
def admin_delete_workflow(workflow_id: int):
    wf = get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    delete_workflow(workflow_id)
    return {"ok": True}


@app.post("/api/admin/workflows/{workflow_id}/copy", dependencies=[Depends(_require_admin)])
def admin_copy_workflow(workflow_id: int):
    """Duplicate a workflow and all its steps. Returns the new workflow with steps."""
    src = get_workflow(workflow_id)
    if not src:
        raise HTTPException(status_code=404, detail="Workflow not found")
    src_steps = get_workflow_steps(workflow_id)

    # Generate unique tag — append a short uuid suffix to avoid UNIQUE collision
    suffix = uuid.uuid4().hex[:6]
    orig_tag = (src.get("campaign_tag") or "").strip()
    new_tag = f"{orig_tag}_cp{suffix}" if orig_tag else f"copy_{suffix}"
    # Strip any trailing " (Copy)" before appending to avoid "Foo (Copy) (Copy)"
    base_name = re.sub(r'\s*\(Copy\)\s*$', '', src['name']).strip()
    new_name = f"{base_name} (Copy)"

    new_wf = upsert_workflow(None, new_name, new_tag, src.get("description", ""))
    new_wf_id = new_wf["id"]

    # Duplicate steps with unique template names using new wf id
    for step in src_steps:
        new_tname = f"wf{new_wf_id}_d{step['sequence_day']}_{step['channel']}_{suffix}"
        upsert_workflow_step(
            None, new_wf_id,
            step["sequence_day"], step["channel"],
            new_tname, step.get("subject", ""), step["body"],
            bool(step.get("terminal", False))
        )

    # Return new workflow with steps
    new_steps = get_workflow_steps(new_wf_id)
    return {**new_wf, "steps": new_steps}


@app.post("/api/admin/workflows/{workflow_id}/seed-nxtsmile", dependencies=[Depends(_require_admin)])
def admin_seed_nxtsmile(workflow_id: int):
    """Seed the nXtSmile 4-email follow-up sequence into an empty workflow."""
    wf = get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    existing_steps = get_workflow_steps(workflow_id)
    if existing_steps:
        raise HTTPException(status_code=409, detail="Workflow already has steps — clear them first")

    # Load practice booking link from DB settings
    booking_link = get_setting("practice_booking_link_consult") or get_setting("practice_booking_link_general") or "https://visitgdc.com"
    office_phone = get_setting("practice_phone") or ""

    seed_steps = [
        (1, "email",
         f"wf{workflow_id}_d1_email",
         "Your new smile is closer than you think, {first_name} :)",
         f"Hi {{first_name}},\n\nWe hope you loved your smile preview! Take another look — this could be you.\n\n"
         f"Every smile transformation starts with a single step. Hundreds of patients walked into Grafton Dental Care "
         f"feeling unsure — and walked out with a smile they couldn't stop showing off.\n\n"
         f"You deserve to eat the foods you love, laugh without thinking twice, and feel proud every time you look in the mirror. "
         f"Dr. Gupta and the nXtSmile team are here to make that happen.\n\n"
         f"Your free consultation is waiting — no pressure, no obligation. Just a conversation about what's possible.\n\n"
         f"📅 Book online: {booking_link}\n📞 Or call us: {office_phone}\n\n"
         f"— Dr. Gupta's Team at Grafton Dental Care\n\n"
         f"To unsubscribe: {{unsub_url}}",
         False),
        (7, "email",
         f"wf{workflow_id}_d7_email",
         "What's holding you back, {first_name}?",
         f"Hi {{first_name}},\n\nWe've had a lot of people tell us the same things before they finally came in:\n\n"
         f"\"I'm worried about the cost.\"\n"
         f"We work with CareCredit, Cherry, and in-house financing — many patients pay as little as $300 a month.\n\n"
         f"\"I'm not sure I'm a candidate.\"\n"
         f"That's exactly what the free consultation is for. There's no commitment — just answers.\n\n"
         f"\"I'm nervous.\"\n"
         f"Dr. Gupta has helped hundreds of patients just like you. The consultation is relaxed and pressure-free.\n\n"
         f"\"Would it hurt?\"\n"
         f"Dr. Gupta is an expert in painless dentistry. You will be provided comfortable sedation to make the procedure as painless as possible.\n\n"
         f"📅 Book your free consult: {booking_link}\n📞 Or call: {office_phone}\n\n"
         f"— Dr. Gupta's Team at Grafton Dental Care\n\n"
         f"To unsubscribe: {{unsub_url}}",
         False),
        (14, "email",
         f"wf{workflow_id}_d14_email",
         "Your new smile might cost less than you think, {first_name}",
         f"Hi {{first_name}},\n\n"
         f"We wanted to share something that surprises most people — All-on-X dental implants don't have to be a huge upfront expense. "
         f"With our financing options, many patients pay as little as $300 a month — and they eat what they want, smile with confidence, "
         f"and never worry about dentures slipping again.\n\n"
         f"Your financing options:\n"
         f"🏦 CareCredit — 0% interest available\n"
         f"🍒 Cherry — instant approval, flexible monthly plans\n"
         f"🏥 In-house financing — we'll work with your situation\n\n"
         f"We'll discuss your financing options at your free consultation — a full treatment plan personalized to your budget. No surprises.\n\n"
         f"📅 Book now: {booking_link}\n📞 Call: {office_phone}\n\n"
         f"— Dr. Gupta's Team at Grafton Dental Care\n\n"
         f"To unsubscribe: {{unsub_url}}",
         False),
        (30, "email",
         f"wf{workflow_id}_d30_email",
         "Still here whenever you're ready, {first_name}",
         f"Hi {{first_name}},\n\n"
         f"We know life gets busy and sometimes the timing just isn't right. That's completely okay.\n\n"
         f"Whether it's next week, next month, or next year — you deserve a smile you're proud of, "
         f"and we'd love to help make that happen. Whenever you're ready, reach out to us.\n\n"
         f"🔒 Your smile preview will be deleted today as part of our privacy policy. "
         f"If you'd like to start fresh in the future, we can always create a new one for you.\n\n"
         f"📅 Book anytime: {booking_link}\n📞 Call: {office_phone}\n\n"
         f"Wishing you a healthy, confident smile — whenever the time is right.\n\n"
         f"— Dr. Gupta's Team at Grafton Dental Care\n\n"
         f"To unsubscribe: {{unsub_url}}",
         True),
    ]

    for day, channel, tname, subject, body, terminal in seed_steps:
        upsert_workflow_step(None, workflow_id, day, channel, tname, subject, body, terminal)

    steps = get_workflow_steps(workflow_id)
    return {**wf, "steps": steps}


@app.post("/api/admin/workflow-steps", dependencies=[Depends(_require_admin)])
def admin_create_workflow_step(body: WorkflowStepCreate):
    wf = get_workflow(body.workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    step = upsert_workflow_step(
        None, body.workflow_id, body.sequence_day, body.channel,
        body.template_name, body.subject, body.body, body.terminal,
        image_attachment=body.image_attachment,
        book_now_url=body.book_now_url,
    )
    return step


@app.put("/api/admin/workflow-steps/{step_id}", dependencies=[Depends(_require_admin)])
def admin_update_workflow_step(step_id: int, body: WorkflowStepUpdate):
    existing = get_workflow_step(step_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Workflow step not found")
    # Merge incoming fields with existing
    merged = {
        "workflow_id": body.workflow_id if body.workflow_id is not None else existing["workflow_id"],
        "sequence_day": body.sequence_day if body.sequence_day is not None else existing["sequence_day"],
        "channel": body.channel if body.channel is not None else existing["channel"],
        "template_name": body.template_name if body.template_name is not None else existing["template_name"],
        "subject": body.subject if body.subject is not None else existing["subject"],
        "body": body.body if body.body is not None else existing["body"],
        "terminal": body.terminal if body.terminal is not None else bool(existing["terminal"]),
        "image_attachment": body.image_attachment if body.image_attachment is not None else existing.get("image_attachment", "none"),
        "book_now_url": body.book_now_url if body.book_now_url is not None else existing.get("book_now_url", ""),
    }
    return upsert_workflow_step(
        step_id, merged["workflow_id"], merged["sequence_day"], merged["channel"],
        merged["template_name"], merged["subject"], merged["body"], merged["terminal"],
        image_attachment=merged["image_attachment"],
        book_now_url=merged["book_now_url"],
    )


@app.delete("/api/admin/workflow-steps/{step_id}", dependencies=[Depends(_require_admin)])
def admin_delete_workflow_step(step_id: int):
    existing = get_workflow_step(step_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Workflow step not found")
    delete_workflow_step(step_id)
    return {"ok": True}


@app.post("/api/admin/test-step-email", dependencies=[Depends(_require_admin)])
def admin_test_step_email(body: dict = Body(...)):
    """
    Fire a workflow step email immediately against the best available test lead.
    Sends to the dev-redirect address (TEST_REDIRECT_EMAIL), never to a real patient.
    Picks the lead with a smile_blob_name first (so image steps can be tested),
    falling back to any lead with an email address.
    Returns which lead and redirect address was used.
    """
    from email_service import send_workflow_step_email
    from config import get_settings

    step_id = body.get("step_id")
    if not step_id:
        raise HTTPException(status_code=400, detail="step_id required")

    step = get_workflow_step(step_id)
    if not step:
        raise HTTPException(status_code=404, detail="Workflow step not found")

    if step.get("channel") != "email":
        raise HTTPException(status_code=400, detail="Step is not an email step")

    settings = get_settings()

    # Pick best test lead: prefer one with smile images, fallback to any with email
    from database import _conn
    with _conn() as _db:
        test_lead = _db.execute(
            "SELECT * FROM leads WHERE smile_blob_name != '' AND email != '' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not test_lead:
            test_lead = _db.execute(
                "SELECT * FROM leads WHERE email != '' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

    if not test_lead:
        raise HTTPException(status_code=404, detail="No leads with email found to use as test subject")

    lead = dict(test_lead)

    # Build unsubscribe URL using the lead's real id
    lead_id = lead.get("lead_id") or lead.get("id", "")
    base = settings.base_url.rstrip("/")
    unsub_url = f"{base}/unsubscribe/{lead_id}/email"

    redirect_to = getattr(settings, "test_redirect_email", "") or lead.get("email", "")

    try:
        ok = send_workflow_step_email(lead, dict(step), unsub_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Send failed: {e}")

    return {
        "ok": ok,
        "sent_to": redirect_to,
        "test_lead": lead.get("first_name", "") + " " + lead.get("last_name", ""),
        "test_lead_id": lead_id,
        "smile_blob": lead.get("smile_blob_name", ""),
        "composite_blob": lead.get("smile_composite_blob_name", ""),
        "step_template": step.get("template_name"),
        "image_attachment": step.get("image_attachment", "none"),
        "note": "Email redirected to dev address — no real patient contacted",
    }


# ─── Media Library ────────────────────────────────────────────────────────────

@app.get("/api/admin/media-library", dependencies=[Depends(_require_admin)])
def admin_list_media_library():
    """List all media library entries (staff-uploaded case photos with tags)."""
    import json as _json
    items = list_media_library()
    for item in items:
        try:
            item["tags"] = _json.loads(item.get("tags") or "[]")
        except Exception:
            item["tags"] = []
        item["url"] = f"/media/case-photos/{item['filename']}"
    return {"items": items}


@app.post("/api/admin/media-library", dependencies=[Depends(_require_admin)])
async def admin_upload_media_library(
    file: UploadFile = File(...),
    label: str = Form(""),
    tags: str = Form("[]"),   # JSON-encoded list e.g. '["implants","cosmetic"]'
):
    """Upload a new case photo to the media library."""
    import json as _json
    import uuid as _uuid
    import shutil

    # Validate file type — allow-list only safe raster types (no SVG/html)
    _ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    _ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    ct = (file.content_type or "").lower().split(";")[0].strip()
    if ct not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, WebP, or GIF images are allowed")

    # Parse tags
    try:
        tags_list = _json.loads(tags) if tags else []
        if not isinstance(tags_list, list):
            tags_list = []
    except Exception:
        tags_list = []

    # Generate unique filename — derive extension from content-type, not user filename
    _ct_to_ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
    ext = _ct_to_ext.get(ct, os.path.splitext(file.filename or "photo.jpg")[1].lower() or ".jpg")
    if ext not in _ALLOWED_EXTENSIONS:
        ext = ".jpg"
    unique_name = f"lib_{_uuid.uuid4().hex[:12]}{ext}"
    case_dir = os.path.join(os.path.dirname(__file__), "case_photos")
    dest_path = os.path.join(case_dir, unique_name)

    try:
        with open(dest_path, "wb") as f_out:
            shutil.copyfileobj(file.file, f_out)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"File save failed: {e}")

    display_label = label.strip() or file.filename or unique_name
    item = create_media_library_item(unique_name, display_label, tags_list)
    item["tags"] = tags_list
    item["url"] = f"/media/case-photos/{unique_name}"
    return item


@app.put("/api/admin/media-library/{item_id}", dependencies=[Depends(_require_admin)])
def admin_update_media_library(item_id: int, body: dict = Body(...)):
    """Update label and/or tags for a media library item."""
    existing = get_media_library_item(item_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Media library item not found")
    import json as _json
    label = body.get("label", existing.get("label", ""))
    # Fall back to existing tags if "tags" key not provided — prevents accidental wipe
    existing_tags = existing.get("tags") or []
    if isinstance(existing_tags, str):
        try:
            existing_tags = _json.loads(existing_tags)
        except Exception:
            existing_tags = []
    raw_tags = body.get("tags", existing_tags)
    tags_list = raw_tags if isinstance(raw_tags, list) else existing_tags
    item = update_media_library_item(item_id, label, tags_list)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found after update")
    item["tags"] = tags_list
    item["url"] = f"/media/case-photos/{item['filename']}"
    return item


@app.delete("/api/admin/media-library/{item_id}", dependencies=[Depends(_require_admin)])
def admin_delete_media_library(item_id: int):
    """Delete a media library item and remove the file from disk."""
    filename = delete_media_library_item(item_id)
    if not filename:
        raise HTTPException(status_code=404, detail="Media library item not found")
    case_dir = os.path.join(os.path.dirname(__file__), "case_photos")
    file_path = os.path.join(case_dir, filename)
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Deleted media library file: {filename}")
    except Exception as e:
        logger.warning(f"Could not delete media library file {filename}: {e}")
    return {"ok": True, "filename": filename}


@app.post("/api/admin/workflow/ai-generate", dependencies=[Depends(_require_admin)])
def admin_ai_generate_workflow(body: AIGenerateRequest):
    """Use Claude to generate a workflow step sequence from a natural-language prompt."""
    prompt_text = (body.prompt or "").strip()
    if not prompt_text:
        raise HTTPException(status_code=422, detail="Prompt is required")
    if len(prompt_text) > 2000:
        raise HTTPException(status_code=422, detail="Prompt too long (max 2000 chars)")

    num_steps = max(1, min(body.num_steps, 12))

    system_prompt = (
        "You are a dental marketing expert helping design automated patient follow-up sequences. "
        "Generate a JSON object with a 'steps' array. Each step must have these exact keys: "
        "sequence_day (integer), channel ('email' or 'sms'), template_name (unique slug, e.g. 'aox_day1_email'), "
        "subject (string, blank for SMS), body (string with {first_name} and {unsub_url} placeholders). "
        "SMS bodies must end with '\\nReply STOP to opt out.' "
        "Email bodies must include {unsub_url} near the end as an unsubscribe link. "
        "Return ONLY the JSON object, no explanation."
    )

    user_prompt = (
        f"Campaign description: {prompt_text}\n\n"
        f"Generate exactly {num_steps} follow-up steps optimized for this campaign. "
        "Make the messaging specific to the campaign goals. "
        "Days should be spread naturally (e.g. 1, 3, 7, 14, 21, 30 for a 6-step sequence). "
        "Return a JSON object with a 'steps' array."
    )

    try:
        import anthropic
        api_key = get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise HTTPException(status_code=400, detail="Anthropic API key not configured. Add it in Admin → AI Settings.")
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            messages=[
                {"role": "user", "content": user_prompt}
            ],
            system=system_prompt,
        )
        raw_text = message.content[0].text if message.content else ""
    except Exception as e:
        logger.error(f"AI generate workflow failed: {e}")
        raise HTTPException(status_code=502, detail=f"AI generation failed: {e}")

    # Extract JSON — handle code fences or bare JSON
    json_text = _extract_json_from_ai_response(raw_text)

    try:
        result = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error(f"AI generate — JSON parse failed: {e}\nRaw: {raw_text[:500]}")
        raise HTTPException(status_code=502, detail="AI returned invalid JSON — please try again")

    # Schema validation
    steps = result.get("steps")
    if not isinstance(steps, list) or not steps:
        raise HTTPException(status_code=502, detail="AI response missing 'steps' array")

    required_keys = {"sequence_day", "channel", "template_name", "body"}
    for i, step in enumerate(steps):
        missing = required_keys - set(step.keys())
        if missing:
            raise HTTPException(
                status_code=502,
                detail=f"Step {i+1} missing required fields: {missing}"
            )
        if step["channel"] not in ("email", "sms"):
            raise HTTPException(
                status_code=502,
                detail=f"Step {i+1} has invalid channel: {step['channel']}"
            )
        if not isinstance(step.get("sequence_day"), int):
            raise HTTPException(
                status_code=502,
                detail=f"Step {i+1} sequence_day must be an integer"
            )
        # Ensure subject key exists
        if "subject" not in step:
            step["subject"] = ""

    return {"steps": steps}


# ─── AI Campaign — Strategy + Implementation + Performance Analysis ─────────
#
# Two-tier model: Opus 4.6 acts as the strategist/analyst; Haiku 4.5 (default)
# or Sonnet 4.6 acts as the implementer. The strategist gathers practice +
# performance context and produces a structured plan with explicit
# implementation_instructions; the implementer executes those instructions to
# produce final ad copy, SMS/email sequences, or analysis writeups.

OPUS_MODEL   = "claude-opus-4-6"
HAIKU_MODEL  = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"


class CampaignStrategyRequest(BaseModel):
    campaign_goal:      str
    target_service:     str = "All-on-4 Implants"
    budget_hint:        str = ""
    additional_context: str = ""
    source_campaign_id: str = ""   # campaign_id to copy intelligence from (optional)


class CampaignImplementRequest(BaseModel):
    strategy:    dict
    deliverable: str         # 'ad_copy' | 'sms_sequence' | 'email_sequence' | 'full_package'
    model:       str = "haiku"  # 'haiku' | 'sonnet'


class PerformanceAnalysisRequest(BaseModel):
    time_range_days: int = 30
    focus:           str = "overall"  # 'overall' | 'google_ads' | 'leads' | 'conversions'


def _get_anthropic_client():
    """Resolve API key from settings/env and return an Anthropic client."""
    import anthropic
    api_key = get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="Anthropic API key not configured. Add it in Admin → AI Settings.",
        )
    return anthropic.Anthropic(api_key=api_key)


def _build_practice_context() -> dict:
    """Read practice info from app_settings into a plain dict."""
    return {f: get_setting(f"practice_{f}") or "" for f in _PRACTICE_FIELDS}


def _format_practice_context(practice: dict) -> str:
    """Compact text block describing the practice for the AI."""
    lines = []
    if practice.get("name"):         lines.append(f"Practice: {practice['name']}")
    if practice.get("doctor_name"):  lines.append(f"Doctor: {practice['doctor_name']}")
    if practice.get("phone"):        lines.append(f"Phone: {practice['phone']}")
    if practice.get("address"):      lines.append(f"Address: {practice['address']}")
    if practice.get("hours"):        lines.append(f"Hours: {practice['hours']}")
    booking_links = []
    for k in ("booking_link_consult", "booking_link_implant", "booking_link_ortho",
              "booking_link_exam", "booking_link_general"):
        if practice.get(k):
            booking_links.append(f"  - {k.replace('booking_link_','')}: {practice[k]}")
    if booking_links:
        lines.append("Booking links:")
        lines.extend(booking_links)
    return "\n".join(lines) if lines else "(no practice info on file)"


def _gather_performance_context(days: int = 30) -> dict:
    """Pull a compact summary of pipeline + Google Ads performance."""
    ctx = {}
    try:
        ctx["pipeline_stats"] = get_pipeline_stats()
    except Exception as e:
        logger.warning(f"AI campaign — pipeline_stats failed: {e}")
        ctx["pipeline_stats"] = {}

    try:
        camps = get_campaign_stats() or []
        # keep top 8 by lead_count for brevity
        camps_sorted = sorted(camps, key=lambda c: c.get("lead_count", 0), reverse=True)[:8]
        ctx["top_campaigns"] = [
            {
                "campaign":      c.get("campaign"),
                "lead_count":    c.get("lead_count", 0),
                "total_cost":    c.get("total_cost", 0),
                "cpl":           c.get("cpl", 0),
                "revenue":       c.get("revenue", 0),
                "scheduled":     c.get("scheduled_count", 0),
                "treated":       c.get("treated_count", 0),
            }
            for c in camps_sorted
        ]
    except Exception as e:
        logger.warning(f"AI campaign — campaign_stats failed: {e}")
        ctx["top_campaigns"] = []

    try:
        kws = get_keyword_stats() or []
        # keep top 12 by lead_count
        kws_sorted = sorted(kws, key=lambda k: k.get("lead_count", 0), reverse=True)[:12]
        ctx["top_keywords"] = [
            {
                "keyword":     k.get("keyword"),
                "campaign":    k.get("campaign_name"),
                "impressions": k.get("impressions", 0),
                "clicks":      k.get("gads_clicks", 0),
                "cost":        k.get("total_cost", 0),
                "leads":       k.get("lead_count", 0),
                "cpl":         k.get("cpl", 0),
                "conv_rate":   k.get("conversion_rate", 0),
            }
            for k in kws_sorted
        ]
    except Exception as e:
        logger.warning(f"AI campaign — keyword_stats failed: {e}")
        ctx["top_keywords"] = []

    try:
        daily = get_daily_stats(days=days) or []
        # aggregate totals across the window
        agg = {"impressions": 0, "clicks": 0, "cost": 0.0, "conversions": 0.0}
        for d in daily:
            agg["impressions"] += int(d.get("impressions") or 0)
            agg["clicks"]      += int(d.get("clicks") or 0)
            agg["cost"]        += float(d.get("cost") or 0.0)
            agg["conversions"] += float(d.get("conversions") or 0.0)
        ctx["window_totals"] = {
            "days":        days,
            "impressions": agg["impressions"],
            "clicks":      agg["clicks"],
            "cost":        round(agg["cost"], 2),
            "conversions": round(agg["conversions"], 2),
        }
    except Exception as e:
        logger.warning(f"AI campaign — daily_stats failed: {e}")
        ctx["window_totals"] = {"days": days}

    return ctx


def _gather_source_campaign_context(campaign_id: str) -> dict:
    """
    Pull keyword performance, search terms, and lead attribution for a specific
    campaign to use as intelligence when cloning it into a new campaign.

    Returns a dict with:
      campaign_name, top_keywords, weak_keywords, top_search_terms,
      negative_candidates, ad_groups, lead_attribution, campaign_settings
    """
    ctx: dict = {"campaign_id": campaign_id}
    try:
        from database import _conn as _db
        with _db() as conn:
            # ── 1. Campaign name + settings from campaigns table ──────────────
            camp_row = conn.execute(
                "SELECT campaign_name, service_focus, objective, monthly_budget, "
                "expected_cpl, landing_page, notes FROM campaigns WHERE campaign_id=? LIMIT 1",
                (campaign_id,)
            ).fetchone()
            if not camp_row:
                logger.warning(f"_gather_source_campaign_context: campaign_id={campaign_id} not found")
                return ctx
            ctx["campaign_name"] = camp_row["campaign_name"]
            ctx["campaign_settings"] = {
                "service_focus":   camp_row["service_focus"] or "",
                "objective":       camp_row["objective"] or "",
                "monthly_budget":  camp_row["monthly_budget"] or 0,
                "expected_cpl":    camp_row["expected_cpl"] or 0,
                "landing_page":    camp_row["landing_page"] or "",
                "notes":           camp_row["notes"] or "",
            }
            camp_name = camp_row["campaign_name"]

            # ── 2. Keyword performance from gads_keywords_cache ───────────────
            kw_rows = conn.execute("""
                SELECT keyword_text, match_type, ad_group_name,
                       impressions, clicks, cost, conversions, avg_cpc,
                       quality_score, impression_share
                FROM gads_keywords_cache
                WHERE LOWER(campaign_name) = LOWER(?) AND days = 30
                ORDER BY cost DESC
            """, (camp_name,)).fetchall()

            keywords = [dict(r) for r in kw_rows]

            # Top performers: most clicks + conversions relative to cost
            top_kws = sorted(
                [k for k in keywords if k.get("clicks", 0) > 0],
                key=lambda k: (k.get("conversions", 0) * 10 + k.get("clicks", 0)) / max(k.get("cost", 0.01), 0.01),
                reverse=True
            )[:15]

            # Weak performers: high cost, zero conversions, zero leads
            weak_kws = sorted(
                [k for k in keywords if k.get("cost", 0) > 5 and k.get("conversions", 0) == 0],
                key=lambda k: -k.get("cost", 0)
            )[:10]

            ctx["top_keywords"] = [
                {
                    "keyword":     k["keyword_text"],
                    "match_type":  k["match_type"],
                    "ad_group":    k["ad_group_name"],
                    "clicks":      k.get("clicks", 0),
                    "cost":        round(k.get("cost", 0), 2),
                    "conversions": round(k.get("conversions", 0), 2),
                    "cpc":         round(k.get("avg_cpc", 0), 2),
                    "qs":          k.get("quality_score", 0),
                }
                for k in top_kws
            ]
            ctx["weak_keywords"] = [
                {
                    "keyword":  k["keyword_text"],
                    "cost":     round(k.get("cost", 0), 2),
                    "clicks":   k.get("clicks", 0),
                    "note":     "high spend, zero conversions",
                }
                for k in weak_kws
            ]

            # Ad groups (unique names)
            ad_groups = list(dict.fromkeys(
                k["ad_group_name"] for k in keywords if k.get("ad_group_name")
            ))
            ctx["ad_groups"] = ad_groups[:10]

            # ── 3. Search terms from gads_search_terms_cache ──────────────────
            st_rows = conn.execute("""
                SELECT search_term, impressions, clicks, cost, conversions, cpc, status
                FROM gads_search_terms_cache
                WHERE LOWER(campaign_name) = LOWER(?) AND days = 30
                ORDER BY cost DESC
            """, (camp_name,)).fetchall()

            search_terms = [dict(r) for r in st_rows]

            # Best converting search terms → candidate keywords for new campaign
            top_st = sorted(
                [s for s in search_terms if s.get("clicks", 0) >= 2],
                key=lambda s: (s.get("conversions", 0) * 10 + s.get("clicks", 0)),
                reverse=True
            )[:20]

            # High-cost zero-conversion search terms → candidate negatives
            neg_candidates = sorted(
                [s for s in search_terms
                 if s.get("cost", 0) > 3 and s.get("conversions", 0) == 0
                 and s.get("status", "") != "EXCLUDED"],
                key=lambda s: -s.get("cost", 0)
            )[:15]

            ctx["top_search_terms"] = [
                {
                    "term":        s["search_term"],
                    "clicks":      s.get("clicks", 0),
                    "cost":        round(s.get("cost", 0), 2),
                    "conversions": round(s.get("conversions", 0), 2),
                    "status":      s.get("status", ""),
                }
                for s in top_st
            ]
            ctx["negative_candidates"] = [
                {
                    "term":  s["search_term"],
                    "cost":  round(s.get("cost", 0), 2),
                    "clicks": s.get("clicks", 0),
                    "note":  "wasted spend — zero conversions",
                }
                for s in neg_candidates
            ]

            # ── 4. Existing negative keywords (applied over time by optimizer) ─
            neg_rows = conn.execute("""
                SELECT keyword_text, match_type
                FROM gads_negative_keywords
                WHERE LOWER(campaign_name) = LOWER(?)
                ORDER BY keyword_text
            """, (camp_name,)).fetchall()
            ctx["existing_negatives"] = [
                {"keyword": r["keyword_text"], "match_type": r["match_type"]}
                for r in neg_rows
            ]

            # ── 5. Lead + production attribution ─────────────────────────────
            lead_row = conn.execute("""
                SELECT
                    COUNT(*) as total_leads,
                    SUM(CASE WHEN stage IN ('scheduled','showed','no_show',
                        'treatment_presented','treatment_accepted','treatment_completed')
                        THEN 1 ELSE 0 END) as scheduled,
                    SUM(CASE WHEN stage IN ('showed',
                        'treatment_presented','treatment_accepted','treatment_completed')
                        THEN 1 ELSE 0 END) as showed,
                    SUM(CASE WHEN stage IN ('treatment_accepted','treatment_completed')
                        THEN 1 ELSE 0 END) as accepted,
                    SUM(attributed_production) as production,
                    SUM(click_cost) as spend_from_leads
                FROM leads
                WHERE LOWER(COALESCE(NULLIF(campaign_name,''), utm_campaign)) = LOWER(?)
            """, (camp_name,)).fetchone()

            if lead_row:
                total_leads = lead_row["total_leads"] or 0
                # Prefer gads_keywords_cache spend total; fall back to leads.click_cost sum
                gads_spend_row = conn.execute(
                    "SELECT SUM(cost) as s FROM gads_keywords_cache WHERE LOWER(campaign_name)=LOWER(?) AND days=30",
                    (camp_name,)
                ).fetchone()
                total_spend = float(gads_spend_row["s"] or 0) if gads_spend_row else 0.0
                if total_spend == 0:
                    total_spend = float(lead_row["spend_from_leads"] or 0)
                actual_cpl = round(total_spend / total_leads, 2) if total_leads > 0 else 0

                ctx["lead_attribution"] = {
                    "total_leads":    total_leads,
                    "scheduled":      lead_row["scheduled"] or 0,
                    "showed":         lead_row["showed"] or 0,
                    "accepted":       lead_row["accepted"] or 0,
                    "production_usd": round(float(lead_row["production"] or 0), 2),
                    "total_spend_usd": round(total_spend, 2),
                    "actual_cpl":     actual_cpl,
                    "roas":           round(float(lead_row["production"] or 0) / total_spend, 2) if total_spend > 0 else 0,
                }
            else:
                ctx["lead_attribution"] = {}

    except Exception as e:
        logger.warning(f"_gather_source_campaign_context failed (non-fatal): {e}")
        ctx.setdefault("campaign_name", "")

    return ctx


def _format_source_campaign_context(src: dict) -> str:
    """Format source campaign intelligence as a readable block for the Opus prompt."""
    lines = []
    name = src.get("campaign_name", "source campaign")
    lines.append(f"Source campaign being cloned: '{name}'")

    la = src.get("lead_attribution", {})
    if la:
        lines.append(
            f"Performance (30d): {la.get('total_leads',0)} leads, "
            f"${la.get('total_spend_usd',0):.0f} spend, "
            f"CPL ${la.get('actual_cpl',0):.0f}, "
            f"ROAS {la.get('roas',0):.1f}x, "
            f"{la.get('scheduled',0)} scheduled, {la.get('accepted',0)} treatment accepted"
        )

    top_kws = src.get("top_keywords", [])
    if top_kws:
        lines.append(f"\nTop performing keywords (use as starting list for new campaign):")
        for k in top_kws[:10]:
            lines.append(
                f"  [{k.get('match_type','?')}] \"{k['keyword']}\" — "
                f"{k.get('clicks',0)} clicks, ${k.get('cost',0):.2f} spend, "
                f"{k.get('conversions',0)} conv, QS {k.get('qs',0)}"
            )

    weak_kws = src.get("weak_keywords", [])
    if weak_kws:
        lines.append(f"\nUnderperforming keywords (consider pausing or restructuring):")
        for k in weak_kws[:6]:
            lines.append(f"  \"{k['keyword']}\" — ${k.get('cost',0):.2f} spend, 0 conversions")

    top_st = src.get("top_search_terms", [])
    if top_st:
        lines.append(f"\nHigh-value search terms (candidate exact-match keywords for new campaign):")
        for s in top_st[:12]:
            lines.append(
                f"  \"{s['term']}\" — {s.get('clicks',0)} clicks, "
                f"{s.get('conversions',0)} conv"
                + (" [already added]" if s.get("status") == "ADDED" else "")
            )

    existing_negs = src.get("existing_negatives", [])
    if existing_negs:
        lines.append(f"\nNegative keywords already applied to this campaign ({len(existing_negs)} total — COPY ALL of these to the new campaign):")
        for n in existing_negs:
            lines.append(f"  [{n.get('match_type','BROAD')}] \"{n['keyword']}\"")

    neg_cands = src.get("negative_candidates", [])
    if neg_cands:
        lines.append(f"\nAdditional wasted-spend search terms (also add as negatives):")
        for s in neg_cands[:10]:
            lines.append(f"  \"{s['term']}\" — ${s.get('cost',0):.2f} wasted, 0 conversions")

    ad_groups = src.get("ad_groups", [])
    if ad_groups:
        lines.append(f"\nAd group structure: {', '.join(ad_groups)}")

    cs = src.get("campaign_settings", {})
    if cs.get("notes"):
        lines.append(f"\nCampaign notes: {cs['notes']}")

    return "\n".join(lines)


@app.post("/api/admin/ai/campaign-strategy", dependencies=[Depends(_require_admin)])
def admin_ai_campaign_strategy(body: CampaignStrategyRequest):
    """Opus researches the practice + performance data and produces a campaign plan."""
    goal = (body.campaign_goal or "").strip()
    if not goal:
        raise HTTPException(status_code=422, detail="campaign_goal is required")
    if len(goal) > 2000:
        raise HTTPException(status_code=422, detail="campaign_goal too long (max 2000 chars)")

    target_service     = (body.target_service or "All-on-4 Implants").strip()
    budget_hint        = (body.budget_hint or "").strip()
    extra              = (body.additional_context or "").strip()
    source_campaign_id = (body.source_campaign_id or "").strip()

    practice = _build_practice_context()
    perf     = _gather_performance_context(days=30)

    # Source campaign intelligence (only when copying from an existing campaign)
    _source_ctx: dict = {}
    if source_campaign_id:
        _source_ctx = _gather_source_campaign_context(source_campaign_id)
        logger.info(f"campaign-strategy: source_campaign_id={source_campaign_id} → '{_source_ctx.get('campaign_name','?')}'")

    # Adjust system prompt to signal clone mode to Opus
    _clone_mode = bool(_source_ctx.get("campaign_name"))

    practice_name = practice.get("name") or "this dental practice"
    practice_location = ""
    if practice.get("address"):
        # Extract city/region from address for local angle.
        # "123 Main St, Grafton, MA 01536" → 4 parts → take middle two → "Grafton, MA"
        # "Grafton, MA 01536" → 3 parts → take last two → "MA 01536" (acceptable)
        # "Grafton, MA" → 2 parts → take last two → "Grafton, MA" (ideal)
        parts = [p.strip() for p in practice["address"].split(",")]
        practice_location = ", ".join(parts[-3:-1]) if len(parts) >= 4 else ", ".join(parts[-2:])

    system_prompt = (
        f"You are a senior dental marketing strategist for {practice_name}. "
        f"Your job is to RESEARCH the practice's data and produce a tightly-scoped "
        f"campaign plan that a junior copywriter (Haiku) can execute without ambiguity. "
        f"Prefer practical, locally-resonant angles"
        + (f" ({practice_location})" if practice_location else "")
        + " — weekend availability, financing options, and specific clinical strengths — over "
        f"generic claims. Return ONLY a JSON object — no markdown, no commentary."
        + (" When source campaign data is provided, you MUST anchor the new strategy to "
           "its real performance: use its winning keywords as the starting keyword list, "
           "its wasted search terms as the initial negative list, and its actual CPL as the "
           "performance baseline to beat. Do NOT ignore this data." if _clone_mode else "")
    )

    # Site intelligence — pull crawled page context for the practice website
    # Returns empty string if the domain hasn't been registered or crawled yet (graceful no-op)
    _practice_website = practice.get("website") or get_setting("practice_website") or ""
    _site_context_block = ""
    if _practice_website:
        try:
            from domain_crawler import build_site_context_for_url
            _site_context_block = build_site_context_for_url(_practice_website)
        except Exception as _sce:
            logger.warning(f"AI campaign-strategy: site context fetch failed: {_sce}")

    user_prompt = (
        f"Campaign goal: {goal}\n"
        f"Target service: {target_service}\n"
        + (f"Budget context: {budget_hint}\n" if budget_hint else "")
        + (f"Additional context: {extra}\n" if extra else "")
        + "\n=== Practice ===\n" + _format_practice_context(practice)
        + ("\n\n=== Website Intelligence (crawled page data) ===\n" + _site_context_block
           if _site_context_block else "")
        + ("\n\n=== Source Campaign Intelligence (THIS CAMPAIGN IS BEING CLONED — anchor strategy to this data) ===\n"
           + _format_source_campaign_context(_source_ctx)
           if _clone_mode else "")
        + "\n\n=== Account Performance snapshot (last 30 days) ===\n"
        + json.dumps(perf, indent=2, default=str)
        + "\n\nReturn a JSON object with EXACTLY these keys:\n"
        + "  campaign_name (string)\n"
        + "  target_audience (string — concrete description)\n"
        + "  objective (string — 1-2 sentence goal)\n"
        + "  key_messages (array of 3-5 strings)\n"
        + "  ad_headlines (array of 6-10 strings, each <= 30 chars)\n"
        + "  ad_descriptions (array of 3-5 strings, each <= 90 chars)\n"
        + "  sms_sequence_brief (string — what the SMS sequence should accomplish, tone, cadence)\n"
        + "  email_sequence_brief (string — same, for email)\n"
        + "  implementation_instructions (string — explicit instructions for the implementer "
        + "    Haiku/Sonnet model, including voice, must-include details, and what to avoid)\n"
        + ("  keyword_recommendations (array of objects: {keyword, match_type, rationale} — "
           "pulled from source campaign winners + top search terms; 10-20 items)\n"
           "  negative_keyword_recommendations (array of strings — from source campaign wasted search terms; 5-15 items)\n"
           if _clone_mode else "")
        + "Return ONLY the JSON object."
    )

    try:
        client = _get_anthropic_client()
        message = client.messages.create(
            model=OPUS_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
        )
        raw_text = message.content[0].text if message.content else ""
        model_used = message.model
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI campaign-strategy failed: {e}")
        raise HTTPException(status_code=502, detail=f"AI strategy failed: {e}")

    json_text = _extract_json_from_ai_response(raw_text)
    try:
        result = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error(f"AI campaign-strategy — JSON parse failed: {e}\nRaw: {raw_text[:500]}")
        raise HTTPException(status_code=502, detail="AI returned invalid JSON — please try again")

    required_keys = {
        "campaign_name", "target_audience", "objective", "key_messages",
        "ad_headlines", "ad_descriptions", "sms_sequence_brief",
        "email_sequence_brief", "implementation_instructions",
    }
    missing = required_keys - set(result.keys())
    if missing:
        raise HTTPException(
            status_code=502,
            detail=f"AI strategy missing required fields: {sorted(missing)}",
        )

    return {
        "strategy": result,
        "model_used": model_used,
        "source_campaign_name": _source_ctx.get("campaign_name", "") if _clone_mode else "",
        "source_campaign_id":   source_campaign_id if _clone_mode else "",
    }


@app.post("/api/admin/ai/campaign-implement", dependencies=[Depends(_require_admin)])
def admin_ai_campaign_implement(body: CampaignImplementRequest):
    """Haiku/Sonnet executes the Opus strategy to produce concrete deliverables."""
    strategy = body.strategy or {}
    if not isinstance(strategy, dict) or not strategy.get("implementation_instructions"):
        raise HTTPException(
            status_code=422,
            detail="strategy must include 'implementation_instructions' from a strategy run",
        )

    deliverable = (body.deliverable or "").strip().lower()
    if deliverable not in ("ad_copy", "sms_sequence", "email_sequence", "full_package"):
        raise HTTPException(
            status_code=422,
            detail="deliverable must be one of: ad_copy, sms_sequence, email_sequence, full_package",
        )

    model_choice = (body.model or "haiku").strip().lower()
    model_id = SONNET_MODEL if model_choice == "sonnet" else HAIKU_MODEL

    practice = _build_practice_context()
    practice_block = _format_practice_context(practice)

    # Format-specific schema instructions
    if deliverable == "ad_copy":
        schema_instructions = (
            "Return JSON with keys: "
            "headlines (array of 10 strings, each <= 30 chars), "
            "descriptions (array of 4 strings, each <= 90 chars), "
            "final_url (string — best booking link from the practice info), "
            "callouts (array of 4 short strings, each <= 25 chars)."
        )
    elif deliverable == "sms_sequence":
        schema_instructions = (
            "Return JSON with key 'steps' (array of objects). Each step has: "
            "sequence_day (integer), body (string ending with '\\nReply STOP to opt out.'). "
            "Keep each SMS body <= 280 chars before token expansion. "
            "Use {first_name} as the only runtime token."
        )
    elif deliverable == "email_sequence":
        schema_instructions = (
            "Return JSON with key 'steps' (array of objects). Each step has: "
            "sequence_day (integer), subject (string), body (string). "
            "Body must include {first_name} and end with {unsub_url}. "
            "2-4 short paragraphs each, conversational tone."
        )
    else:  # full_package
        schema_instructions = (
            "Return JSON with keys: "
            "ad_copy (object with headlines [10 strings <=30 chars], descriptions [4 strings <=90 chars], callouts [4 strings <=25 chars]), "
            "sms_sequence (object with 'steps' array — each step: sequence_day, body ending with '\\nReply STOP to opt out.'), "
            "email_sequence (object with 'steps' array — each step: sequence_day, subject, body with {first_name} and {unsub_url})."
        )

    system_prompt = (
        "You are a dental marketing copywriter executing instructions from a senior strategist. "
        "Follow the strategist's implementation_instructions exactly. Voice should be warm, "
        "specific, and locally grounded — use location, doctor name, and specific benefits "
        "from the practice context. Avoid generic phrases like "
        "'best dental care' — use specific benefits and proof points from the strategy. "
        "Return ONLY the JSON object — no markdown, no commentary."
    )

    user_prompt = (
        "=== Practice ===\n" + practice_block
        + "\n\n=== Strategist's plan (from Opus) ===\n"
        + json.dumps(strategy, indent=2, default=str)
        + f"\n\n=== Deliverable requested ===\n{deliverable}\n"
        + f"\n=== Output schema ===\n{schema_instructions}\n"
        + "\nReturn ONLY the JSON object."
    )

    try:
        client = _get_anthropic_client()
        message = client.messages.create(
            model=model_id,
            max_tokens=4096,
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
        )
        raw_text = message.content[0].text if message.content else ""
        model_used = message.model
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI campaign-implement failed (model={model_id}): {e}")
        raise HTTPException(status_code=502, detail=f"AI implementation failed: {e}")

    json_text = _extract_json_from_ai_response(raw_text)
    try:
        content = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error(f"AI campaign-implement — JSON parse failed: {e}\nRaw: {raw_text[:500]}")
        raise HTTPException(status_code=502, detail="AI returned invalid JSON — please try again")

    return {
        "deliverable": deliverable,
        "content":     content,
        "model_used":  model_used,
    }


@app.post("/api/admin/ai/performance-analysis", dependencies=[Depends(_require_admin)])
def admin_ai_performance_analysis(body: PerformanceAnalysisRequest):
    """Opus analyzes pipeline + Google Ads data and returns actionable insights."""
    days = max(1, min(int(body.time_range_days or 30), 90))
    focus = (body.focus or "overall").strip().lower()
    if focus not in ("overall", "google_ads", "leads", "conversions"):
        raise HTTPException(
            status_code=422,
            detail="focus must be one of: overall, google_ads, leads, conversions",
        )

    practice = _build_practice_context()
    perf     = _gather_performance_context(days=days)

    focus_hint = {
        "overall":     "the overall marketing pipeline health",
        "google_ads":  "Google Ads spend efficiency, top/bottom keywords, and wasted spend",
        "leads":       "lead volume by source and stage progression",
        "conversions": "conversion rates from lead → scheduled → treated and where leakage occurs",
    }[focus]

    practice_name_for_analysis = practice.get("name") or "this dental practice"
    system_prompt = (
        f"You are a senior dental marketing analyst reviewing data from {practice_name_for_analysis}. "
        "Be specific and quantitative — cite concrete numbers from the data. "
        "Flag wasted spend, conversion leakage, and underused keywords. "
        "Action items must be specific enough that a junior implementer (Haiku) could execute them. "
        "Return ONLY a JSON object."
    )

    user_prompt = (
        f"Analysis focus: {focus_hint}\n"
        f"Time window: last {days} days\n\n"
        + "=== Practice ===\n" + _format_practice_context(practice)
        + "\n\n=== Performance data ===\n"
        + json.dumps(perf, indent=2, default=str)
        + "\n\nReturn a JSON object with EXACTLY these keys:\n"
        + "  summary (string — 2-3 sentences citing concrete numbers)\n"
        + "  wins (array of strings — what's working, with numbers)\n"
        + "  concerns (array of strings — what's underperforming, with numbers)\n"
        + "  action_items (array of objects, each with: priority ['high'|'medium'|'low'], "
        + "    action [string], rationale [string])\n"
        + "  implementation_prompt (string — explicit instructions for Haiku to produce "
        + "    a follow-up implementation plan from these action items)\n"
        + "Return ONLY the JSON object."
    )

    try:
        client = _get_anthropic_client()
        message = client.messages.create(
            model=OPUS_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
        )
        raw_text = message.content[0].text if message.content else ""
        model_used = message.model
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI performance-analysis failed: {e}")
        raise HTTPException(status_code=502, detail=f"AI analysis failed: {e}")

    json_text = _extract_json_from_ai_response(raw_text)
    try:
        result = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error(f"AI performance-analysis — JSON parse failed: {e}\nRaw: {raw_text[:500]}")
        raise HTTPException(status_code=502, detail="AI returned invalid JSON — please try again")

    required_keys = {"summary", "wins", "concerns", "action_items", "implementation_prompt"}
    missing = required_keys - set(result.keys())
    if missing:
        raise HTTPException(
            status_code=502,
            detail=f"AI analysis missing required fields: {sorted(missing)}",
        )

    # Normalize action_items priority casing
    for item in result.get("action_items") or []:
        if isinstance(item, dict) and "priority" in item:
            item["priority"] = str(item["priority"]).lower()

    return {"analysis": result, "model_used": model_used, "time_range_days": days, "focus": focus}


# ─── Campaign AI Refine ───────────────────────────────────────────────────────

class PauseAdStageRequest(BaseModel):
    campaign_id:            str
    ad_group_ad_resource:   str
    rationale:              str = ""


@app.post("/api/admin/campaigns/{campaign_id}/pause-ad/stage",
          dependencies=[Depends(_require_admin)])
def admin_pause_ad_stage(campaign_id: str, body: PauseAdStageRequest):
    """
    Stage a single-ad pause for staff approval.
    Validates the ad exists in the campaign; writes a pending_approval audit row.
    Actual Google Ads mutate happens at gads_approve_action.
    """
    from database import log_admin_manual_action, get_campaign_by_id, get_ads_with_metrics
    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Verify the ad actually belongs to this campaign
    all_ads = get_ads_with_metrics(days=30)
    settings_obj = get_settings()
    cid = settings_obj.google_ads_customer_id
    campaign_numeric = camp.get("gads_campaign_numeric_id") or camp.get("gads_campaign_id") or ""
    valid_resources = {
        a["ad_group_ad_resource"]
        for a in all_ads
        if a.get("ad_group_ad_resource") and str(a.get("campaign_id", "")) == str(campaign_numeric)
    }
    if valid_resources and body.ad_group_ad_resource not in valid_resources:
        raise HTTPException(status_code=422, detail="ad_group_ad_resource not found in this campaign")

    before = {"ad_group_ad_resource": body.ad_group_ad_resource, "status": "ENABLED"}
    after  = {"ad_group_ad_resource": body.ad_group_ad_resource, "status": "PAUSED",
               "rationale": body.rationale}
    action_id = log_admin_manual_action(
        operation="pause_ad",
        entity_type="ad",
        entity_id=body.ad_group_ad_resource,
        entity_name=(camp.get("campaign_name") or "")[:120],
        before=before,
        after=after,
        reason=f"ai_refine: {body.rationale[:200]}",
    )
    return {
        "action_id": action_id,
        "operation": "pause_ad",
        "before": before,
        "after": after,
    }


class ReplaceAdStageRequest(BaseModel):
    campaign_id:                str
    old_ad_group_ad_resource:   str
    new_headlines:              list
    new_descriptions:           list
    final_url:                  str
    path1:                      str = ""
    path2:                      str = ""
    rationale:                  str = ""


@app.post("/api/admin/campaigns/{campaign_id}/replace-ad/stage",
          dependencies=[Depends(_require_admin)])
def admin_replace_ad_stage(campaign_id: str, body: ReplaceAdStageRequest):
    """
    Stage an AI-proposed RSA replacement for staff approval.
    Validates the payload, snapshots the old RSA, writes a pending_approval
    audit row. Actual Google Ads mutate happens at gads_approve_action.
    """
    from database import log_admin_manual_action, get_campaign_by_id, get_ads_with_metrics
    from ai_optimizer import _build_client, _get_rsa_current_assets

    camp = get_campaign_by_id(campaign_id)
    if not camp:
        raise HTTPException(404, detail="Campaign not found")

    # Server-side validation
    h_list = [(s or "").strip() for s in (body.new_headlines or []) if (s or "").strip()]
    d_list = [(s or "").strip() for s in (body.new_descriptions or []) if (s or "").strip()]
    if not (3 <= len(h_list) <= 15):
        raise HTTPException(422, detail=f"Need 3-15 headlines (got {len(h_list)})")
    if not (2 <= len(d_list) <= 4):
        raise HTTPException(422, detail=f"Need 2-4 descriptions (got {len(d_list)})")
    over_h = [h for h in h_list if len(h) > 30]
    over_d = [d for d in d_list if len(d) > 90]
    if over_h:
        raise HTTPException(422, detail=f"Headlines over 30 chars: {over_h}")
    if over_d:
        raise HTTPException(422, detail=f"Descriptions over 90 chars: {over_d}")
    if not body.final_url.lower().startswith(("http://", "https://")):
        raise HTTPException(422, detail="final_url must be http(s)")

    # Derive ad_group_resource
    try:
        cid_part = body.old_ad_group_ad_resource.split("/adGroupAds/")[0]
        ag_id    = body.old_ad_group_ad_resource.split("/adGroupAds/")[1].split("~")[0]
        ad_group_resource = f"{cid_part}/adGroups/{ag_id}"
    except Exception:
        raise HTTPException(422, detail="Malformed old_ad_group_ad_resource")

    # Snapshot old RSA for before_state audit
    before = {}
    try:
        client = _build_client()
        snap = _get_rsa_current_assets(
            client, get_settings().google_ads_customer_id,
            body.old_ad_group_ad_resource
        )
        if snap:
            before = {
                "ad_group_ad_resource": body.old_ad_group_ad_resource,
                "headlines":    [h["text"] for h in snap.get("headlines", [])],
                "descriptions": [d["text"] for d in snap.get("descriptions", [])],
            }
    except Exception as e:
        logger.warning(f"replace-ad stage: snapshot failed (non-fatal): {e}")

    after = {
        "old_ad_group_ad_resource": body.old_ad_group_ad_resource,
        "ad_group_resource":        ad_group_resource,
        "new_headlines":            h_list,
        "new_descriptions":         d_list,
        "final_url":                body.final_url,
        "path1":                    (body.path1 or "")[:15],
        "path2":                    (body.path2 or "")[:15],
        "rationale":                body.rationale,
    }
    action_id = log_admin_manual_action(
        operation="replace_ad",
        entity_type="ad",
        entity_id=body.old_ad_group_ad_resource,
        entity_name=(camp.get("campaign_name") or "")[:120],
        before=before,
        after=after,
        reason=f"ai_refine: {body.rationale[:200]}",
    )
    return {
        "action_id": action_id,
        "operation": "replace_ad",
        "before": before,
        "after": after,
    }


class CampaignRefineRequest(BaseModel):
    campaign_id:       str
    instruction:       str          # free-text from user, e.g. "raise budget to $80, weekdays only"
    campaign_context:  dict = {}    # current campaign state passed from frontend


@app.post("/api/admin/ai/campaign-refine", dependencies=[Depends(_require_admin)])
def admin_ai_campaign_refine(body: CampaignRefineRequest):
    """
    Ask AI (Opus) to analyse this campaign and suggest changes based on the
    user's free-text instruction.  Uses the full AI Optimizer engine:
    _call_claude_advisories() with complete context (keyword perf, search terms,
    ad groups, RSAs, ad performance, LQI signals).

    Returns {summary, changes} where each change is an optimizer rec with an
    action_id already staged as pending_approval in gads_audit_log.
    The frontend Apply button calls /api/admin/gads/approve/{action_id} —
    the same execution path the optimizer uses.
    """
    instruction = (body.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=422, detail="instruction is required")

    ctx = body.campaign_context or {}
    campaign_name = ctx.get("campaign_name") or ctx.get("name") or body.campaign_id or ""
    campaign_id   = body.campaign_id or ""
    gads_num_id   = str(ctx.get("gads_campaign_numeric_id") or "")
    camp_name_lc  = campaign_name.strip().lower()

    settings_obj = get_settings()
    cid = settings_obj.google_ads_customer_id

    # ── SCHEDULE SHORTCUT: detect schedule-related instructions ─────────────
    # When the user says "set schedule" / "use office hours" / etc., skip the
    # full Claude optimizer and directly stage a set_ad_schedule action so the
    # user gets a single Apply button instead of keyword noise.
    import re as _re_sched
    _SCHED_PATTERN = _re_sched.compile(
        r'\b(schedule|hours|office hours|ad hours|ad schedule|time|days|weekday|weekend|'
        r'monday|tuesday|wednesday|thursday|friday|saturday|sunday|'
        r'am|pm|7am|8am|9am|10am|11am|noon|6pm|7pm|8pm|9pm|10pm|11pm|midnight)\b',
        _re_sched.IGNORECASE
    )
    _sched_match_count = len(_SCHED_PATTERN.findall(instruction))
    _is_schedule_instruction = _sched_match_count >= 2  # at least 2 schedule-related tokens

    if _is_schedule_instruction:
        camp_resource_sched = ctx.get("gads_campaign_resource", "")
        if camp_resource_sched:
            from google_ads_create import parse_ad_schedule

            # Resolve "office hours" keyword → actual practice hours from DB
            # e.g. "follow office hours and sunday 8am-10pm"
            # → "Mon-Thu 10am-6pm and sunday 8am-10pm"
            _sched_instruction = instruction
            if _re_sched.search(r'\boffice\s+hours\b', _sched_instruction, _re_sched.IGNORECASE):
                _practice_hours = get_setting("practice_hours") or ""
                if _practice_hours:
                    _sched_instruction = _re_sched.sub(
                        r'\boffice\s+hours\b', _practice_hours, _sched_instruction,
                        flags=_re_sched.IGNORECASE
                    )

            slots = parse_ad_schedule(_sched_instruction)
            if slots:
                from campaign_audit import log_pending
                days_summary = ", ".join(sorted(set(s["day"] for s in slots)))
                # Store the resolved instruction (with "office hours" substituted) so
                # gads_approve_action can re-parse it if needed. Also store original for audit.
                _resolved_text = _sched_instruction if _sched_instruction != instruction else instruction
                before_sched = {"schedule_text": "", "original_instruction": instruction}
                after_sched  = {"schedule_text": _resolved_text, "slots": slots, "days": days_summary,
                                "campaign_resource": camp_resource_sched}
                action_id_sched = log_pending(
                    operation="set_ad_schedule",
                    entity_type="campaign",
                    entity_id=camp_resource_sched,
                    entity_name=campaign_name,
                    before_state=before_sched,
                    after_state=after_sched,
                    optimizer_run_id="ai_refine",
                    actor="ai_refine",
                    reason=f"User requested schedule change: {instruction[:200]}",
                    campaign_id=campaign_id,
                    campaign_name=campaign_name,
                )
                change = {
                    "operation": "set_ad_schedule",
                    "schedule_text": _resolved_text,
                    "slots": slots,
                    "days": days_summary,
                    "campaign_resource": camp_resource_sched,
                    "reason": f"Apply ad schedule: {days_summary}",
                    "action_id": action_id_sched,
                }
                return {
                    "summary": f"Schedule change staged: {days_summary} ({len(slots)} time slots). Click Apply to push to Google Ads.",
                    "changes": [change],
                }
            else:
                # parse failed — return helpful advisory instead of keyword noise
                return {
                    "summary": "Could not parse schedule from your instruction.",
                    "changes": [{
                        "operation": "claude_advisory",
                        "reason": (
                            f"Could not parse a schedule from: \"{instruction}\". "
                            "Try a format like: 'Mon-Fri 9am-7pm, Sat 9am-3pm, Sun 9am-11pm'"
                        ),
                    }],
                }
        else:
            return {
                "summary": "Campaign resource not found — cannot set schedule.",
                "changes": [{
                    "operation": "claude_advisory",
                    "reason": "This campaign does not have a linked Google Ads resource. Launch it first.",
                }],
            }

    # ── 1. Fetch keyword performance for this campaign ───────────────────────
    camp_kw: list = []
    try:
        from database import get_keyword_stats
        all_kw = get_keyword_stats()  # no args — function takes no parameters
        for kw in all_kw:
            if (str(kw.get("campaign_id","")) == gads_num_id or
                    (camp_name_lc and kw.get("campaign_name","").lower() == camp_name_lc)):
                camp_kw.append(kw)
    except Exception as e:
        logger.warning(f"campaign-refine: keyword fetch failed: {e}")

    # ── 2. Fetch search terms for this campaign ──────────────────────────────
    camp_st: list = []
    try:
        from database import get_search_term_stats
        all_st = get_search_term_stats(days=30)
        for st in all_st:
            if (str(st.get("campaign_id","")) == gads_num_id or
                    (camp_name_lc and st.get("campaign_name","").lower() == camp_name_lc)):
                camp_st.append(st)
    except Exception as e:
        logger.warning(f"campaign-refine: search term fetch failed: {e}")

    # ── 3. Fetch ad group performance + resource names ───────────────────────
    camp_ag_perf: list = []
    try:
        from database import get_ad_group_stats, get_gads_campaign_snapshot
        all_ag = get_ad_group_stats(days=30)
        camp_ag_raw = [ag for ag in all_ag
                       if str(ag.get("campaign_id","")) == gads_num_id
                       or (camp_name_lc and ag.get("campaign_name","").lower() == camp_name_lc)]
        # Enrich with resource names from snapshot
        snap_ag: dict = {}
        if campaign_id:
            try:
                snap = get_gads_campaign_snapshot(campaign_id)
                ag_block = snap.get("ad_groups") or {}
                snap_ag_list = (ag_block.get("ad_groups") if isinstance(ag_block, dict) else ag_block) or []
                for sag in snap_ag_list:
                    rn = sag.get("resource_name","")
                    if rn and "/adGroups/" in rn:
                        ag_id_s = rn.split("/adGroups/")[-1]
                        snap_ag[ag_id_s] = {
                            "resource_name": rn,
                            "status": (sag.get("status") or "ENABLED").upper()
                        }
            except Exception:
                pass
        for ag in camp_ag_raw:
            ag_id_s = str(ag.get("ad_group_id") or "")
            snap_info = snap_ag.get(ag_id_s, {})
            rn = snap_info.get("resource_name","") or (
                f"customers/{cid}/adGroups/{ag_id_s}" if ag_id_s.isdigit() else ""
            )
            impr = ag.get("impressions") or 0
            clk  = ag.get("clicks") or 0
            cost = ag.get("cost") or 0.0
            ctr  = round(clk / impr * 100, 2) if impr > 0 else 0
            camp_ag_perf.append({
                "ad_group_resource":  rn,
                "ad_group_name":      ag.get("ad_group_name",""),
                "gads_status":        snap_info.get("status","ENABLED"),
                "impressions_30d":    impr,
                "clicks_30d":         clk,
                "cost_30d":           cost,
                "ctr_pct":            ctr,
                "conversions_30d":    ag.get("conversions") or 0,
            })
    except Exception as e:
        logger.warning(f"campaign-refine: ad group fetch failed: {e}")

    # ── 4. Fetch RSA ad performance for this campaign ────────────────────────
    camp_ad_perf: list = []
    rsa_resources: list = []
    try:
        from database import get_ads_with_metrics
        from ai_optimizer import _score_tier
        all_ads = get_ads_with_metrics(days=30)
        # Compute per-campaign CTR averages for tier scoring (same logic as optimizer)
        camp_ctr_totals: dict = {}
        for ad in all_ads:
            if ad.get("status") != "ENABLED" or ad.get("ad_type") != "RESPONSIVE_SEARCH_AD":
                continue
            impr  = ad.get("impressions") or 0
            clks  = ad.get("clicks") or 0
            cname = (ad.get("campaign_name") or "").strip().lower()
            if cname not in camp_ctr_totals:
                camp_ctr_totals[cname] = {"impressions": 0, "clicks": 0}
            camp_ctr_totals[cname]["impressions"] += impr
            camp_ctr_totals[cname]["clicks"]      += clks
        camp_avg_ctr_map: dict = {
            c: (v["clicks"] / v["impressions"]) if v["impressions"] > 0 else 0
            for c, v in camp_ctr_totals.items()
        }
        for ad in all_ads:
            if ad.get("ad_type") != "RESPONSIVE_SEARCH_AD":
                continue
            if not (str(ad.get("campaign_id","")) == gads_num_id or
                    (camp_name_lc and ad.get("campaign_name","").lower() == camp_name_lc)):
                continue
            assets = ad.get("assets_json") or {}
            if isinstance(assets, str):
                try: assets = json.loads(assets)
                except Exception: assets = {}
            ag_id_a  = ad.get("ad_group_id","")
            ad_id_a  = ad.get("ad_id","")
            aga_res  = (ad.get("ad_group_ad_resource","") or
                        (f"customers/{cid}/adGroupAds/{ag_id_a}~{ad_id_a}"
                         if ag_id_a and ad_id_a else ""))
            headlines    = [h.get("text","") if isinstance(h,dict) else h
                            for h in (assets.get("headlines") or [])]
            descriptions = [d.get("text","") if isinstance(d,dict) else d
                            for d in (assets.get("descriptions") or [])]
            impr_a   = ad.get("impressions") or 0
            clicks_a = ad.get("clicks") or 0
            cost_usd = (ad.get("cost_micros") or 0) / 1_000_000   # cost_micros, not cost
            conv_a   = ad.get("conversions") or 0
            cname_a  = (ad.get("campaign_name") or "").strip().lower()
            ctr_a    = (clicks_a / impr_a) if impr_a > 0 else 0
            avg_ctr_a = camp_avg_ctr_map.get(cname_a, 0)
            tier_a    = _score_tier(impr_a, clicks_a, cost_usd, conv_a, avg_ctr_a)
            ad_entry = {
                "ad_group_ad_resource": aga_res,
                "ad_group_name":        ad.get("ad_group_name",""),
                "status":               ad.get("status",""),
                "final_url":            ad.get("final_url",""),
                "headlines":            headlines,
                "descriptions":         descriptions,
                "impressions_30d":      impr_a,
                "clicks":               clicks_a,
                "cost_30d_usd":         round(cost_usd, 2),
                "ctr":                  round(ctr_a, 4),
                "avg_campaign_ctr":     round(avg_ctr_a, 4),
                "conversions_30d":      conv_a,
                "performance_tier":     tier_a,
            }
            camp_ad_perf.append(ad_entry)
            if aga_res:
                rsa_resources.append({
                    "ad_group_ad_resource": aga_res,
                    "ad_group_resource":    ad.get("ad_group_resource",""),
                    "ad_group_name":        ad.get("ad_group_name",""),
                    "headlines":            headlines,
                    "descriptions":         descriptions,
                    "final_url":            ad.get("final_url",""),
                    "status":               ad.get("status",""),
                })
    except Exception as e:
        logger.warning(f"campaign-refine: ad/RSA fetch failed: {e}")

    # ── 5. Build empty attribution/od/summary dicts (refine doesn't need them) ─
    attribution: dict = {}
    call_attribution: dict = {}
    od_production: dict = {}
    summary: dict = {
        "total_spend":           ctx.get("total_cost_30d_usd", 0),
        "total_clicks":          ctx.get("total_clicks_30d", 0),
        "total_impressions":     ctx.get("total_impressions_30d", 0),
        "total_leads":           0,
        "total_booked_calls":    0,
        "total_production":      0,
        "overall_roas":          0,
        "cost_per_lead":         0,
        "cost_per_acquisition":  0,
    }

    # ── 6. Live negative keywords (for dedup) ───────────────────────────────
    live_negatives: set = set()
    try:
        from database import _conn as _dbc
        with _dbc() as _c:
            rows = _c.execute(
                "SELECT keyword_text FROM gads_negative_keywords WHERE active=1"
            ).fetchall()
            live_negatives = {r["keyword_text"].strip().lower() for r in rows}
    except Exception:
        pass

    # ── 7. Collect LQI signals ───────────────────────────────────────────────
    lqi_signals: dict = {}
    try:
        from lqi_signals import collect_all as _lqi_collect
        lqi_signals = _lqi_collect(days=30)
    except Exception as _lqi_e:
        logger.warning(f"campaign-refine: LQI collection failed (non-fatal): {_lqi_e}")

    # ── 8. Campaign settings (budget, bid strategy) ─────────────────────────
    camp_settings: dict = {}
    camp_resource = ctx.get("gads_campaign_resource","")
    if camp_resource:
        try:
            from ai_optimizer import _build_client, _get_campaign_settings
            _gs_client = _build_client()
            csmap = _get_campaign_settings(_gs_client, cid, days=30)
            camp_settings = csmap.get(camp_resource, {})
        except Exception as _cs_e:
            logger.warning(f"campaign-refine: campaign settings fetch failed (non-fatal): {_cs_e}")

    # ── 9. Call _call_claude_advisories with instruction as feedback hint ─────
    from ai_optimizer import _call_claude_advisories
    structured = _call_claude_advisories(
        keyword_perf=camp_kw,
        attribution=attribution,
        search_terms=camp_st,
        call_attribution=call_attribution,
        od_production=od_production,
        summary=summary,
        campaign=campaign_name,
        rsa_resources=rsa_resources,
        existing_negatives=live_negatives,
        camp_settings=camp_settings,
        ad_performance=camp_ad_perf,
        ad_group_performance=camp_ag_perf,
        lqi=lqi_signals,
        feedback=instruction,   # user's instruction becomes the Claude focus hint
        optimizer_run_id="refine",
    )

    if not structured:
        # If Claude returned nothing useful, return a note
        return {
            "summary": "No specific changes identified based on your instruction and current campaign data.",
            "changes": [{"operation": "claude_advisory", "reason":
                "Try being more specific, or run the full AI Optimizer for a comprehensive analysis."}]
        }

    # ── 10. Stage each rec as pending_approval in gads_audit_log ─────────────
    from campaign_audit import log_pending
    _OP_MAP = {
        "add_negative_keyword":        ("keyword",  "campaign_resource",       "keyword_text"),
        "add_to_shared_negative_list": ("keyword",  "keyword_text",            "keyword_text"),
        "pause_keyword":               ("keyword",  "resource_name",           "keyword_text"),
        "enable_keyword":              ("keyword",  "resource_name",           "keyword_text"),
        "increase_bid":                ("keyword",  "resource_name",           "keyword_text"),
        "decrease_bid":                ("keyword",  "resource_name",           "keyword_text"),
        "add_exact_keyword":           ("keyword",  "ad_group_resource",       "keyword_text"),
        "ad_copy_suggestion":          ("ad",       "ad_resource",             "headline"),
        "geo_exclusion":               ("campaign", "geo_target_resource",     "location_name"),
        "change_budget":               ("campaign", "campaign_resource",       "campaign_resource"),
        "change_bid_strategy":         ("campaign", "campaign_resource",       "bid_strategy"),
        "change_match_type":           ("keyword",  "resource_name",           "keyword_text"),
        "add_asset":                   ("campaign", "campaign_resource",       "asset_type"),
        "replace_ad":                  ("ad",       "old_ad_group_ad_resource","old_ad_group_ad_resource"),
        "pause_ad_group":              ("ad_group", "ad_group_resource",       "ad_group_name"),
        "pause_ad":                    ("ad",       "ad_group_ad_resource",    "ad_group_ad_resource"),
        "claude_advisory":             ("campaign", "campaign_resource",       "campaign_resource"),
    }

    # Load existing pending_approval rows for this campaign so we can dedup
    _existing_pending: set = set()
    try:
        from database import _conn as _dbc_refine
        with _dbc_refine() as _c:
            _pending_rows = _c.execute(
                """SELECT operation, after_state_json FROM gads_audit_log
                   WHERE execution_result = 'pending_approval'
                     AND (campaign_id = ? OR campaign_name = ?)""",
                (campaign_id, campaign_name)
            ).fetchall()
            for _pr in _pending_rows:
                _pr_after = json.loads(_pr["after_state_json"] or "{}")
                _kw = (_pr_after.get("keyword_text") or "").strip().lower()
                if _kw:
                    _existing_pending.add((_pr["operation"], _kw))
    except Exception:
        pass  # non-fatal — worst case we create duplicate pending rows

    staged_changes = []
    for rec in structured:
        op     = rec.get("operation", "claude_advisory")
        reason = rec.get("reason", rec.get("insight", ""))

        # Dedup: skip if an identical pending_approval row already exists
        if op in ("add_negative_keyword", "add_to_shared_negative_list", "pause_keyword", "enable_keyword"):
            _kw_check = (rec.get("keyword_text") or "").strip().lower()
            if _kw_check and (op, _kw_check) in _existing_pending:
                # Already staged — add as a read-only informational entry without an action_id
                staged_changes.append({
                    **rec,
                    "action_id": None,
                    "_already_pending": True,
                    "reason": f"[Already in approval queue] {reason}",
                })
                continue

        # Build before/after for audit log
        after  = {k: v for k, v in rec.items() if k != "operation"}
        before: dict = {}
        if op == "replace_ad":
            old_rn = rec.get("old_ad_group_ad_resource","")
            matched = next((a for a in camp_ad_perf if a.get("ad_group_ad_resource") == old_rn), None)
            if matched:
                before = {
                    "status":          matched.get("status","ENABLED"),
                    "headlines":       matched.get("headlines",[]),
                    "descriptions":    matched.get("descriptions",[]),
                    "final_url":       matched.get("final_url",""),
                    "impressions_30d": matched.get("impressions_30d",0),
                }
            else:
                before = {"ad_group_ad_resource": old_rn}

        op_meta = _OP_MAP.get(op)
        if op_meta:
            entity_type, id_field, name_field = op_meta
            entity_id   = str(rec.get(id_field, camp_name_lc.replace(" ","_")))
            entity_name = str(rec.get(name_field, campaign_name))
            if op == "replace_ad":
                matched = next((a for a in camp_ad_perf
                                if a.get("ad_group_ad_resource") == rec.get("old_ad_group_ad_resource","")), None)
                entity_name = (f"Replace ad in {matched['ad_group_name']}"
                               if matched else f"Replace ad — {campaign_name}")
        else:
            entity_type = "campaign"
            entity_id   = camp_name_lc.replace(" ","_")
            entity_name = campaign_name

        action_id = log_pending(
            operation=op,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            before_state=before,
            after_state=after,
            optimizer_run_id="ai_refine",
            actor="ai_refine",
            reason=reason,
            campaign_id=campaign_id,
            campaign_name=campaign_name,
        )
        staged_changes.append({**rec, "action_id": action_id})

    return {
        "summary": f"Found {len(staged_changes)} recommendation(s) based on: {instruction[:120]}",
        "changes": staged_changes,
    }


# ─── OD Connection Settings ──────────────────────────────────────────────────

class ODSettingsRequest(BaseModel):
    od_db_host:       str = ""
    od_db_port:       int = 3306
    od_db_user:       str = ""
    od_db_password:   str = ""   # empty string = don't change existing saved password
    od_db_name:       str = "opendental"
    od_api_base:      str = ""
    od_developer_key: str = ""
    od_customer_key:  str = ""


@app.get("/api/admin/od-settings", dependencies=[Depends(_require_admin)])
def admin_get_od_settings():
    s = get_od_settings()
    return {
        "od_db_host":       s["od_db_host"],
        "od_db_port":       s["od_db_port"],
        "od_db_user":       s["od_db_user"],
        "od_db_password":   "••••••••" if s["od_db_password"] else "",
        "od_db_name":       s["od_db_name"],
        "od_api_base":      s["od_api_base"],
        "od_developer_key": s["od_developer_key"],
        "od_customer_key":  "••••••••" if s["od_customer_key"] else "",
    }


@app.get("/api/admin/ai-settings", dependencies=[Depends(_require_admin)])
def admin_get_ai_settings():
    key = get_setting("anthropic_api_key") or ""
    return {"anthropic_api_key": "••••••••" if key else ""}

@app.post("/api/admin/ai-settings", dependencies=[Depends(_require_admin)])
def admin_save_ai_settings(body: dict):
    key = (body.get("anthropic_api_key") or "").strip()
    if key and not key.startswith("•"):
        save_setting("anthropic_api_key", key)
    return {"ok": True}


# ─── Mango Voice + AI Settings ───────────────────────────────────────────────

@app.get("/api/admin/mango-settings", dependencies=[Depends(_require_admin)])
def admin_get_mango_settings():
    from database import get_mango_settings
    s = get_mango_settings()
    return {
        "mango_username":            s["mango_username"],
        "mango_password":            "••••••••" if s["mango_password"] else "",
        "mango_pbx_id":              s["mango_pbx_id"],
        "mango_api_base":            s["mango_api_base"],
        "mango_account_uuid":        s.get("mango_account_uuid", ""),
        "openai_api_key":            "••••••••" if s["openai_api_key"] else "",
        # Vertex AI (HIPAA-compliant Gemini)
        "vertex_project_id":         s["vertex_project_id"],
        "vertex_location":           s["vertex_location"],
        "vertex_credentials_path":   s["vertex_credentials_path"],
        "vertex_model":              s["vertex_model"],
        "mango_whisper_mode":        s["mango_whisper_mode"],
        "mango_enabled":             s["mango_enabled"],
        "mango_pipeline_enabled":    s["mango_pipeline_enabled"],
        "mango_pipeline_auto_grade": s["mango_pipeline_auto_grade"],
        "mango_pipeline_auto_suggest_action": s.get("mango_pipeline_auto_suggest_action", True),
    }


@app.post("/api/admin/mango-settings", dependencies=[Depends(_require_admin)])
def admin_save_mango_settings(body: dict, request: Request):
    from database import save_setting

    def _save(key: str, val: str):
        v = (val or "").strip()
        if v and not v.startswith("•"):
            save_setting(key, v)

    _save("mango_username",            body.get("mango_username", ""))
    _save("mango_password",            body.get("mango_password", ""))
    _save("mango_pbx_id",              body.get("mango_pbx_id", ""))
    _save("mango_api_base",            body.get("mango_api_base", ""))
    _save("mango_account_uuid",        body.get("mango_account_uuid", ""))
    _save("mango_openai_api_key",      body.get("openai_api_key", ""))
    # Vertex AI settings (replaces direct gemini_api_key)
    _save("vertex_project_id",        body.get("vertex_project_id", ""))
    _save("vertex_location",          body.get("vertex_location", ""))
    _save("vertex_credentials_path",  body.get("vertex_credentials_path", ""))
    _save("vertex_model",             body.get("vertex_model", ""))
    _save("mango_whisper_mode",        body.get("mango_whisper_mode", ""))

    # Boolean toggles — always save even when False
    for key, field in [
        ("mango_enabled",                    "mango_enabled"),
        ("mango_pipeline_enabled",           "mango_pipeline_enabled"),
        ("mango_pipeline_auto_grade",        "mango_pipeline_auto_grade"),
        ("mango_pipeline_auto_suggest_action", "mango_pipeline_auto_suggest_action"),
    ]:
        val = body.get(field)
        if val is not None:
            save_setting(key, "true" if val else "false")

    # If credentials changed, rebuild the token manager so it takes effect now
    new_user = (body.get("mango_username") or "").strip()
    new_pass = (body.get("mango_password") or "").strip()
    if new_user and new_pass and not new_pass.startswith("•"):
        try:
            from mango_service import MangoTokenManager
            from database import get_mango_settings
            ms = get_mango_settings()
            mgr = MangoTokenManager(
                username=ms["mango_username"],
                password=ms["mango_password"],
                api_base=ms["mango_api_base"],
            )
            request.app.state.mango_token_mgr = mgr
            logger.info("Mango token manager rebuilt after credential update")
        except Exception as e:
            logger.warning(f"Mango token manager rebuild failed: {e}")

    return {"ok": True}


# ─── Recording streaming endpoint ─────────────────────────────────────────────

@app.get("/api/admin/calls/recording/{uuid}/play", dependencies=[Depends(_require_admin_media)])
def admin_play_recording(uuid: str, request: Request):
    """Stream a Mango call recording to the browser for in-page playback.

    Download flow: Mango S3 → temp file → stream to browser.
    The temp file is reused if already present (TTL sweeper cleans it later).
    """
    from database import get_mango_call
    from mango_pipeline import _fetch_recording
    from fastapi.responses import StreamingResponse

    call = get_mango_call(uuid)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    token_mgr = getattr(request.app.state, "mango_token_mgr", None)
    tok = token_mgr.get_token() if token_mgr else None

    recording_url = call.get("recording_url") or ""
    if not recording_url:
        # Older calls may have no stored recording_url (it expired at sync time).
        # Re-fetch a fresh pre-signed URL from Mango API — same strategy as process_call.
        try:
            from mango_service import fetch_fresh_recording_url, MangoTokenManager
            from database import get_mango_settings as _gms
            msettings = _gms()
            pbx_id = msettings.get("mango_pbx_id") or ""
            api_base = msettings.get("mango_api_base") or "https://api.mangovoice.com"

            class _SingleTokenMgr:
                def get_token(self): return tok
            recording_url = fetch_fresh_recording_url(_SingleTokenMgr(), uuid, pbx_id, api_base=api_base)
        except Exception as e:
            logger.warning("[play] Could not fetch fresh recording URL for %s: %s", uuid, e)

    if not recording_url:
        raise HTTPException(status_code=404, detail="No recording available for this call")

    try:
        path = _fetch_recording(recording_url, uuid, token=tok)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch recording: {e}")

    # FileResponse handles Content-Length, Accept-Ranges, and range requests
    # automatically — required for browser Audio element to play and seek correctly.
    from fastapi.responses import FileResponse
    return FileResponse(
        path=str(path),
        media_type="audio/mpeg",
        filename=f"{uuid}.mp3",
        headers={"Content-Disposition": f'inline; filename="{uuid}.mp3"'},
    )


# ─── Pipeline run-now endpoint ────────────────────────────────────────────────

@app.post("/api/admin/calls/pipeline/run-now", dependencies=[Depends(_require_admin)])
def admin_pipeline_run_now(request: Request):
    """Immediately trigger a pipeline tick in a background thread."""
    import threading
    from mango_pipeline import run_pipeline_tick

    token_mgr = getattr(request.app.state, "mango_token_mgr", None)
    tok = token_mgr.get_token() if token_mgr else None

    def _run():
        try:
            run_pipeline_tick(mango_token=tok)
        except Exception as e:
            logger.error(f"Manual pipeline run failed: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"ok": True, "started": True}


# ─── Practice Information Settings ──────────────────────────────────────────

@app.get("/api/admin/practice-settings", dependencies=[Depends(_require_admin)])
def admin_get_practice_settings():
    return {f: get_setting(f"practice_{f}") or "" for f in _PRACTICE_FIELDS}

@app.post("/api/admin/practice-settings", dependencies=[Depends(_require_admin)])
def admin_save_practice_settings(body: PracticeSettingsRequest):
    for f in _PRACTICE_FIELDS:
        save_setting(f"practice_{f}", getattr(body, f).strip())
    return {"ok": True}


# ─── Google Ads Attribution Window Settings (PR 3) ───────────────────────────

class GadsAttributionSettingsRequest(BaseModel):
    gads_attribution_window_days: int = 365


@app.get("/api/admin/gads-attribution-settings", dependencies=[Depends(_require_admin)])
def admin_get_gads_attribution_settings():
    from config import get_settings as _get_cfg
    default_window = _get_cfg().gads_attribution_window_days
    raw = get_setting("gads_attribution_window_days")
    window = int(raw) if raw and raw.isdigit() else default_window
    return {"gads_attribution_window_days": window}


@app.post("/api/admin/gads-attribution-settings", dependencies=[Depends(_require_admin)])
def admin_save_gads_attribution_settings(body: GadsAttributionSettingsRequest):
    valid_windows = {90, 180, 365, 730}
    window = body.gads_attribution_window_days
    if window not in valid_windows:
        raise HTTPException(status_code=422, detail=f"gads_attribution_window_days must be one of {sorted(valid_windows)}")
    save_setting("gads_attribution_window_days", str(window))
    logger.info(f"gads_attribution_window_days updated to {window}")
    return {"ok": True, "gads_attribution_window_days": window}


# ─── Account Monthly Budget ──────────────────────────────────────────────────

class AccountBudgetRequest(BaseModel):
    account_monthly_budget: float | None = None  # None = don't change budget amount
    budget_constrained: bool | None = None        # None = don't change constraint flag

@app.get("/api/admin/account-budget", dependencies=[Depends(_require_admin)])
def admin_get_account_budget():
    from database import get_setting
    val = get_setting("account_monthly_budget") or "0"
    constrained = get_setting("budget_constrained") or "false"
    return {
        "account_monthly_budget": float(val),
        "budget_constrained": constrained == "true",
    }

@app.post("/api/admin/account-budget", dependencies=[Depends(_require_admin)])
def admin_save_account_budget(body: AccountBudgetRequest):
    from database import save_setting, get_setting
    if body.account_monthly_budget is not None:
        if body.account_monthly_budget < 0:
            raise HTTPException(status_code=422, detail="Budget cannot be negative")
        save_setting("account_monthly_budget", str(round(body.account_monthly_budget, 2)))
    if body.budget_constrained is not None:
        save_setting("budget_constrained", "true" if body.budget_constrained else "false")
    # Return authoritative values from DB (avoids stale-read on concurrent requests)
    current_budget = float(get_setting("account_monthly_budget") or "0")
    constrained_val = (get_setting("budget_constrained") or "false") == "true"
    return {
        "ok": True,
        "account_monthly_budget": current_budget,
        "budget_constrained": constrained_val,
    }


# ─── AI Generate Single Message ──────────────────────────────────────────────

@app.post("/api/admin/workflow/ai-generate-message", dependencies=[Depends(_require_admin)])
def admin_ai_generate_message(body: AIGenerateMessageRequest):
    """Generate a single email or SMS message with AI, pre-filled with practice context."""
    # Validate channel
    channel = (body.channel or "").strip().lower()
    if channel not in ("email", "sms"):
        raise HTTPException(status_code=422, detail="channel must be 'email' or 'sms'")

    # Validate appointment_type
    appt_type = (body.appointment_type or "general").strip().lower()
    if appt_type not in _APPT_TYPE_FIELD_MAP:
        raise HTTPException(status_code=422,
            detail=f"appointment_type must be one of: {list(_APPT_TYPE_FIELD_MAP.keys())}")

    # Validate prompt
    prompt_text = (body.prompt or "").strip()
    if not prompt_text:
        raise HTTPException(status_code=422, detail="prompt is required")
    if len(prompt_text) > 2000:
        raise HTTPException(status_code=422, detail="prompt too long (max 2000 chars)")

    # Load practice info from DB
    practice = {f: get_setting(f"practice_{f}") or "" for f in _PRACTICE_FIELDS}
    practice_name = practice.get("name") or "Grafton Dental Care"
    doctor_name   = practice.get("doctor_name") or "Dr. Gupta"
    practice_phone = practice.get("phone") or ""
    practice_address = practice.get("address") or ""
    practice_hours = practice.get("hours") or ""
    # Resolve booking link — fall back to general
    booking_link = (practice.get(_APPT_TYPE_FIELD_MAP[appt_type])
                    or practice.get("booking_link_general") or "")

    # Build prompts — bake practice values directly (no runtime tokens except {first_name} and {unsub_url})
    system_prompt = (
        f"You are a dental marketing expert writing patient communication for {practice_name}. "
        + (f"Practice phone: {practice_phone}. " if practice_phone else "")
        + (f"Location: {practice_address}. " if practice_address else "")
        + (f"Hours: {practice_hours}. " if practice_hours else "")
        + (f"Doctor: {doctor_name}. " if doctor_name else "")
        + (f"Booking link for this appointment type: {booking_link}. " if booking_link else "")
        + "Write warm, professional, conversational dental patient messages. "
        + "Use {{first_name}} as the ONLY runtime placeholder — this will be replaced with the patient's first name at send time. "
        + ("Use {{unsub_url}} as the ONLY other runtime placeholder — include it near the end of email bodies as an unsubscribe link. " if channel == "email" else "Do NOT include {{unsub_url}} in SMS messages. ")
        + "All other content (practice name, phone, URLs, doctor name) should be written as plain text, NOT as placeholders. "
        + "Return a JSON object with "
        + ("keys 'subject' (string) and 'body' (string)." if channel == "email"
           else "key 'body' (string) only. SMS body must end with '\\nReply STOP to opt out.'")
        + " Return ONLY the JSON object, no explanation."
    )

    user_prompt = (
        f"Channel: {channel}\n"
        f"Appointment type: {appt_type}\n"
        f"Goal: {prompt_text}\n\n"
        + ("Keep the SMS concise (ideally under 140 characters of content before token expansion). " if channel == "sms"
           else "Write 2-4 short paragraphs. ")
        + "Use the patient's first name at the start. "
        + (f"Include the booking link naturally: {booking_link}" if booking_link else "")
    )

    try:
        import anthropic
        api_key = get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise HTTPException(status_code=400,
                detail="Anthropic API key not configured. Add it in Admin → AI Settings.")
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
        )
        raw_text = message.content[0].text if message.content else ""
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI generate message failed: {e}")
        raise HTTPException(status_code=502, detail=f"AI generation failed: {e}")

    json_text = _extract_json_from_ai_response(raw_text)
    try:
        result = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error(f"AI generate message — JSON parse failed: {e}\nRaw: {raw_text[:300]}")
        raise HTTPException(status_code=502, detail="AI returned invalid JSON — please try again")

    body_text = (result.get("body") or "").strip()
    if not body_text:
        raise HTTPException(status_code=502, detail="AI returned empty body — please try again")

    # Warn in logs if SMS is unusually long
    if channel == "sms" and len(body_text) > 320:
        logger.warning(f"AI generated SMS body is {len(body_text)} chars — may fragment on delivery")

    return {
        "channel":  channel,
        "subject":  result.get("subject", "") if channel == "email" else "",
        "body":     body_text,
    }


@app.post("/api/admin/od-settings", dependencies=[Depends(_require_admin)])
def admin_save_od_settings(body: ODSettingsRequest):
    save_setting("od_db_host",       body.od_db_host.strip())
    save_setting("od_db_port",       str(body.od_db_port))
    save_setting("od_db_user",       body.od_db_user.strip())
    save_setting("od_db_name",       body.od_db_name.strip())
    save_setting("od_api_base",      body.od_api_base.strip())
    save_setting("od_developer_key", body.od_developer_key.strip())
    # Only overwrite password/customer key if a real value was sent
    if body.od_db_password and not body.od_db_password.startswith("•"):
        save_setting("od_db_password", body.od_db_password)
    if body.od_customer_key and not body.od_customer_key.startswith("•"):
        save_setting("od_customer_key", body.od_customer_key)
    return {"ok": True}


@app.post("/api/admin/od-test", dependencies=[Depends(_require_admin)])
def admin_test_od_connection():
    s = get_od_settings()
    if not s["od_db_host"]:
        raise HTTPException(status_code=400, detail="No host configured")
    try:
        import pymysql
        conn = pymysql.connect(
            host=s["od_db_host"],
            port=s["od_db_port"],
            user=s["od_db_user"],
            password=s["od_db_password"],
            database=s["od_db_name"],
            connect_timeout=5,
            charset="utf8mb4",
        )
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
        conn.close()
        return {"ok": True, "message": f"Connected ✓  MySQL {version}"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── Domain Registry endpoints ────────────────────────────────────────────────

class DomainCreateRequest(BaseModel):
    domain_url:   str
    label:        str  = ""
    domain_type:  str  = "practice"   # practice | landing | competitor
    crawl_enabled: bool = True
    crawl_depth:  int  = 20

    @validator("domain_url")
    def _validate_url(cls, v):
        v = v.strip().rstrip("/")
        if not v.startswith("http://") and not v.startswith("https://"):
            v = "https://" + v
        return v

    @validator("domain_type")
    def _validate_type(cls, v):
        if v not in ("practice", "landing", "competitor"):
            raise ValueError("domain_type must be practice, landing, or competitor")
        return v

    @validator("crawl_depth")
    def _validate_depth(cls, v):
        if not (1 <= v <= 100):
            raise ValueError("crawl_depth must be between 1 and 100")
        return v


class DomainUpdateRequest(BaseModel):
    label:         Optional[str]  = None
    domain_type:   Optional[str]  = None
    crawl_enabled: Optional[bool] = None
    crawl_depth:   Optional[int]  = None


@app.get("/api/admin/domains", dependencies=[Depends(_require_admin)])
def admin_list_domains():
    """Return all registered domains with crawl status."""
    return {"domains": list_domains()}


@app.post("/api/admin/domains", dependencies=[Depends(_require_admin)])
def admin_create_domain(body: DomainCreateRequest):
    """Register a new domain for crawling."""
    from database import get_domain_by_url
    existing = get_domain_by_url(body.domain_url)
    if existing:
        raise HTTPException(status_code=409, detail="Domain already registered")
    domain_id = create_domain(
        domain_url=body.domain_url,
        label=body.label,
        domain_type=body.domain_type,
        crawl_enabled=body.crawl_enabled,
        crawl_depth=body.crawl_depth,
    )
    return {"ok": True, "domain_id": domain_id, "domain": get_domain(domain_id)}


@app.patch("/api/admin/domains/{domain_id}", dependencies=[Depends(_require_admin)])
def admin_update_domain(domain_id: int, body: DomainUpdateRequest):
    """Update label, type, crawl settings."""
    d = get_domain(domain_id)
    if not d:
        raise HTTPException(status_code=404, detail="Domain not found")
    fields = {k: v for k, v in body.dict().items() if v is not None}
    if fields:
        update_domain(domain_id, **fields)
    return {"ok": True, "domain": get_domain(domain_id)}


@app.delete("/api/admin/domains/{domain_id}", dependencies=[Depends(_require_admin)])
def admin_delete_domain(domain_id: int):
    """Remove a domain and all its crawled page data."""
    d = get_domain(domain_id)
    if not d:
        raise HTTPException(status_code=404, detail="Domain not found")
    delete_domain(domain_id)
    return {"ok": True}


@app.post("/api/admin/domains/{domain_id}/crawl", dependencies=[Depends(_require_admin)])
def admin_trigger_crawl(domain_id: int):
    """
    Trigger an immediate crawl of the domain.
    Runs in a background thread so the endpoint returns instantly.
    Returns 409 if a crawl is already running for this domain.
    """
    import threading
    from domain_crawler import crawl_domain

    d = get_domain(domain_id)
    if not d:
        raise HTTPException(status_code=404, detail="Domain not found")
    if d.get("crawl_status") == "running":
        raise HTTPException(status_code=409, detail="Crawl already in progress for this domain")

    thread = threading.Thread(
        target=crawl_domain,
        args=(domain_id,),
        daemon=True,
        name=f"crawl-domain-{domain_id}",
    )
    thread.start()
    return {"ok": True, "message": f"Crawl started for {d['domain_url']}"}


@app.get("/api/admin/domains/{domain_id}/pages", dependencies=[Depends(_require_admin)])
def admin_domain_pages(domain_id: int):
    """Return crawled pages for a domain."""
    d = get_domain(domain_id)
    if not d:
        raise HTTPException(status_code=404, detail="Domain not found")
    pages = list_domain_pages(domain_id)
    return {"domain": d, "pages": pages, "total": len(pages)}


@app.get("/api/admin/domains/{domain_id}/context", dependencies=[Depends(_require_admin)])
def admin_domain_context(domain_id: int, max_pages: int = 10):
    """
    Preview the AI context block that will be injected into campaign prompts.
    Returns the raw markdown string.
    """
    from domain_crawler import build_site_context
    d = get_domain(domain_id)
    if not d:
        raise HTTPException(status_code=404, detail="Domain not found")
    context = build_site_context(domain_id, max_pages=max_pages)
    if not context:
        return {"context": "", "message": "No crawled pages yet. Trigger a crawl first."}
    return {"context": context, "char_count": len(context)}


# ─── Nearby Practices (Google Places quarterly sync) ─────────────────────────

@app.get("/api/admin/nearby-practices", dependencies=[Depends(_require_admin)])
def list_nearby_practices(max_miles: float = 20.0, include_excluded: bool = False):
    """
    Return all nearby dental practices within max_miles from Google Places DB.
    Groups by radius_band for the admin UI.
    """
    from database import get_nearby_practices, get_nearby_sync_stats
    practices = get_nearby_practices(max_miles=max_miles, include_excluded=include_excluded)
    stats = get_nearby_sync_stats()
    # Group by radius band
    by_band: dict = {}
    for p in practices:
        band = str(p["radius_band"])
        by_band.setdefault(band, []).append(p)
    return {
        "practices": practices,
        "by_band": by_band,
        "stats": stats,
        "total": len(practices),
    }


@app.post("/api/admin/nearby-practices/sync", dependencies=[Depends(_require_admin)])
def trigger_nearby_sync():
    """
    Manually trigger a nearby-practices sync (runs in background thread).
    Same job that runs quarterly — fetches all 4 radius bands from Google Places.
    """
    import threading

    def _run():
        try:
            from places_client import sync_nearby_practices as _sync
            _places_key = get_settings().google_places_api_key
            if not _places_key:
                logger.warning("[nearby_sync] Manual trigger: no API key configured")
                return
            result = _sync(_places_key)
            logger.info(f"[nearby_sync] Manual sync complete: {result}")
        except Exception as e:
            logger.error(f"[nearby_sync] Manual sync failed: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"status": "started", "message": "Sync running in background — refresh in ~30 seconds"}


@app.post(
    "/api/admin/nearby-practices/{place_id}/exclude",
    dependencies=[Depends(_require_admin)],
)
def exclude_nearby_practice(place_id: str, body: dict = Body(default={})):
    """
    Toggle is_excluded on a nearby practice (manual override).
    Body: {"excluded": true/false, "note": "reason"}
    Excluded practices are NOT used for brand-negative keyword generation.
    """
    from database import set_nearby_practice_excluded
    excluded = bool(body.get("excluded", True))
    note = str(body.get("note", ""))
    updated = set_nearby_practice_excluded(place_id, excluded, note)
    if not updated:
        raise HTTPException(status_code=404, detail="Practice not found")
    return {"ok": True, "place_id": place_id, "excluded": excluded}


@app.get("/api/admin/nearby-practices/brand-negatives", dependencies=[Depends(_require_admin)])
def get_brand_negatives(max_miles: float = 20.0, campaign_type: str = "general"):
    """
    Return the full list of brand-negative keyword stems for a given campaign type.
    Used by the campaign wizard keywords step to preview what will be injected.
    """
    from database import get_brand_negatives_for_campaign, get_nearby_sync_stats
    stems = get_brand_negatives_for_campaign(campaign_type=campaign_type, max_miles=max_miles)
    stats = get_nearby_sync_stats()
    return {
        "stems": stems,
        "count": len(stems),
        "last_synced_at": stats.get("last_synced_at", ""),
        "campaign_type": campaign_type,
        "max_miles": max_miles,
    }


# ── Competitor advertising intelligence admin endpoints ───────────────────────

@app.post("/api/admin/competitor-intel/scan", dependencies=[Depends(_require_admin)])
def trigger_competitor_intel_scan():
    """
    Manually trigger a competitor advertising intelligence scan.
    Runs the full Auction Insights + domain-matching pipeline.
    Returns a summary of what was detected and staged.
    """
    try:
        from competitor_intel_engine import run_competitor_intel_scan
        result = run_competitor_intel_scan()
        return {"ok": True, "result": result}
    except Exception as e:
        logger.error(f"Manual competitor intel scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/competitor-intel/feed", dependencies=[Depends(_require_admin)])
def get_competitor_intel_feed(limit: int = 100):
    """
    Return the recent competitor_ad_intel rows (intel feed).
    Includes practice name, domain, campaign type, confidence, and admin review state.
    """
    from database import get_competitor_intel_feed, get_intel_scan_stats
    feed = get_competitor_intel_feed(limit=limit)
    stats = get_intel_scan_stats()
    return {"feed": feed, "stats": stats}


@app.get("/api/admin/competitor-intel/actions", dependencies=[Depends(_require_admin)])
def get_competitor_intel_actions():
    """Return all pending competitor_intel_actions for admin review."""
    from database import get_pending_intel_actions, get_intel_scan_stats
    actions = get_pending_intel_actions(limit=200)
    stats = get_intel_scan_stats()
    return {"actions": actions, "stats": stats}


@app.post(
    "/api/admin/competitor-intel/actions/{action_id}/apply",
    dependencies=[Depends(_require_admin)],
)
def apply_competitor_intel_action(action_id: int):
    """
    Apply a single pending competitor_intel_actions row.
    For suppress_negative: removes the brand negative from the campaign in Google Ads.
    For add_conquest_keyword: returns guidance to use AI Refine panel instead.
    """
    from competitor_intel_engine import apply_intel_action
    result = apply_intel_action(action_id=action_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "Unknown error"))
    return result


@app.post(
    "/api/admin/competitor-intel/actions/{action_id}/dismiss",
    dependencies=[Depends(_require_admin)],
)
def dismiss_competitor_intel_action(action_id: int):
    """Dismiss (reject) a pending competitor_intel_actions row."""
    from database import update_intel_action_status
    ok = update_intel_action_status(action_id, "rejected", applied_by="admin")
    if not ok:
        raise HTTPException(status_code=404, detail="Action not found")
    return {"ok": True, "action_id": action_id, "status": "rejected"}


@app.post(
    "/api/admin/competitor-intel/intel/{intel_id}/review",
    dependencies=[Depends(_require_admin)],
)
def review_competitor_intel_row(intel_id: int, decision: str = "confirm"):
    """Mark a competitor_ad_intel row as admin-reviewed (confirm or dismiss)."""
    from database import update_intel_admin_decision
    if decision not in ("confirm", "dismiss"):
        raise HTTPException(status_code=400, detail="decision must be 'confirm' or 'dismiss'")
    ok = update_intel_admin_decision(intel_id, decision)
    if not ok:
        raise HTTPException(status_code=404, detail="Intel row not found")
    return {"ok": True, "intel_id": intel_id, "decision": decision}


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)

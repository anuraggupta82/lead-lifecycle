"""
Optimizer Memory — persistent cross-run memory for the AI Campaign Optimizer.

Stores each run's analysis, search terms seen, recommendations made, and
approval/rejection decisions. Injected into Claude's context each run so it
builds on prior analysis rather than starting fresh every time.

File: backend/optimizer_memory.json  (JSON, human-readable, atomic writes)

Lifecycle:
  - First run: fetches last 30 days of search terms, seeds the memory file.
  - Subsequent runs: fetches only since last run date (incremental).
  - Every 30 days (or 30+ raw run entries): consolidates old runs into
    consolidated_summary, drops raw data older than 30 days.
"""

import fcntl
import json
import logging
import os
import tempfile
from datetime import date, datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Default path — relative to this file's directory (backend/)
_DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "optimizer_memory.json")

_EMPTY_SCHEMA = {
    "version": 1,
    "created_at": None,
    "last_consolidated_at": None,
    "consolidated_summary": {
        "period_start": None,
        "period_end": None,
        "total_runs": 0,
        "themes": [],
        "winning_keywords": [],
        "losing_keywords": [],
        "negatives_added": [],
        "approval_rate": 0.0,
        "cum_total_recs": 0,
        "cum_approved_recs": 0,
        "rejected_patterns": [],
        "lessons_learned": [],
    },
    "runs": [],
}


class MemoryStore:
    """
    Persistent cross-run memory for the AI optimizer.

    Usage:
        memory = MemoryStore()
        last_date = memory.get_last_run_date()   # None on first run
        digest = memory.build_digest()
        # ... run optimizer ...
        memory.append_run({...})
        memory.save()
    """

    def __init__(self, path: str = _DEFAULT_PATH):
        self.path = path
        self._data: Optional[dict] = None

    # ── Load / Save ─────────────────────────────────────────────────────────

    def load(self) -> dict:
        """Load memory from disk. Returns empty schema if file doesn't exist."""
        if self._data is not None:
            return self._data
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                logger.info(f"Optimizer memory loaded: {len(self._data.get('runs', []))} runs, "
                            f"consolidated={self._data.get('last_consolidated_at', 'never')}")
            except Exception as e:
                logger.warning(f"Could not load optimizer memory ({e}), starting fresh")
                self._data = self._fresh()
        else:
            logger.info("Optimizer memory file not found — first run, will create")
            self._data = self._fresh()
        return self._data

    def save(self) -> None:
        """Atomic write: write to tmp file then rename to avoid corruption."""
        if self._data is None:
            return
        try:
            dir_ = os.path.dirname(self.path) or "."
            fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2, default=str)
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                raise
            logger.info(f"Optimizer memory saved ({len(self._data.get('runs', []))} runs)")
        except Exception as e:
            logger.error(f"Failed to save optimizer memory: {e}")

    # ── Core API ─────────────────────────────────────────────────────────────

    def get_last_run_date(self) -> Optional[date]:
        """
        Return the date of the most recent run entry, or None if no runs yet.
        Used to compute the search terms lookback window:
          - None → use last 30 days (first run, seed the memory)
          - date → fetch from that date forward (incremental)
        """
        data = self.load()
        runs = data.get("runs", [])
        if not runs:
            return None
        dates = []
        for r in runs:
            d = r.get("run_date")
            if d:
                try:
                    dates.append(date.fromisoformat(str(d)[:10]))
                except Exception:
                    pass
        return max(dates) if dates else None

    def append_run(self, run_entry: dict) -> None:
        """
        Append a run record to memory. Triggers consolidation if needed.

        run_entry keys:
          run_id, run_date (YYYY-MM-DD), trigger, summary (dict),
          top_search_terms (list), negatives_added (list of str),
          recommendations (list of {rec_id, type, target, rationale, status, decided_at}),
          claude_notes (str)
        """
        data = self.load()
        # Ensure required keys exist
        if "created_at" not in data or not data["created_at"]:
            data["created_at"] = datetime.now(timezone.utc).isoformat()
        data.setdefault("runs", [])
        data["runs"].append(run_entry)

        # Consolidate if needed before saving
        if self._should_consolidate(data):
            self._consolidate(data)

    def update_rec_status(self, run_id: str, rec_id: str, status: str,
                          decided_at: Optional[str] = None) -> None:
        """
        Update a recommendation's status in the memory file.
        Called when user approves/rejects in the UI, or when execution succeeds/fails.
        status: "approved" | "rejected" | "executed" | "errored"

        Uses a file lock to prevent lost updates from concurrent approve/reject clicks.
        """
        decided_at = decided_at or datetime.now(timezone.utc).isoformat()
        lock_path = self.path + ".lock"
        try:
            with open(lock_path, "w") as _lf:
                fcntl.flock(_lf, fcntl.LOCK_EX)
                # Re-load from disk inside the lock to get the freshest state
                self._data = None
                data = self.load()
                updated = False
                for run in data.get("runs", []):
                    if run.get("run_id") != run_id:
                        continue
                    for rec in run.get("recommendations", []):
                        if rec.get("rec_id") == rec_id:
                            rec["status"] = status
                            rec["decided_at"] = decided_at
                            updated = True
                            break
                    if updated:
                        break
                if updated:
                    self.save()
                else:
                    logger.info(f"update_rec_status: rec_id={rec_id[:8]} not found in run={run_id[:8]} (may have been consolidated)")
        except Exception as e:
            logger.warning(f"update_rec_status failed for rec_id={rec_id[:8]}: {e}")

    def build_digest(self, max_runs: int = 10) -> dict:
        """
        Build a compact digest for injection into Claude's context.
        Keeps token count low by summarising rather than dumping raw data.

        Returns:
          {
            "consolidated_summary": {...},   # long-term patterns
            "recent_runs": [...],            # last max_runs entries (trimmed)
            "approval_rate": float,
            "rejected_patterns": [...],      # terms/patterns the user has rejected
            "negatives_history": [...],      # all negatives ever added (deduped)
            "lessons_learned": [...],
          }
        """
        data = self.load()
        runs = data.get("runs", [])
        recent = runs[-max_runs:] if len(runs) > max_runs else runs

        # Slim down recent runs — only fields Claude needs
        slim_runs = []
        for r in recent:
            slim_runs.append({
                "run_date": r.get("run_date"),
                "trigger": r.get("trigger"),
                "summary": r.get("summary", {}),
                "negatives_added": r.get("negatives_added", []),
                "claude_notes": (r.get("claude_notes") or "")[:500],
                "recommendations": [
                    {
                        "type": rec.get("type"),
                        "target": rec.get("target"),
                        "status": rec.get("status"),
                        "rationale": (rec.get("rationale") or "")[:200],
                    }
                    for rec in r.get("recommendations", [])
                ],
            })

        # Collect all approved negatives (deduplicated)
        neg_history: set = set()
        for r in runs:
            for n in r.get("negatives_added", []):
                if isinstance(n, str):
                    neg_history.add(n.lower().strip())
                elif isinstance(n, dict):
                    neg_history.add(n.get("keyword_text", "").lower().strip())

        # Collect rejected patterns
        rejected: set = set()
        for r in runs:
            for rec in r.get("recommendations", []):
                if rec.get("status") == "rejected":
                    t = rec.get("target", "")
                    if t:
                        rejected.add(str(t).lower().strip())

        # Approval rate across all runs
        total_recs = sum(len(r.get("recommendations", [])) for r in runs)
        approved_recs = sum(
            1 for r in runs
            for rec in r.get("recommendations", [])
            if rec.get("status") in ("approved", "executed")
        )
        approval_rate = round(approved_recs / total_recs, 2) if total_recs > 0 else 0.0

        cs = data.get("consolidated_summary", {})

        return {
            "consolidated_summary": cs,
            "recent_runs": slim_runs,
            "approval_rate": approval_rate,
            "rejected_patterns": sorted(rejected)[:100],
            "negatives_history": sorted(neg_history)[:300],
            "lessons_learned": cs.get("lessons_learned", []),
        }

    # ── Consolidation ────────────────────────────────────────────────────────

    def _should_consolidate(self, data: dict) -> bool:
        """Consolidate if 30+ raw run entries OR oldest raw run is >30 days old."""
        runs = data.get("runs", [])
        if len(runs) >= 30:
            return True
        if not runs:
            return False
        try:
            oldest = min(date.fromisoformat(str(r["run_date"])[:10])
                        for r in runs if r.get("run_date"))
            return (date.today() - oldest).days > 30
        except Exception:
            return False

    def _consolidate(self, data: dict) -> None:
        """
        Fold runs older than 30 days into consolidated_summary.
        Keeps recent runs intact; discards raw search-term noise from old runs.
        """
        cutoff = date.today() - timedelta(days=30)
        old_runs = []
        keep_runs = []
        for r in data.get("runs", []):
            try:
                rd = date.fromisoformat(str(r.get("run_date", ""))[:10])
                if rd < cutoff:
                    old_runs.append(r)
                else:
                    keep_runs.append(r)
            except Exception:
                keep_runs.append(r)

        if not old_runs:
            return

        cs = data.setdefault("consolidated_summary", _EMPTY_SCHEMA["consolidated_summary"].copy())

        # Extend negatives_added (deduped)
        existing_negs = set(cs.get("negatives_added", []))
        for r in old_runs:
            for n in r.get("negatives_added", []):
                kw = (n if isinstance(n, str) else n.get("keyword_text", "")).lower().strip()
                if kw:
                    existing_negs.add(kw)
        cs["negatives_added"] = sorted(existing_negs)

        # Extend rejected_patterns (deduped)
        existing_rej = set(cs.get("rejected_patterns", []))
        for r in old_runs:
            for rec in r.get("recommendations", []):
                if rec.get("status") == "rejected":
                    t = str(rec.get("target", "")).lower().strip()
                    if t:
                        existing_rej.add(t)
        cs["rejected_patterns"] = sorted(existing_rej)

        # Accumulate approval rate using cumulative rec counters (correct blending)
        old_total = sum(len(r.get("recommendations", [])) for r in old_runs)
        old_approved = sum(
            1 for r in old_runs
            for rec in r.get("recommendations", [])
            if rec.get("status") in ("approved", "executed")
        )
        cum_total = cs.get("cum_total_recs", 0) + old_total
        cum_approved = cs.get("cum_approved_recs", 0) + old_approved
        cs["cum_total_recs"] = cum_total
        cs["cum_approved_recs"] = cum_approved
        cs["approval_rate"] = round(cum_approved / cum_total, 2) if cum_total > 0 else 0.0

        # Collect claude_notes as lessons_learned (keep last 20 unique non-empty lines)
        existing_lessons = set(cs.get("lessons_learned", []))
        for r in old_runs:
            notes = r.get("claude_notes", "")
            if notes:
                for line in notes.split("\n"):
                    line = line.strip()
                    if line and len(line) > 20:
                        existing_lessons.add(line)
        cs["lessons_learned"] = sorted(existing_lessons)[:20]

        # Update period and counts
        if old_runs:
            dates = [r.get("run_date", "") for r in old_runs if r.get("run_date")]
            if dates:
                if not cs.get("period_start"):
                    cs["period_start"] = min(dates)
                cs["period_end"] = max(dates)
        cs["total_runs"] = cs.get("total_runs", 0) + len(old_runs)

        data["runs"] = keep_runs
        data["last_consolidated_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(f"Memory consolidated: folded {len(old_runs)} old runs, "
                    f"{len(keep_runs)} runs remain in window")

    # ── Internal ─────────────────────────────────────────────────────────────

    def _fresh(self) -> dict:
        import copy
        schema = copy.deepcopy(_EMPTY_SCHEMA)
        schema["created_at"] = datetime.now(timezone.utc).isoformat()
        return schema

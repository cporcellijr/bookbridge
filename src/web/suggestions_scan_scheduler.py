"""C29 helpers extracted from web_server with no behavior change."""

from importlib import import_module
from typing import Optional
from datetime import datetime

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


SUGGESTIONS_AUTO_SCAN_STATE_KEY = "SUGGESTIONS_AUTO_SCAN_STATE"


SUGGESTIONS_AUTO_SCAN_MIN_MINUTES = 5


_SUGGESTIONS_AUTO_SCAN_JOB = {}


_CRON_FIELDS = (
    (0, 59, {}),
    (0, 23, {}),
    (1, 31, {}),
    (1, 12, {name: i for i, name in enumerate(
        ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'), start=1)}),
    (0, 7, {name: i for i, name in enumerate(('sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'))}),
)


_SUGGESTIONS_WEEKDAYS = ('sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday')


def parse_cron_expression(expression: str):
    """Parse a five-field cron expression into the set of values each field allows.

    Supports `*`, numbers, `a-b` ranges, `/step`, comma lists and three-letter month and
    weekday names. Day of week accepts 0-7, where 0 and 7 are both Sunday. Raises
    ValueError for anything else.
    """
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(f"expected 5 fields, got {len(fields)}")
    allowed = []
    for text, (low, high, names) in zip(fields, _CRON_FIELDS):
        values = set()
        for part in text.lower().split(','):
            span, _, step = part.partition('/')
            if span == '*':
                start, end = low, high
            else:
                first, _, last = span.partition('-')
                start = names[first] if first in names else int(first)
                end = (names[last] if last in names else int(last)) if last else (high if step else start)
            stride = int(step) if step else 1
            if not low <= start <= end <= high or stride < 1:
                raise ValueError(f"'{part}' is outside {low}-{high}")
            values.update(range(start, end + 1, stride))
        allowed.append(values)
    if 7 in allowed[4]:
        allowed[4].add(0)
    # As in cronie, a field starting with '*' (including '*/n') leaves its day unrestricted.
    return allowed, not fields[2].startswith('*'), not fields[4].startswith('*')


def cron_matches(cron, when) -> bool:
    """Whether a parsed cron expression fires at `when`, to the minute.

    As in standard cron, when both day of month and day of week are restricted, a day
    matching either one fires.
    """
    (minutes, hours, days, months, weekdays), day_restricted, weekday_restricted = cron
    if when.minute not in minutes or when.hour not in hours or when.month not in months:
        return False
    day_ok = when.day in days
    weekday_ok = when.isoweekday() % 7 in weekdays
    if day_restricted and weekday_restricted:
        return day_ok or weekday_ok
    return day_ok and weekday_ok


def _suggestions_full_refresh_schedule():
    web = _web_server()
    if web.env_truthy('SUGGESTIONS_FULL_REFRESH_ENABLED'):
        expression = (web.os.environ.get('SUGGESTIONS_FULL_REFRESH_CRON') or '').strip()
        return ('SUGGESTIONS_FULL_REFRESH_CRON', expression) if expression else (None, None)
    day = (web.os.environ.get('SUGGESTIONS_FULL_REFRESH_DAY') or 'off').strip().lower()
    if day not in _SUGGESTIONS_WEEKDAYS:
        return None, None
    raw_time = (web.os.environ.get('SUGGESTIONS_FULL_REFRESH_TIME') or '04:00').strip()
    try:
        refresh_time = web.datetime.strptime(raw_time, '%H:%M').time()
    except ValueError:
        return 'SUGGESTIONS_FULL_REFRESH_TIME', raw_time
    return 'SUGGESTIONS_FULL_REFRESH_DAY', f"{refresh_time.minute} {refresh_time.hour} * * {day[:3]}"


def _suggestions_full_refresh_due(now, state) -> Optional[str]:
    """The cron fire a full refresh is owed for, as an ISO string, or None.

    The latest fire within the past day stays owed until a refresh has run for it, so a
    fire missed while BookBridge was down still runs once it is back.
    """
    web = _web_server()
    source, expression = _suggestions_full_refresh_schedule()
    if not expression:
        return None
    try:
        cron = parse_cron_expression(expression)
    except ValueError:
        web.get_persistent_condition_logger().warn(
            web.logger,
            "suggestions-full-refresh-schedule",
            "⚠️ %s '%s' is not a valid schedule — full refresh is off",
            source,
            web.sanitize_log_data(expression),
            exc_info=True,
        )
        return None
    web.get_persistent_condition_logger().resolve(
        web.logger, "suggestions-full-refresh-schedule", "Full refresh schedule is valid again",
    )
    # Step back through real instants (UTC) so the repeated hour of a fall-back change is
    # walked in order instead of being skipped by wall-clock arithmetic.
    fire = now.replace(second=0, microsecond=0)
    instant = fire.astimezone(web.timezone.utc) if fire.tzinfo else None
    for _ in range(24 * 60 + 1):
        if cron_matches(cron, fire):
            return None if _suggestions_full_refresh_done(fire, state) else fire.isoformat()
        if instant is None:
            fire -= web.timedelta(minutes=1)
        else:
            instant -= web.timedelta(minutes=1)
            fire = instant.astimezone(fire.tzinfo)
    return None


def _suggestions_full_refresh_done(fire: datetime, state: dict) -> bool:
    """Whether a full refresh already covers the cron fire at `fire`.

    A recorded fire covers every fire at or before it, and also the same wall-clock time
    repeated by a fall-back change, so one schedule slot never runs twice.
    """
    web = _web_server()
    if state.get("last_full_date") == fire.date().isoformat():
        return True
    try:
        last = web.datetime.fromisoformat(state.get("last_full_fire") or "")
    except (TypeError, ValueError):
        return False
    if last.tzinfo is None or fire.tzinfo is None:
        return fire.replace(tzinfo=None) <= last.replace(tzinfo=None)
    return fire <= last or fire.replace(tzinfo=None) == last.replace(tzinfo=None)


def _suggestions_auto_scan_due(now, state):
    web = _web_server()
    if _suggestions_full_refresh_due(now, state):
        return 'full'

    try:
        minutes = int(float(web.os.environ.get('SUGGESTIONS_AUTO_SCAN_MINUTES') or 0))
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        return None
    interval_seconds = max(minutes, SUGGESTIONS_AUTO_SCAN_MIN_MINUTES) * 60
    if now.timestamp() - float(state.get("last_finished") or 0) >= interval_seconds:
        return 'incremental'
    return None


def _run_scheduled_suggestions_scan(full: bool):
    web = _web_server()
    from src.utils.user_config import _ALLOW_GLOBAL_FALLBACK_KEY

    user_id = web.database_service._default_user_id()
    if user_id is None:
        return None
    creds = dict(web.database_service.get_user_credentials(user_id) or {})
    creds[_ALLOW_GLOBAL_FALLBACK_KEY] = web._global_fallback_allowed(
        web.database_service, web.database_service.get_user(user_id),
    )

    tok_bundle = web._active_bundle.set(web.container.user_client_registry().get_clients(user_id))
    tok_uid = web.set_current_user_id(user_id)
    tok_creds = web.set_current_user_credentials(creds)
    try:
        if full:
            cached_by_abs, cached_no_match = {}, []
        else:
            persisted = web._load_persisted_suggestions_cache()
            cached_by_abs = persisted.get('scan_cache_by_abs', {}) or {}
            cached_no_match = persisted.get('scan_cache_no_match_abs_ids', []) or []
        return _start_suggestions_scan_job(
            cached_suggestions_by_abs=cached_by_abs,
            cached_no_match_abs_ids=cached_no_match,
            full_refresh=full,
        )
    finally:
        web.reset_current_user_credentials(tok_creds)
        web.reset_current_user_id(tok_uid)
        web._active_bundle.reset(tok_bundle)


def _suggestions_auto_scan_tick():
    web = _web_server()
    if not web.env_truthy('SUGGESTIONS_ENABLED'):
        return
    try:
        state = web.database_service.get_json_setting(SUGGESTIONS_AUTO_SCAN_STATE_KEY, default={})
        if not isinstance(state, dict):
            state = {}

        if _SUGGESTIONS_AUTO_SCAN_JOB:
            job = web._get_suggestions_scan_job(_SUGGESTIONS_AUTO_SCAN_JOB["job_id"])
            if job and job["status"] in web._SCAN_JOB_ACTIVE:
                return
            state["last_finished"] = job["updated_at"] if job else web.time.time()
            if _SUGGESTIONS_AUTO_SCAN_JOB["full_fire"]:
                state["last_full_fire"] = _SUGGESTIONS_AUTO_SCAN_JOB["full_fire"]
                state.pop("last_full_date", None)
            web.database_service.set_json_setting(SUGGESTIONS_AUTO_SCAN_STATE_KEY, state)
            with web.SUGGESTIONS_SCAN_JOBS_LOCK:
                web.SUGGESTIONS_SCAN_JOBS.pop(_SUGGESTIONS_AUTO_SCAN_JOB["job_id"], None)
            _SUGGESTIONS_AUTO_SCAN_JOB.clear()

        now = web.datetime.now(web._get_stats_timezone())
        due = _suggestions_auto_scan_due(now, state)
        with web.SUGGESTIONS_SCAN_JOBS_LOCK:
            scan_running = any(job.get("status") in web._SCAN_JOB_ACTIVE for job in web.SUGGESTIONS_SCAN_JOBS.values())
        if due and not scan_running:
            job_id = _run_scheduled_suggestions_scan(full=(due == 'full'))
            if job_id:
                _SUGGESTIONS_AUTO_SCAN_JOB.update(
                    job_id=job_id,
                    full_fire=_suggestions_full_refresh_due(now, state) if due == 'full' else None,
                )
                web.logger.info("🔎 Scheduled suggestions scan started (%s)", due)
        web.get_persistent_condition_logger().resolve(
            web.logger, "suggestions-auto-scan", "Scheduled suggestions scans resumed",
        )
    except Exception as e:
        web.get_persistent_condition_logger().warn(
            web.logger, "suggestions-auto-scan", "Scheduled suggestions scan tick failed: %s", e, exc_info=True,
        )


def _start_suggestions_scan_job(cached_suggestions_by_abs=None, cached_no_match_abs_ids=None, full_refresh=False):
    web = _web_server()
    web._prune_suggestions_scan_jobs()
    bundle = web._scan_user_clients()
    try:
        user = web.current_user()
        user_id = user.id if user is not None else None
    except Exception:
        user_id = None
    if user_id is None:
        try:
            user_id = web.get_current_user_id()
        except Exception:
            user_id = None
    creds = web.get_current_user_credentials()

    job_id = web.uuid.uuid4().hex
    with web.SUGGESTIONS_SCAN_JOBS_LOCK:
        # Reuse this reader's queued or running scan; checked under the same lock
        # as the insert so two clicks can never start two scans.
        if user_id:
            for existing_id, existing in web.SUGGESTIONS_SCAN_JOBS.items():
                if existing.get('user_id') == user_id and existing.get('status') in web._SCAN_JOB_ACTIVE:
                    web.logger.info(f"Suggestions scan already running; reusing job {existing_id}")
                    return existing_id
        web.SUGGESTIONS_SCAN_JOBS[job_id] = {
            "status": "queued",
            "user_id": user_id,
            "results": {},
            "error": None,
            "progress": {
                "phase": "queued",
                "percent": 0,
                "message": "Waiting for another library scan to finish...",
                "scanned_new_done": 0,
                "scanned_new_total": 0,
                "reused_cached": 0,
                "total_unmatched": 0,
            },
            "started_at": web.time.time(),
            "updated_at": web.time.time(),
        }

    def runner():
        tok_bundle = web._active_bundle.set(bundle)
        tok_uid = web.set_current_user_id(user_id)
        tok_creds = web.set_current_user_credentials(creds)
        try:
            with web.SUGGESTIONS_SCAN_SLOT:
                with web.SUGGESTIONS_SCAN_JOBS_LOCK:
                    job = web.SUGGESTIONS_SCAN_JOBS.get(job_id)
                    if job is None:
                        return
                    job["status"] = "running"
                    job["progress"] = {**job["progress"], "phase": "initializing", "message": "Preparing scan..."}
                    job["updated_at"] = web.time.time()
                web._run_suggestions_scan_job(
                    job_id,
                    cached_suggestions_by_abs or {},
                    cached_no_match_abs_ids or [],
                    full_refresh,
                )
        finally:
            web.reset_current_user_credentials(tok_creds)
            web.reset_current_user_id(tok_uid)
            web._active_bundle.reset(tok_bundle)

    if web._BACKGROUND_TASKS_SYNCHRONOUS:
        runner()
    else:
        web.threading.Thread(target=runner, daemon=True, name="suggestions-scan").start()
    return job_id

"""Private dashboard with selected-date ratings and separately authenticated device uploads."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from sleepscape import __version__
from sleepscape.config import Settings
from sleepscape.domain.dashboard_dates import NightBeforeDisplayStart, visible_start
from sleepscape.domain.morning_feeling import Feeling
from sleepscape.web.device_security import AUDIO_PATH, INGEST_PATH, DeviceGate
from sleepscape.web.security import SECURITY_HEADERS, AccessGate

STATIC_DIR = Path(__file__).parent / "web" / "static"


class RatingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feeling: Feeling


def _day(value: date) -> date:
    if not date(2000, 1, 1) <= value <= date(2100, 12, 31):
        raise HTTPException(422, "date_outside_supported_range")
    return value


def _query(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except NightBeforeDisplayStart:
        raise HTTPException(404, "night_before_dashboard_start") from None
    except Exception as exc:
        logging.getLogger("sleepscape").warning("dashboard_query_failed [%s]", type(exc).__name__)
        raise HTTPException(503, "local_data_unavailable") from None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings()
    gate = AccessGate(settings)
    device_gate = DeviceGate(settings)
    from sleepscape.storage import event_audio

    async def expire_audio():
        while True:
            try:
                await run_in_threadpool(event_audio.expire, settings.data_dir)
            except Exception:
                logging.getLogger("sleepscape").warning("audio_expiry_unavailable")
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(app):
        cleaner = asyncio.create_task(expire_audio())
        try:
            yield
        finally:
            cleaner.cancel()
            with suppress(asyncio.CancelledError):
                await cleaner

    app = FastAPI(
        lifespan=lifespan,
        title="SleepScape",
        version=__version__,
        # An optional JSON schema is enough for local debugging; CDN-based
        # Swagger assets would violate the dashboard's same-origin policy.
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.debug_api_docs else None,
    )
    app.state.settings = settings

    @app.middleware("http")
    async def private_read_only_access(request, call_next):
        if request.method == "POST" and request.url.path in {INGEST_PATH, AUDIO_PATH}:
            response = await device_gate.check(request)
        else:
            response = await gate.check(request)
        if response is None:
            response = await call_next(request)
        gate.attach_session(request, response)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"error": "invalid_parameters"}, status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(
            {"error": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers
        )

    def service():
        from sleepscape.services.views import DashboardService

        return DashboardService(settings)

    @app.post(AUDIO_PATH)
    async def receive_audio(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > event_audio.MAX_BODY:
                raise HTTPException(413, "audio_too_large")
            body.extend(chunk)
        try:
            await run_in_threadpool(
                event_audio.stage,
                settings.data_dir,
                request.state.environment_device_id,
                bytes(body),
                time.time(),
            )
        except ValueError:
            raise HTTPException(422, "audio_rejected") from None
        except Exception:
            raise HTTPException(503, "audio_storage_unavailable") from None
        return {"status": "accepted"}

    @app.get("/api/v1/event-audio/{ref}")
    def audio(ref: str):
        from sleepscape.services.event_audio import playback

        value = _query(playback, settings, ref)
        if value is None:
            raise HTTPException(404, "audio_unavailable")
        return Response(value, media_type="audio/wav")

    @app.post(INGEST_PATH)
    async def environment_batch(request: Request):
        from sleepscape.domain.environment import MAX_BATCH_BYTES
        from sleepscape.services.environment import parse_batch, receive
        from sleepscape.storage.environment import EnvironmentError

        length = request.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdecimal():
                raise HTTPException(400, "invalid_content_length")
            if len(length) > 10 or int(length) > MAX_BATCH_BYTES:
                raise HTTPException(413, "environment_body_too_large")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BATCH_BYTES:
                raise HTTPException(413, "environment_body_too_large")
            body.extend(chunk)
        try:
            batch = parse_batch(bytes(body))
            return await run_in_threadpool(
                receive, settings, request.state.environment_device_id, batch
            )
        except EnvironmentError as exc:
            headers = {"Retry-After": "60"} if exc.status == 503 else None
            raise HTTPException(exc.status, str(exc), headers=headers) from None
        except Exception as exc:
            logging.getLogger("sleepscape").warning(
                "environment_ingest_failed [%s]", type(exc).__name__
            )
            raise HTTPException(
                503, "environment_storage_unavailable", headers={"Retry-After": "60"}
            ) from None

    @app.get("/sleep", include_in_schema=False)
    @app.get("/sleep/", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/v1/status")
    def status(navigation: bool = False):
        view = service()
        return {
            **_query(view.navigation_status if navigation else view.status),
            "demo": settings.demo,
        }

    @app.get("/api/v1/metrics")
    def metrics():
        from sleepscape.domain.metrics import metric_registry

        return metric_registry()

    @app.get("/api/v1/environment/timeline")
    def environment_timeline_view(
        start: Annotated[datetime, Query(alias="from")],
        end: Annotated[datetime, Query(alias="to")],
        max_points: Annotated[int, Query(ge=2, le=2000)] = 2000,
    ):
        from sleepscape.services.environment_dashboard import environment_timeline

        if start.tzinfo is None or end.tzinfo is None:
            raise HTTPException(422, "timeline_requires_explicit_timezone")
        if not timedelta(0) < end - start <= timedelta(hours=48):
            raise HTTPException(422, "timeline_window_requires_up_to_48_hours")
        _day(start.date())
        _day(end.date())
        return {
            "contract_version": "1",
            "timezone": settings.timezone,
            "window": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "default_start": start.isoformat(),
                "default_end": end.isoformat(),
            },
            "stages": [],
            "quality": ["environment_window_not_sleep"],
            **_query(environment_timeline, settings, start, end, max_points),
        }

    @app.get("/api/v1/nights")
    def nights(
        start: date | None = None,
        end: date | None = None,
        cursor: date | None = None,
        limit: Annotated[int, Query(ge=1, le=92)] = 31,
        include_review: bool = False,
    ):
        view = service()
        if end is None:
            latest = _query(view.status).get("latest_night")
            end = (
                date.fromisoformat(latest)
                if latest
                else visible_start(
                    datetime.now(ZoneInfo(settings.timezone)).date(), settings.dashboard_start_date
                )
            )
        start = start or end - timedelta(days=29)
        _day(start)
        _day(end)
        if start > end or (end - start).days >= 92:
            raise HTTPException(422, "history_window_requires_1_to_92_days")
        if cursor is not None and not start <= cursor <= end:
            raise HTTPException(422, "cursor_outside_window")
        return _query(
            view.nights, start, end, cursor=cursor, limit=limit, include_review=include_review
        )

    @app.get("/api/v1/nights/{day}")
    def night(day: date):
        result = _query(service().night, _day(day))
        if result is None:
            raise HTTPException(404, "night_not_found")
        return result

    @app.get("/api/v1/nights/{day}/timeline")
    def timeline(
        day: date,
        start: Annotated[datetime | None, Query(alias="from")] = None,
        end: Annotated[datetime | None, Query(alias="to")] = None,
        max_points: Annotated[int, Query(ge=2, le=2000)] = 2000,
    ):
        _day(day)
        if (start is None) != (end is None):
            raise HTTPException(422, "timeline_requires_both_bounds")
        if start is not None and end is not None:
            if start.tzinfo is None or end.tzinfo is None:
                raise HTTPException(422, "timeline_requires_explicit_timezone")
            if not timedelta(0) < end - start <= timedelta(hours=48):
                raise HTTPException(422, "timeline_window_requires_up_to_48_hours")
            _day(start.date())
            _day(end.date())
        result = _query(service().timeline, day, start=start, end=end, max_points=max_points)
        if result is None:
            raise HTTPException(404, "night_not_found")
        return result

    @app.get("/api/v1/nights/{day}/morning-feeling")
    def read_morning_feeling(day: date):
        from sleepscape.services.morning_feeling import get

        return _query(get, settings, _day(day))

    @app.put("/api/v1/nights/{day}/morning-feeling")
    def set_morning_feeling(day: date, body: RatingInput):
        from sleepscape.services.morning_feeling import put

        return _query(put, settings, _day(day), body.feeling)

    @app.get("/api/v1/nights/{day}/analysis")
    def analysis(day: date):
        from sleepscape.services.analysis_views import analysis_view

        return _query(analysis_view, settings, _day(day))

    @app.get("/api/v1/sleep-reviews/{day}")
    def weekly_sleep_review(day: date):
        from sleepscape.services.weekly_review import review_view

        return _query(review_view, settings, _day(day))

    @app.get("/health", tags=["service"])
    def health() -> dict[str, str]:
        """Process liveness only; this does not assert data-source readiness."""
        return {"status": "ok", "service": "SleepScape", "version": __version__}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app

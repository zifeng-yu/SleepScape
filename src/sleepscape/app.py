"""Read-only dashboard and bounded data APIs; no collection during HTTP requests."""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from sleepscape import __version__
from sleepscape.config import Settings
from sleepscape.web.security import SECURITY_HEADERS, AccessGate

STATIC_DIR = Path(__file__).parent / "web" / "static"


def _day(value: date) -> date:
    if not date(2000, 1, 1) <= value <= date(2100, 12, 31):
        raise HTTPException(422, "date_outside_supported_range")
    return value


def _query(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except Exception as exc:
        logging.getLogger("sleepscape").warning("dashboard_query_failed [%s]", type(exc).__name__)
        raise HTTPException(503, "local_data_unavailable") from None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings()
    gate = AccessGate(settings)
    app = FastAPI(
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
        response = await gate.check(request)
        if response is None:
            response = await call_next(request)
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

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/v1/status")
    def status():
        return {**_query(service().status), "demo": settings.demo}

    @app.get("/api/v1/nights")
    def nights(
        start: date | None = None,
        end: date | None = None,
        cursor: date | None = None,
        limit: Annotated[int, Query(ge=1, le=92)] = 31,
    ):
        view = service()
        if end is None:
            latest = _query(view.status).get("latest_night")
            end = (
                date.fromisoformat(latest)
                if latest
                else datetime.now(ZoneInfo(settings.timezone)).date()
            )
        start = start or end - timedelta(days=29)
        _day(start)
        _day(end)
        if start > end or (end - start).days >= 92:
            raise HTTPException(422, "history_window_requires_1_to_92_days")
        if cursor is not None and not start <= cursor <= end:
            raise HTTPException(422, "cursor_outside_window")
        return _query(view.nights, start, end, cursor=cursor, limit=limit)

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

    @app.get("/health", tags=["service"])
    def health() -> dict[str, str]:
        """Process liveness only; this does not assert data-source readiness."""
        return {"status": "ok", "service": "SleepScape", "version": __version__}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app

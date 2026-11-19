"""Protected, read-only readiness, report and model presentation."""

from datetime import date

from sleepscape.config import Settings
from sleepscape.domain.dashboard_dates import require_visible_night
from sleepscape.services.model_health import model_status
from sleepscape.services.readiness import evaluate_night, next_check, public_readiness
from sleepscape.services.sleep_analysis import report_state
from sleepscape.storage.analysis import AnalysisStorageError
from sleepscape.storage.reader import StorageReadError


def analysis_view(settings: Settings, day: date) -> dict:
    require_visible_night(day, settings.dashboard_start_date)
    try:
        private_readiness = evaluate_night(settings, day)
    except (StorageReadError, AnalysisStorageError):
        private_readiness = None
    readiness = public_readiness(private_readiness) if private_readiness is not None else None
    state = report_state(settings, day, readiness=private_readiness)
    return {
        "night_date": day.isoformat(),
        "implementation_stage": "reports",
        "data_state": readiness["data_state"] if readiness is not None else "not_evaluated",
        "execution_state": state["execution_state"],
        "automatic_enabled": state.get("automatic_enabled"),
        "model": model_status(settings),
        "missing": readiness["missing"] if readiness is not None else [],
        "readiness": readiness,
        "readiness_unavailable": readiness is None,
        "next_check": next_check(settings),
        "blockers": state["blockers"],
        "attempt": state.get("attempt"),
        "report": state["report"],
        "notice": (
            "报告保留生成时的证据；当前观测变化会提示，不自动改写已发布结论。刷新页面不会调用模型。"
        ),
    }

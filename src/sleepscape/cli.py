"""Thin command interface: explicit online collection and offline verification."""

import argparse
import json
import platform
import sqlite3
from datetime import date, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn

from sleepscape import __version__
from sleepscape.adapters.catalog import build_plan
from sleepscape.config import Settings
from sleepscape.services.acceptance import audit
from sleepscape.services.backup import BackupError, check_restore, create_backup
from sleepscape.services.collection import CollectionError, collect, prepare_sync
from sleepscape.services.verification import public_summary, verify
from sleepscape.storage.reports import write_private_json
from sleepscape.storage.repository import Repository


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def selected_end(value: date | None, settings: Settings) -> date:
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    end = value or today - timedelta(days=1)
    if end >= today:
        raise ValueError("end must be before today in the configured timezone")
    return end


def main() -> None:
    parser = argparse.ArgumentParser(description="SleepScape 本地开发工具")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="启动本地 API 服务")
    commands.add_parser("doctor", help="离线检查环境；不读取密钥、不连接 Garmin")
    commands.add_parser("model-status", help="只读模型配置与最近核验状态，不读密钥、不联网")
    model_check = commands.add_parser("model-check", help="显式发送合成请求核验模型接口")
    model_check.add_argument(
        "--live", action="store_true", help="确认调用已配置模型；只发送合成探针，不发送健康数据"
    )
    commands.add_parser("web-password", help="交互设置本地看板密码，仅保存哈希")
    commands.add_parser("db-backup", help="在线创建新的私有SQLite备份并验证，不覆盖现有文件")
    restore = commands.add_parser("db-restore-check", help="仅在临时目录验证备份恢复，不覆盖源库")
    restore.add_argument("backup", type=Path, help="相对data目录的备份路径，或绝对路径")
    for name in ("environment-register", "environment-profile"):
        command = commands.add_parser(name, help="离线注册环境设备或不可变测量配置")
        command.add_argument("--device-id", required=True)
        command.add_argument("--profile-id", required=True)
        command.add_argument("--profile-file", type=Path, required=True)
        if name == "environment-register":
            command.add_argument(
                "--token-file",
                type=Path,
                required=True,
                help="新建私有文件保存设备令牌，不在终端显示",
            )
    command = commands.add_parser("environment-rotate-token", help="轮换设备令牌，旧令牌立即失效")
    command.add_argument("--device-id", required=True)
    command.add_argument("--token-file", type=Path, required=True)
    command = commands.add_parser("environment-revoke", help="撤销设备上传权限，保留历史数据")
    command.add_argument("--device-id", required=True)
    commands.add_parser("environment-status", help="只读检查环境接收状态，不联网、不建库")
    index = commands.add_parser("environment-index", help="显式建立或更新环境UTC派生索引")
    index.add_argument("--rebuild", action="store_true", help="先核验备份，再强制重建派生索引")
    commands.add_parser("environment-index-verify", help="只读源库，在临时副本验证环境时间索引重放")
    daily = commands.add_parser("daily-run", help="执行上海当日有界采集、检查及已启用的模型分析")
    daily_mode = daily.add_mutually_exclusive_group()
    daily_mode.add_argument(
        "--plan-only", action="store_true", help="只预览严格N日范围，不联网、不写入"
    )
    daily_mode.add_argument(
        "--retry", action="store_true", help="显式重试当日未完成任务，仍遵守Retry-After"
    )
    commands.add_parser("daily-status", help="只读每日编排状态；不检查或安装服务器定时器")
    commands.add_parser("morning-feeling-prepare", help="核验备份并建立晨起睡感表")
    commands.add_parser("analysis-prepare", help="核验备份后显式建立完整性检查表；不调用LLM")
    readiness = commands.add_parser("analysis-check", help="检查单夜或配置N日双源完整性；不联网")
    readiness.add_argument("--date", type=date.fromisoformat, help="睡眠归属日，默认配置的N日窗口")
    readiness.add_argument("--read-only", action="store_true", help="只计算，不保存检查记录或迁移")
    commands.add_parser("sleep-analysis-prepare", help="验证备份后显式建立报告表，不调用模型")
    commands.add_parser("sleep-review-prepare", help="验证备份后显式建立七日复盘表，不调用模型")
    analysis = commands.add_parser("sleep-analyze", help="按资格与预算生成尚未发布的睡眠报告")
    analysis.add_argument("--date", type=date.fromisoformat, help="睡眠归属日，必须在配置N日内")
    analysis.add_argument(
        "--revise", action="store_true", help="按新Prompt显式重读指定夜晚，最多一次请求"
    )
    analysis.add_argument("--live", action="store_true", help="调用已配置模型发送最小睡眠证据")
    maintenance = commands.add_parser("maintenance-run", help="核验成套备份、导出及受控保留")
    maintenance_mode = maintenance.add_mutually_exclusive_group()
    maintenance_mode.add_argument(
        "--plan-only", action="store_true", help="只读维护预检，不备份、不删除、不联网"
    )
    maintenance_mode.add_argument("--retry", action="store_true", help="显式重试当日未完成维护")
    commands.add_parser("maintenance-status", help="只读维护、磁盘及导出状态")
    maintenance_restore = commands.add_parser(
        "maintenance-restore-check", help="在临时目录核验成套恢复，不覆盖运行库"
    )
    maintenance_restore.add_argument("backup_id", help="本地已登记的成套备份ID")
    sync = commands.add_parser("garmin-sync", help="同步今日暂定睡眠与截至昨日的全天数据")
    sync.add_argument("--plan-only", action="store_true", help="只读运行元数据预览计划，不联网")
    sync.add_argument("--detail-days", type=int, default=7)
    sync.add_argument("--history-days", type=int, help="覆盖自动选择的初始38日/滚动7日背景")
    for name, help_text in (
        ("garmin-plan", "离线预览接口范围，不读取凭据"),
        ("garmin-collect", "采集 Garmin 并保存本地，随后生成核验报告"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--end", type=date.fromisoformat, help="截止日期，默认当地昨日")
        command.add_argument("--detail-days", type=int, default=7)
        command.add_argument("--history-days", type=int, default=31)
    commands.add_parser("garmin-verify", help="离线核验已保存的数据，不连接 Garmin")
    commands.add_parser("garmin-audit", help="只读源库，在临时库验证重放、去重及修订保护")
    args = parser.parse_args()
    settings = Settings()

    if args.command == "morning-feeling-prepare":
        from sleepscape.services.morning_feeling import prepare

        try:
            emit(prepare(settings))
        except Exception:
            emit({"status": "failed", "error_code": "morning_feeling_prepare_failed"})
            raise SystemExit(1) from None
        return

    if args.command.startswith("maintenance-"):
        from sleepscape.services.maintenance import (
            check_bundle_restore,
            maintenance_status,
            plan_maintenance,
            run_maintenance,
        )
        from sleepscape.storage.backup_manifest import MaintenanceError

        if args.command == "maintenance-status":
            emit(maintenance_status(settings))
            return
        if args.command == "maintenance-run" and args.plan_only:
            emit(plan_maintenance(settings))
            return
        try:
            result = (
                check_bundle_restore(settings, args.backup_id)
                if args.command == "maintenance-restore-check"
                else run_maintenance(settings, retry=args.retry)
            )
        except MaintenanceError as exc:
            emit({"status": "failed", "error_code": exc.code})
            raise SystemExit(1) from None
        emit(result)
        if result["status"] not in {"completed", "verified"}:
            raise SystemExit(1)
        return

    if args.command in {"daily-run", "daily-status"}:
        from sleepscape.services.daily import daily_status, prepare_daily, run_daily

        if args.command == "daily-status":
            emit(daily_status(settings))
            return
        if args.plan_only:
            plan, metadata = prepare_daily(settings)
            emit(
                {
                    "status": "planned",
                    "timezone": "Asia/Shanghai",
                    "scheduled_time": "09:00",
                    "lookback_days": settings.garmin_lookback_days,
                    "sleep_window": {
                        "start_date": metadata["detail_start"],
                        "end_date": metadata["end_date"],
                    },
                    "background_window": {
                        "start_date": metadata["history_start"],
                        "end_date": metadata["complete_day_through"],
                    },
                    "planned_requests": len(plan),
                    "analysis": "automatic_enabled"
                    if settings.llm_automatic_enabled
                    else "automatic_disabled",
                    "note": "离线计划；分页、认证和有限网络重试另计。定时器是否已安装未核验。",
                }
            )
            return
        result = run_daily(settings, retry=args.retry)
        emit(result)
        if result["status"] != "completed":
            raise SystemExit(1)
        return

    if args.command == "sleep-review-prepare":
        from sleepscape.services.weekly_review import prepare_review
        from sleepscape.storage.reader import StorageReadError
        from sleepscape.storage.usage import CapacityError
        from sleepscape.storage.weekly_review import ReviewStorageError

        try:
            emit(prepare_review(settings))
        except (ReviewStorageError, StorageReadError, BackupError, CapacityError) as exc:
            emit({"status": "failed", "code": getattr(exc, "code", str(exc))})
            raise SystemExit(1) from None
        return

    if args.command in {"sleep-analysis-prepare", "sleep-analyze"}:
        from sleepscape.services.sleep_analysis import prepare_reports, run_analysis
        from sleepscape.storage.reader import StorageReadError
        from sleepscape.storage.sleep_analysis import SleepAnalysisStorageError

        if args.command == "sleep-analyze" and not args.live:
            parser.error("sleep-analyze requires --live; no request was sent")
        if args.command == "sleep-analyze" and args.revise and args.date is None:
            parser.error("--revise requires --date")
        try:
            result = (
                prepare_reports(settings)
                if args.command == "sleep-analysis-prepare"
                else run_analysis(
                    settings, day=args.date, **({"revise": True} if args.revise else {})
                )
            )
            emit(result)
        except (SleepAnalysisStorageError, StorageReadError, BackupError) as exc:
            emit({"status": "failed", "code": str(exc)})
            raise SystemExit(1) from None
        except ValueError:
            parser.error("analysis_date_outside_window")
        if result["status"] not in {"initialized", "already_initialized", "completed"}:
            raise SystemExit(1)
        return

    if args.command in {"analysis-prepare", "analysis-check"}:
        from sleepscape.services.readiness import check_readiness, prepare_readiness
        from sleepscape.storage.analysis import AnalysisStorageError
        from sleepscape.storage.reader import StorageReadError

        try:
            result = (
                prepare_readiness(settings)
                if args.command == "analysis-prepare"
                else check_readiness(settings, day=args.date, persist=not args.read_only)
            )
            emit(result)
        except (AnalysisStorageError, BackupError, StorageReadError) as exc:
            emit({"status": "failed", "code": str(exc)})
            raise SystemExit(1) from None
        except ValueError:
            parser.error("invalid_analysis_date_or_window")
        return

    if args.command in {"model-status", "model-check"}:
        from sleepscape.services.model_health import model_status, run_model_check

        if args.command == "model-status":
            emit(model_status(settings))
            return
        if not args.live:
            parser.error("model-check requires --live; no request was sent")
        result = run_model_check(settings)
        emit(result)
        if result["status"] != "verified":
            raise SystemExit(1)
        return

    if args.command.startswith("environment-"):
        from sleepscape.services.environment import (
            identifiers,
            register_device,
            register_profile,
            rotate_device_token,
        )
        from sleepscape.storage.environment import EnvironmentError, revoke, status

        try:
            if args.command == "environment-register":
                result = register_device(
                    settings, args.device_id, args.profile_id, args.profile_file, args.token_file
                )
            elif args.command == "environment-profile":
                result = register_profile(
                    settings, args.device_id, args.profile_id, args.profile_file
                )
            elif args.command == "environment-rotate-token":
                result = rotate_device_token(settings, args.device_id, args.token_file)
            elif args.command == "environment-revoke":
                identifiers(args.device_id)
                revoke(settings.data_dir, args.device_id)
                result = {"status": "revoked", "device_id": args.device_id}
            elif args.command in {"environment-index", "environment-index-verify"}:
                from sleepscape.services.environment_index import (
                    prepare_environment_index,
                    verify_environment_index,
                )

                result = (
                    prepare_environment_index(settings, force=args.rebuild)
                    if args.command == "environment-index"
                    else verify_environment_index(settings)
                )
            else:
                result = status(settings.data_dir)
        except EnvironmentError as exc:
            emit({"status": "environment_operation_failed", "code": str(exc)})
            raise SystemExit(1) from None
        except Exception:
            emit(
                {
                    "status": "environment_operation_failed",
                    "code": "local_environment_operation_failed",
                }
            )
            raise SystemExit(1) from None
        emit(result)
        if args.command == "environment-index-verify" and result["status"] != "verified":
            raise SystemExit(1)
        return

    if args.command in {"db-backup", "db-restore-check"}:
        try:
            result = (
                create_backup(settings)
                if args.command == "db-backup"
                else check_restore(settings, args.backup)
            )
        except BackupError as exc:
            emit({"status": "backup_not_verified", "code": str(exc)})
            raise SystemExit(1) from None
        emit(result)
        return

    if args.command == "web-password":
        from sleepscape.web.security import configure_password

        try:
            configure_password(settings)
        except (OSError, ValueError):
            emit({"status": "configuration_failed", "code": "web_password_configuration_failed"})
            raise SystemExit(1) from None
        emit({"status": "configured"})
        return

    if args.command == "garmin-sync":
        today = datetime.now(ZoneInfo(settings.timezone)).date()
        try:
            plan, metadata = prepare_sync(
                settings,
                today,
                detail_days=args.detail_days,
                history_days=args.history_days,
            )
        except ValueError as exc:
            parser.error(str(exc))
        except CollectionError as exc:
            emit({"status": "sync_not_started", "code": str(exc)})
            raise SystemExit(1) from None
        if args.plan_only:
            emit(
                {
                    "status": "plan_only",
                    "region": settings.garmin_base_url,
                    "timezone": settings.timezone,
                    "planned_requests": len(plan),
                    "plan": metadata,
                    "requests": [
                        {"resource": r.resource, "key": r.key, "path": r.path, "params": r.params}
                        for r in plan
                    ],
                }
            )
            return
        if metadata.get("backfill_required"):
            emit({"status": "backfill_required", "plan": metadata})
            raise SystemExit(2)
        try:
            summary = collect(
                settings,
                today,
                metadata["detail_days"],
                metadata["history_days"],
                progress=emit,
                plan=plan,
                metadata=metadata,
            )
        except CollectionError as exc:
            emit({"status": "collection_not_completed", "code": str(exc)})
            raise SystemExit(1) from None
        emit(summary)

    if args.command == "garmin-audit":
        report = audit(settings)
        destination = settings.data_dir / "reports" / "garmin-acceptance.json"
        write_private_json(destination, report)
        emit({**report, "private_report": str(destination)})
        if report["status"] != "passed":
            raise SystemExit(1)
        return

    if args.command == "serve":
        uvicorn.run(
            "sleepscape.app:create_app",
            factory=True,
            host=settings.host,
            port=settings.port,
            access_log=False,
        )
        return

    if args.command in {"garmin-plan", "garmin-collect"}:
        try:
            end = selected_end(args.end, settings)
            plan = build_plan(end, args.detail_days, args.history_days)
        except ValueError as exc:
            parser.error(str(exc))
        if args.command == "garmin-plan":
            emit(
                {
                    "region": settings.garmin_base_url,
                    "timezone": settings.timezone,
                    "planned_requests": len(plan),
                    "note": "活动额外分页、认证和有限重试另计；此命令没有联网。",
                    "requests": [
                        {"resource": r.resource, "key": r.key, "path": r.path, "params": r.params}
                        for r in plan
                    ],
                }
            )
            return
        try:
            summary = collect(settings, end, args.detail_days, args.history_days, progress=emit)
        except CollectionError as exc:
            emit({"status": "collection_not_completed", "code": str(exc)})
            raise SystemExit(1) from None
        emit(summary)

    if args.command in {"garmin-collect", "garmin-verify", "garmin-sync"}:
        if not (settings.data_dir / "sleepscape.sqlite3").exists():
            emit({"status": "no_local_database"})
            raise SystemExit(1)
        with Repository(settings.data_dir) as repository:
            report = verify(repository)
        destination = settings.data_dir / "reports" / "garmin-verification.json"
        write_private_json(destination, report)
        emit({**public_summary(report), "private_report": str(destination)})
        if report["status"] != "usable_with_limitations":
            raise SystemExit(1)
        return

    from sleepscape.services.model_health import model_status

    report = {
        "project": "SleepScape",
        "version": __version__,
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "garmin_sdk": version("garmin-connect-personal-api-python"),
        "timezone": settings.timezone,
        "local_env_present": Path(".env").is_file(),
        "data_directory_present": settings.data_dir.is_dir(),
        "garmin_key_file_present": settings.garmin_key_file.is_file(),
        "garmin_connection": "not_checked_by_this_offline_command",
        "model": model_status(settings),
        "note": "离线环境检查；文件存在不代表凭据有效或采集已经运行。",
    }
    emit(report)

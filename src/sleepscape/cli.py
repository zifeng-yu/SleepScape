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
        "note": "离线环境检查；文件存在不代表凭据有效或采集已经运行。",
    }
    emit(report)

"""Explicit preparation and selected-date access for personal morning ratings."""

from pathlib import Path

from sleepscape.domain.dashboard_dates import require_visible_night
from sleepscape.services.backup import check_restore, create_backup
from sleepscape.storage import morning_feeling
from sleepscape.storage.reader import DashboardReader
from sleepscape.storage.repository import Repository
from sleepscape.storage.usage import require_capacity


def prepare(settings):
    directory = settings.data_dir
    with DashboardReader(directory) as reader:
        if reader.present and morning_feeling.available(reader.connection):
            return {"status": "already_initialized"}
        new_database = not reader.present
    if new_database:
        require_capacity(directory, "write")
        with Repository(directory):
            pass
    backup = create_backup(settings)
    check_restore(settings, Path(backup["backup_path"]))
    morning_feeling.migrate(directory)
    return {"status": "initialized", "backup_verified": True}


def get(settings, day):
    require_visible_night(day, settings.dashboard_start_date)
    return morning_feeling.read(settings.data_dir, day)


def put(settings, day, feeling):
    require_visible_night(day, settings.dashboard_start_date)
    return morning_feeling.save(settings.data_dir, day, feeling)

"""
Regression tests for app/services/scheduling_service.py -- covering the
real bugs hit (and fixed) this session:

1. Scheduled Drive/YouTube execution used to look up the destination
   folder/playlist from the Setlist's day_name -- which is always None
   for single-screenshot imports, and doesn't necessarily match whatever
   folder/playlist you'd actually selected in that tab's dropdown at
   Schedule time. Fixed by adding `target_name` to ScheduledTask,
   captured at schedule_task() time and read back directly at execution.

2. The merged Drive PDF's filename used to be built from
   `{day_name} {date} Charts.pdf` -- since day_name is None for
   single-screenshot setlists, this produced "None 2026-09-23
   Charts.pdf" instead of matching what a manual click on Merge would
   have produced. Fixed to use target_name directly, matching
   ChartsPage._merged_pdf_filename()'s own convention exactly.

3. Logic Pro's scheduled Copy Audio / Build Manifest steps used to read
   their destination folder from a `logic_pro_output_folders` Settings
   field that had no UI to configure it anywhere -- meaning every
   scheduled Logic Pro task was guaranteed to fail. Fixed to use the
   same `desktop_output_folder` field the interactive Build page's own
   Prepare button already uses.

4. execute_task must never leave a task stuck in RUNNING, even when the
   underlying action raises an exception it doesn't explicitly handle.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from unittest.mock import patch

import pypdf
import pytest

from app.models.scheduled_task import ActionType, TaskStatus
from app.models.settings import Settings
from app.models.song_entry import ChartMatch, SongEntry, YouTubeMatch
from app.services import database_service, scheduling_service


def _write_minimal_pdf(path: str) -> None:
    """A real, single-blank-page PDF -- pdf_merge_service actually parses these with pypdf, so a raw byte stub isn't enough."""
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with open(path, "wb") as f:
        writer.write(f)


@pytest.fixture
def setlist_with_entries(isolated_home):
    """A single-screenshot setlist (day_name=None) with one song -- the exact shape that exposed bug #1/#2 above."""
    database_service.initialize_database()
    setlist_id = database_service.create_setlist(
        source_type="single_screenshot", service_date=datetime.date(2026, 9, 27)
    )
    entries = [SongEntry(order=1, ocr_title="Song A")]
    database_service.add_song_entries(setlist_id, entries)
    return setlist_id


def test_drive_execution_uses_target_name_not_day_name(setlist_with_entries, isolated_home, tmp_path):
    """The core target_name regression: a setlist with no day_name must still resolve its folder correctly."""
    setlist_id = setlist_with_entries
    settings = Settings()
    settings.chart_folders = {"domingo": str(tmp_path / "charts")}
    settings.charts_merged_output_folder = str(tmp_path / "merged")
    os.makedirs(settings.charts_merged_output_folder, exist_ok=True)

    entries = [SongEntry(order=1, ocr_title="Song A", chart_match=ChartMatch(title="doc", doc_id="fake-id", confidence=1.0))]
    database_service.replace_song_entries(setlist_id, entries)

    task_id = scheduling_service.schedule_task(
        setlist_id, ActionType.DRIVE, datetime.datetime.now(), target_name="domingo"
    )

    def fake_export(_drive, _doc_id, output_path):
        _write_minimal_pdf(output_path)
        return output_path

    with patch("app.services.scheduling_service.drive_service.authenticate", return_value="creds"), \
         patch("app.services.scheduling_service.drive_service.get_drive_client", return_value="client"), \
         patch("app.services.scheduling_service.drive_service.export_doc_as_pdf", side_effect=fake_export), \
         patch("app.services.scheduling_service.pdf_merge_service.merge_charts_from_manifest", return_value=1):
        scheduling_service.approve_task(task_id, settings)

    task = database_service.get_task(task_id)
    assert task.status == TaskStatus.DONE, task.error_message


def test_drive_merged_pdf_is_named_after_target_not_day_and_date(setlist_with_entries, tmp_path):
    """Regression for the 'None 2026-09-23 Charts.pdf' bug -- the merged file must be named after target_name alone."""
    setlist_id = setlist_with_entries
    settings = Settings()
    settings.chart_folders = {"domingo": str(tmp_path / "charts")}
    settings.charts_merged_output_folder = str(tmp_path / "merged")
    os.makedirs(settings.charts_merged_output_folder, exist_ok=True)

    entries = [SongEntry(order=1, ocr_title="Song A", chart_match=ChartMatch(title="doc", doc_id="fake-id", confidence=1.0))]
    database_service.replace_song_entries(setlist_id, entries)

    task_id = scheduling_service.schedule_task(
        setlist_id, ActionType.DRIVE, datetime.datetime.now(), target_name="domingo"
    )

    def fake_export(_drive, _doc_id, output_path):
        _write_minimal_pdf(output_path)
        return output_path

    with patch("app.services.scheduling_service.drive_service.authenticate", return_value="creds"), \
         patch("app.services.scheduling_service.drive_service.get_drive_client", return_value="client"), \
         patch("app.services.scheduling_service.drive_service.export_doc_as_pdf", side_effect=fake_export):
        scheduling_service.approve_task(task_id, settings)

    produced_files = os.listdir(settings.charts_merged_output_folder)
    assert produced_files == ["domingo.pdf"], produced_files


def test_youtube_execution_uses_target_name_not_day_name(setlist_with_entries):
    """Same target_name bug, YouTube side."""
    setlist_id = setlist_with_entries
    settings = Settings()
    settings.youtube_playlists = {"MyPlaylist": "fake-playlist-id"}

    entries = [SongEntry(order=1, ocr_title="Song A", youtube_match=YouTubeMatch(title="vid", video_id="xyz", confidence=1.0))]
    database_service.replace_song_entries(setlist_id, entries)

    task_id = scheduling_service.schedule_task(
        setlist_id, ActionType.YOUTUBE, datetime.datetime.now(), target_name="MyPlaylist"
    )

    with patch("app.services.scheduling_service.youtube_service.authenticate", return_value="creds"), \
         patch("app.services.scheduling_service.youtube_service.get_youtube_client", return_value="client"), \
         patch("app.services.scheduling_service.youtube_service.clear_playlist", return_value=0), \
         patch("app.services.scheduling_service.youtube_service.add_videos_to_playlist", return_value=1):
        scheduling_service.approve_task(task_id, settings)

    task = database_service.get_task(task_id)
    assert task.status == TaskStatus.DONE, task.error_message


def test_youtube_execution_skips_songs_excluded_from_youtube(setlist_with_entries):
    """
    Per-destination inclusion must be honored by the actual SCHEDULED
    run, not just the interactive UI -- a song excluded from YouTube
    (e.g. kept only for Drive/Logic Pro) must never end up in the
    playlist a background/approved task actually pushes.
    """
    setlist_id = setlist_with_entries
    settings = Settings()
    settings.youtube_playlists = {"MyPlaylist": "fake-playlist-id"}

    included = SongEntry(
        order=1, ocr_title="Song A", youtube_match=YouTubeMatch(title="vid", video_id="included-id", confidence=1.0)
    )
    excluded = SongEntry(
        order=2,
        ocr_title="Song B",
        youtube_match=YouTubeMatch(title="vid2", video_id="excluded-id", confidence=1.0),
        excluded_from_youtube=True,
    )
    database_service.replace_song_entries(setlist_id, [included, excluded])

    task_id = scheduling_service.schedule_task(
        setlist_id, ActionType.YOUTUBE, datetime.datetime.now(), target_name="MyPlaylist"
    )

    added_video_ids = []
    with patch("app.services.scheduling_service.youtube_service.authenticate", return_value="creds"), \
         patch("app.services.scheduling_service.youtube_service.get_youtube_client", return_value="client"), \
         patch("app.services.scheduling_service.youtube_service.clear_playlist", return_value=0), \
         patch(
             "app.services.scheduling_service.youtube_service.add_videos_to_playlist",
             side_effect=lambda _yt, _pid, ids: added_video_ids.extend(ids) or len(ids),
         ):
        scheduling_service.approve_task(task_id, settings)

    task = database_service.get_task(task_id)
    assert task.status == TaskStatus.DONE, task.error_message
    assert added_video_ids == ["included-id"]


def test_logic_pro_destination_uses_desktop_output_folder(isolated_home):
    """
    Regression for the 'no Settings UI ever configures this' bug --
    _logic_pro_destination_folder must read desktop_output_folder (the
    same field the interactive Build page uses), not the removed
    logic_pro_output_folders field.
    """
    settings = Settings()
    settings.desktop_output_folder = "/tmp/my_desktop_output"

    destination = scheduling_service._logic_pro_destination_folder(settings)

    assert str(destination) == "/tmp/my_desktop_output"
    assert not hasattr(settings, "logic_pro_output_folders")


def test_logic_pro_destination_falls_back_to_desktop_secuencias(isolated_home):
    settings = Settings()  # desktop_output_folder left unconfigured

    destination = scheduling_service._logic_pro_destination_folder(settings)

    assert destination == Path(isolated_home) / "Desktop" / "Secuencias"


def test_execute_task_never_leaves_a_task_stuck_running(setlist_with_entries):
    """
    execute_task must always resolve to DONE or FAILED, even for an
    exception type it doesn't explicitly catch (SchedulingError is
    handled explicitly; this checks the broader except Exception path).
    """
    setlist_id = setlist_with_entries
    settings = Settings()
    settings.youtube_playlists = {"X": "playlist-id"}

    entries = [SongEntry(order=1, ocr_title="Song A", youtube_match=YouTubeMatch(title="v", video_id="v1", confidence=1.0))]
    database_service.replace_song_entries(setlist_id, entries)

    task_id = scheduling_service.schedule_task(setlist_id, ActionType.YOUTUBE, datetime.datetime.now(), target_name="X")

    with patch(
        "app.services.scheduling_service.youtube_service.authenticate",
        side_effect=RuntimeError("unexpected network stack failure"),
    ):
        scheduling_service.execute_task(task_id, settings)

    task = database_service.get_task(task_id)
    assert task.status == TaskStatus.FAILED
    assert task.status != TaskStatus.RUNNING
    assert "unexpected network stack failure" in task.error_message

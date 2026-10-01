"""
YouTube service for Setlist Builder.

Authenticates once via a one-time browser login (OAuth), then remembers
you afterward via a saved token file -- so the weekly playlist update
never requires logging in again after the first successful run.

Two operations, kept deliberately separate and simple:
  - clear_playlist(): removes every existing video from the playlist
  - add_videos_to_playlist(): adds a list of video IDs, in order

Called together in that order, these fully replace a playlist's
contents each week.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# What this app is allowed to do with your YouTube account -- managing
# playlists, nothing else (not reading subscriptions, not uploading
# videos). A narrower scope is safer, and is also exactly what Google's
# one-time consent screen will show you plainly during login.
_SCOPES = ["https://www.googleapis.com/auth/youtube"]


def _token_path() -> Path:
    """
    Where the saved login token lives, once you've authenticated once.
    Same folder as settings.json (Application Support), so it survives
    moving the project folder around -- same reasoning as Settings itself.
    """
    directory = Path.home() / "Library" / "Application Support" / "SetlistBuilder"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "youtube_token.json"


def authenticate(client_secret_path: str, allow_interactive: bool = True) -> Optional[Credentials]:
    """
    Return valid credentials for the YouTube API, logging in via a
    one-time browser flow if there's no saved token yet, or refreshing
    silently if a saved token has simply expired.

    Returns None (logged as an error) if client_secret_path is missing
    or invalid, rather than raising -- a misconfigured Settings path
    shouldn't crash the app, just prevent this one feature from running.

    `allow_interactive=False` (used by scheduled/unattended task
    execution -- see scheduling_service.py) skips the browser-login
    fallback entirely: if there's no valid, silently-refreshable token,
    this returns None instead of popping open a browser window with no
    one there to complete it. That's exactly the "re-auth needed" signal
    the architecture discussion calls for -- the caller turns it into a
    notification asking you to open the app and log in normally instead.
    """
    token_file = _token_path()
    creds: Optional[Credentials] = None

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), _SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_file.write_text(creds.to_json())
            return creds
        except Exception as error:
            logger.warning("Failed to refresh saved YouTube login, re-authenticating: %s", error)
            creds = None

    if not allow_interactive:
        logger.warning("No valid YouTube login and interactive login is disabled for this call.")
        return None

    if not client_secret_path or not Path(client_secret_path).exists():
        logger.error(
            "YouTube client secret file not found: %s -- configure it in Settings.",
            client_secret_path,
        )
        return None

    logger.info("No valid saved YouTube login -- opening browser for one-time login.")
    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, _SCOPES)
    creds = flow.run_local_server(port=0)

    token_file.write_text(creds.to_json())
    logger.info("YouTube login saved to %s for future runs.", token_file)
    return creds


def get_youtube_client(creds: Credentials):
    """Build the actual API client object used for playlist operations."""
    return build("youtube", "v3", credentials=creds)


def clear_playlist(youtube, playlist_id: str) -> int:
    """
    Remove every existing video from the given playlist.

    Returns the number of items removed. Logs and skips (rather than
    raising) any individual deletion that fails, so one bad item doesn't
    block clearing the rest of the playlist.
    """
    removed_count = 0
    page_token = None

    while True:
        response = youtube.playlistItems().list(
            part="id", playlistId=playlist_id, maxResults=50, pageToken=page_token
        ).execute()

        for item in response.get("items", []):
            item_id = item["id"]
            try:
                youtube.playlistItems().delete(id=item_id).execute()
                removed_count += 1
            except HttpError as error:
                logger.error("Failed to remove playlist item %s: %s", item_id, error)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    logger.info("Removed %d existing item(s) from playlist %s", removed_count, playlist_id)
    return removed_count


def add_videos_to_playlist(youtube, playlist_id: str, video_ids: list[str]) -> int:
    """
    Add each video ID to the playlist, in the given order.

    Returns the number successfully added. Logs and skips (rather than
    raising) any individual video that fails to add -- for example, a
    video that's been deleted or made private since the lookup site was
    last checked -- so one bad video doesn't block adding the rest.
    """
    added_count = 0

    for position, video_id in enumerate(video_ids):
        try:
            youtube.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "position": position,
                        "resourceId": {
                            "kind": "youtube#video",
                            "videoId": video_id,
                        },
                    }
                },
            ).execute()
            added_count += 1
        except HttpError as error:
            logger.error("Failed to add video %s to playlist: %s", video_id, error)

    logger.info("Added %d video(s) to playlist %s", added_count, playlist_id)
    return added_count

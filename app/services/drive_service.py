"""
Google Drive service for Setlist Builder.

Authenticates separately from the YouTube service (its own client secret,
its own scope, its own saved token) even though both use the same
Google-account OAuth pattern -- keeping them independent means revoking
or reconfiguring one never affects the other.

Two operations, mirroring youtube_service.py's shape:
  - list_docs_in_folder(): lists every Google Doc in one Drive folder
    (one chart doc per song), for fuzzy-matching against song titles
  - export_doc_as_pdf(): downloads a single Google Doc, converted to PDF

Matching (find_chart_for_song) lives here too, the same way
find_video_for_song lives alongside fetch_video_lookup_library in
video_lookup_service.py -- "fetch/list the remote library" and "match a
title against it" are the same concern, just for Drive instead of a
scraped web page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from rapidfuzz import fuzz, process

from app.utils.text_normalize import normalize_for_matching

logger = logging.getLogger(__name__)

# Read-only, and only for files -- never folders/Shared Drives metadata
# beyond what listing needs. This app only ever reads chart docs, so
# there's no reason to request write access to Drive.
_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

_GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
_EXPORT_MIME_TYPE = "application/pdf"


@dataclass
class DriveDoc:
    """One Google Doc found in the configured charts folder."""

    id: str
    name: str


def _token_path() -> Path:
    """
    Where the saved Drive login token lives. Same Application Support
    folder as the YouTube token and settings.json, but its own filename
    -- Drive and YouTube are authenticated (and can be re-authenticated,
    revoked, etc.) completely independently of each other.
    """
    directory = Path.home() / "Library" / "Application Support" / "SetlistBuilder"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "drive_token.json"


def authenticate(client_secret_path: str, allow_interactive: bool = True) -> Optional[Credentials]:
    """
    Return valid credentials for the Drive API, logging in via a
    one-time browser flow if there's no saved token yet, or refreshing
    silently if a saved token has simply expired.

    Returns None (logged as an error) if client_secret_path is missing
    or invalid, rather than raising -- an unconfigured Drive client
    secret shouldn't crash the app, just prevent the Charts feature from
    running until it's set in Settings.

    `allow_interactive=False` (used by scheduled/unattended task
    execution -- see scheduling_service.py) skips the browser-login
    fallback: with no valid, silently-refreshable token, this returns
    None rather than popping open a browser with no one there to
    complete it -- the caller turns that into a "please re-authenticate"
    notification instead.
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
            logger.warning("Failed to refresh saved Drive login, re-authenticating: %s", error)
            creds = None

    if not allow_interactive:
        logger.warning("No valid Drive login and interactive login is disabled for this call.")
        return None

    if not client_secret_path or not Path(client_secret_path).exists():
        logger.error(
            "Google Drive client secret file not found: %s -- configure it in Settings.",
            client_secret_path,
        )
        return None

    logger.info("No valid saved Drive login -- opening browser for one-time login.")
    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, _SCOPES)
    creds = flow.run_local_server(port=0)

    token_file.write_text(creds.to_json())
    logger.info("Drive login saved to %s for future runs.", token_file)
    return creds


def get_drive_client(creds: Credentials):
    """Build the actual API client object used for Drive operations."""
    return build("drive", "v3", credentials=creds)


def list_docs_in_folder(drive, folder_id: str) -> list[DriveDoc]:
    """
    List every Google Doc directly inside the given Drive folder.

    Restricted to mimeType 'application/vnd.google-apps.document' (a
    real Google Doc, not a PDF/image/other file that might also live in
    that folder) and excludes trashed files, since a deleted chart
    shouldn't still show up as a match candidate.

    Returns an empty list (logged as an error, not raised) if the API
    call fails -- e.g. the folder ID is wrong or the account has no
    access to it -- so a bad Settings value doesn't crash the app.
    """
    docs: list[DriveDoc] = []
    page_token = None

    query = f"'{folder_id}' in parents and mimeType = '{_GOOGLE_DOC_MIME_TYPE}' and trashed = false"

    try:
        while True:
            response = drive.files().list(
                q=query,
                fields="nextPageToken, files(id, name)",
                pageSize=1000,
                pageToken=page_token,
            ).execute()

            docs.extend(
                DriveDoc(id=f["id"], name=f["name"]) for f in response.get("files", [])
            )

            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except HttpError as error:
        logger.error("Failed to list charts folder %s: %s", folder_id, error)
        return []

    logger.info("Found %d chart doc(s) in Drive folder %s", len(docs), folder_id)
    return docs


def find_chart_for_song(
    song_title: str, library: list[DriveDoc]
) -> Optional[tuple[DriveDoc, float]]:
    """
    Fuzzy-match a song title against the listed charts folder -- the
    same normalization + RapidFuzz approach used for matching
    audio/Logic library folders and the configured lookup site's videos.

    Returns (best_matching_doc, confidence_score), or None if the
    library itself is empty. As with the other matchers, this always
    returns SOMETHING if the library is non-empty, even a low-confidence
    guess -- the caller decides whether the score is good enough to use.
    """
    if not library:
        return None

    normalized_query = normalize_for_matching(song_title)
    normalized_choices = [normalize_for_matching(doc.name) for doc in library]

    result = process.extractOne(normalized_query, normalized_choices, scorer=fuzz.token_sort_ratio)
    if result is None:
        return None

    _matched_text, score, index = result
    logger.debug("Matched '%s' -> '%s' (score=%.1f)", song_title, library[index].name, score)
    return library[index], score


def export_doc_as_pdf(drive, file_id: str, output_path: str) -> Optional[str]:
    """
    Download a single Google Doc, converted server-side to PDF, and
    write it to output_path.

    Uses files().export() (NOT files().get_media()) because a Google Doc
    has no native binary form to just download -- it only exists as
    Drive's internal document format until you ask Drive to render it as
    something else (PDF, docx, plain text, ...).

    Returns output_path on success, or None (logged as an error) if the
    export fails -- e.g. the doc was deleted or access was revoked since
    it was matched -- so one bad chart doesn't block downloading the rest.
    """
    try:
        data = drive.files().export(fileId=file_id, mimeType=_EXPORT_MIME_TYPE).execute()
    except HttpError as error:
        logger.error("Failed to export chart doc %s as PDF: %s", file_id, error)
        return None

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)

    logger.info("Exported chart doc %s to %s", file_id, path)
    return str(path)

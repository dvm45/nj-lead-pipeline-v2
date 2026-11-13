"""
Google Drive integration — download PDFs, upload CSVs, move processed files.

Uses a Google Cloud service account for authentication. Both Drive folders
(input and output) must be shared with the service account's email address
the same way you'd share with a person.

Required .env settings:
  GDRIVE_INPUT_FOLDER_ID    — folder where users drop PDFs
  GDRIVE_OUTPUT_FOLDER_ID   — folder where CSVs and run logs go
  GDRIVE_SERVICE_ACCOUNT_FILE — path to the JSON key (default: credentials/service-account.json)
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

_log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]

_MAX_RETRIES = 3
_RETRY_DELAY = 5  # seconds between retries for transient 5xx errors


# ---------------------------------------------------------------------------
# Service account client
# ---------------------------------------------------------------------------

def _get_service():
    """Build an authorized Google Drive API service using a service account."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_file = os.environ.get(
        "GDRIVE_SERVICE_ACCOUNT_FILE", "credentials/service-account.json"
    )
    creds_path = Path(creds_file)
    if not creds_path.exists():
        raise FileNotFoundError(
            f"Service account file not found: {creds_path.resolve()}\n"
            "Place your Google service account JSON at that path, or set\n"
            "GDRIVE_SERVICE_ACCOUNT_FILE in .env to point to it."
        )
    creds = service_account.Credentials.from_service_account_file(
        str(creds_path), scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# Folder listing helpers
# ---------------------------------------------------------------------------

def _execute_with_retry(request):
    """Execute a Google API request with retry on transient 5xx errors."""
    from googleapiclient.errors import HttpError

    for attempt in range(_MAX_RETRIES):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status >= 500 and attempt < _MAX_RETRIES - 1:
                _log.warning("Drive API %d error (attempt %d/%d), retrying in %ds...",
                             e.resp.status, attempt + 1, _MAX_RETRIES, _RETRY_DELAY)
                time.sleep(_RETRY_DELAY)
            else:
                raise


def _list_children(service, folder_id: str) -> list[dict]:
    """List all non-trashed children (files + folders) of a Drive folder."""
    items: list[dict] = []
    page_token = None
    while True:
        resp = _execute_with_retry(service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ))
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def _find_or_create_subfolder(service, parent_id: str, name: str) -> str:
    """Return the ID of a subfolder, creating it if it doesn't exist."""
    query = (
        f"'{parent_id}' in parents and name='{name}' "
        f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    results = service.files().list(
        q=query, fields="files(id)",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    matches = results.get("files", [])
    if matches:
        return matches[0]["id"]
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(
        body=meta, fields="id", supportsAllDrives=True,
    ).execute()
    return folder["id"]


def _get_processed_folder_id(service, input_folder_id: str) -> str:
    """Return the ID of the Processed subfolder inside the input folder."""
    return _find_or_create_subfolder(service, input_folder_id, "Processed")


# ---------------------------------------------------------------------------
# Recursive PDF listing (preserves subfolder path)
# ---------------------------------------------------------------------------

def _list_pdfs_recursive(
    service,
    folder_id: str,
    prefix: str,
    results: list[dict],
    skip_folder_names: set[str] | None = None,
) -> None:
    """
    Recursively list PDFs in a folder tree.

    Each result dict has keys: id, name, mimeType, relative_path (e.g.
    'Passaic County/report.pdf'). Folders whose name is in skip_folder_names
    are not entered.
    """
    if skip_folder_names is None:
        skip_folder_names = set()

    children = _list_children(service, folder_id)
    for f in children:
        if f["mimeType"] == "application/vnd.google-apps.folder":
            if f["name"] in skip_folder_names:
                continue
            sub_prefix = f"{prefix}{f['name']}/" if prefix else f"{f['name']}/"
            _list_pdfs_recursive(
                service, f["id"], sub_prefix, results, skip_folder_names,
            )
        elif f["mimeType"] == "application/pdf" or f["name"].lower().endswith(".pdf"):
            f["relative_path"] = f"{prefix}{f['name']}" if prefix else f["name"]
            # Stash the parent folder id so we can move the file later
            f["parent_folder_id"] = folder_id
            results.append(f)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_new_pdfs(
    input_folder_id: str,
    local_dir: Path,
    *,
    include_processed: bool = False,
) -> list[dict]:
    """
    Download PDFs from the Drive input folder, preserving subfolder structure.

    By default, files inside the 'Processed' subfolder are skipped. Set
    include_processed=True for --full-refresh mode to process everything.

    Returns a list of dicts for each downloaded file:
      {id, name, relative_path, parent_folder_id, local_path}
    """
    from googleapiclient.http import MediaIoBaseDownload

    service = _get_service()
    skip = set() if include_processed else {"Processed"}
    pdf_files: list[dict] = []
    _list_pdfs_recursive(service, input_folder_id, "", pdf_files, skip_folder_names=skip)

    downloaded: list[dict] = []
    for f in pdf_files:
        local_path = local_dir / f["relative_path"]
        local_path.parent.mkdir(parents=True, exist_ok=True)

        request = service.files().get_media(fileId=f["id"])
        with open(local_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

        f["local_path"] = local_path
        downloaded.append(f)
        _log.info("Downloaded: %s", f["relative_path"])

    return downloaded


# ---------------------------------------------------------------------------
# Move to Processed
# ---------------------------------------------------------------------------

def move_to_processed(
    input_folder_id: str,
    file_info: dict,
) -> None:
    """
    Move a successfully processed PDF to the Processed subfolder in Drive,
    preserving its county subfolder structure.

    file_info must have keys: id, relative_path, parent_folder_id.
    """
    service = _get_service()
    processed_root = _get_processed_folder_id(service, input_folder_id)

    # Recreate intermediate folders under Processed/
    # e.g. relative_path = "Passaic County/report.pdf"
    # → create Processed/Passaic County/ and move the file there
    rel = Path(file_info["relative_path"])
    parts = rel.parent.parts
    dest_folder_id = processed_root
    for part in parts:
        dest_folder_id = _find_or_create_subfolder(service, dest_folder_id, part)

    # Move the file: remove old parent, add new parent
    service.files().update(
        fileId=file_info["id"],
        addParents=dest_folder_id,
        removeParents=file_info["parent_folder_id"],
        supportsAllDrives=True,
    ).execute()
    _log.info("Moved to Processed: %s", file_info["relative_path"])


# ---------------------------------------------------------------------------
# CSV download / upload (for master.csv and issues.csv merge)
# ---------------------------------------------------------------------------

def download_file_by_name(
    folder_id: str,
    filename: str,
    local_path: Path,
) -> bool:
    """
    Download a specific file by name from a Drive folder.

    Returns True if the file was found and downloaded, False if not found.
    """
    from googleapiclient.http import MediaIoBaseDownload

    service = _get_service()
    query = f"'{folder_id}' in parents and name='{filename}' and trashed=false"
    results = service.files().list(
        q=query, fields="files(id)",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    matches = results.get("files", [])
    if not matches:
        return False

    local_path.parent.mkdir(parents=True, exist_ok=True)
    request = service.files().get_media(fileId=matches[0]["id"])
    with open(local_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return True


def upload_file(
    local_path: Path,
    folder_id: str,
    *,
    overwrite_name: str | None = None,
    mimetype: str = "text/csv",
) -> str:
    """
    Upload a file to a Drive folder.

    If overwrite_name is given, update the existing file with that name
    in place (for master.csv pattern). Otherwise create a new file.

    Returns the Drive file ID.
    """
    from googleapiclient.http import MediaFileUpload

    service = _get_service()
    upload_name = overwrite_name or local_path.name

    existing_id = None
    if overwrite_name:
        query = (
            f"'{folder_id}' in parents and name='{overwrite_name}' "
            f"and trashed=false"
        )
        results = service.files().list(
            q=query, fields="files(id)",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        matches = results.get("files", [])
        if matches:
            existing_id = matches[0]["id"]

    media = MediaFileUpload(str(local_path), mimetype=mimetype)

    if existing_id:
        updated = service.files().update(
            fileId=existing_id, media_body=media, supportsAllDrives=True,
        ).execute()
        return updated["id"]

    metadata = {"name": upload_name, "parents": [folder_id]}
    created = service.files().create(
        body=metadata, media_body=media, fields="id", supportsAllDrives=True,
    ).execute()
    return created["id"]


def upload_text(
    content: str,
    folder_id: str,
    filename: str,
) -> str:
    """Upload a text string as a file to a Drive folder (for last_run.txt)."""
    from googleapiclient.http import MediaInMemoryUpload

    service = _get_service()

    query = f"'{folder_id}' in parents and name='{filename}' and trashed=false"
    results = service.files().list(
        q=query, fields="files(id)",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    matches = results.get("files", [])

    media = MediaInMemoryUpload(
        content.encode("utf-8"), mimetype="text/plain",
    )

    if matches:
        updated = service.files().update(
            fileId=matches[0]["id"], media_body=media, supportsAllDrives=True,
        ).execute()
        return updated["id"]

    metadata = {"name": filename, "parents": [folder_id]}
    created = service.files().create(
        body=metadata, media_body=media, fields="id", supportsAllDrives=True,
    ).execute()
    return created["id"]

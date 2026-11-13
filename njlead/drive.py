"""
Google Drive integration — download PDFs (read-only).

Uses a Google Cloud service account for authentication. The Drive input
folder must be shared with the service account's email address the same
way you'd share with a person.

Required .env settings:
  GDRIVE_INPUT_FOLDER_ID    — folder where users drop PDFs
  GDRIVE_SERVICE_ACCOUNT_FILE — path to the JSON key (default: credentials/service-account.json)
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path

_log = logging.getLogger(__name__)

# Read-only scope — this pipeline never writes to Drive
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

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
# Retry helper for transient 5xx errors
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


# ---------------------------------------------------------------------------
# Folder listing helpers
# ---------------------------------------------------------------------------

def _list_children(service, folder_id: str) -> list[dict]:
    """List all non-trashed children (files + folders) of a Drive folder."""
    items: list[dict] = []
    page_token = None
    while True:
        resp = _execute_with_retry(service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, md5Checksum, size)",
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

    Each result dict has keys: id, name, mimeType, md5Checksum, size,
    relative_path (e.g. 'Passaic County/report.pdf').
    Folders whose name is in skip_folder_names are not entered.
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
            results.append(f)


def _local_md5(path: Path) -> str:
    """Compute MD5 hex digest of a local file (for comparison with Drive md5Checksum)."""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Public listing (used by find-duplicates before any download)
# ---------------------------------------------------------------------------

def list_drive_pdfs(input_folder_id: str) -> list[dict]:
    """
    List all PDFs in the Drive input folder without downloading anything.

    Returns a list of dicts with keys: id, name, mimeType, md5Checksum,
    size, relative_path.
    """
    service = _get_service()
    results: list[dict] = []
    _list_pdfs_recursive(service, input_folder_id, "", results)
    return results


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _load_exclude_names() -> set[str]:
    """Load filenames from exclude.txt (shared with ingest loader)."""
    exclude_path = Path.cwd() / "exclude.txt"
    if not exclude_path.exists():
        return set()
    names: set[str] = set()
    for line in exclude_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.add(Path(line).name)
    return names


def download_new_pdfs(
    input_folder_id: str,
    local_dir: Path,
) -> list[dict]:
    """
    Download PDFs from the Drive input folder, preserving subfolder structure.

    Skips files in exclude.txt (duplicate detection output) and files that
    already exist locally with a matching MD5 checksum.

    Returns a list of dicts for each file (both downloaded and skipped):
      {id, name, relative_path, md5Checksum, local_path, skipped}
    """
    from googleapiclient.http import MediaIoBaseDownload

    service = _get_service()
    pdf_files: list[dict] = []
    _list_pdfs_recursive(service, input_folder_id, "", pdf_files)
    excluded = _load_exclude_names()

    all_files: list[dict] = []
    downloaded_count = 0
    skipped_count = 0
    excluded_count = 0

    for f in pdf_files:
        # Skip files in exclude.txt before downloading
        if f["name"] in excluded:
            _log.info("Excluding %s (in exclude.txt)", f["relative_path"])
            f["local_path"] = local_dir / f["relative_path"]
            f["skipped"] = True
            all_files.append(f)
            excluded_count += 1
            continue

        local_path = local_dir / f["relative_path"]
        f["local_path"] = local_path

        # Skip download if local copy exists with matching MD5
        drive_md5 = f.get("md5Checksum", "")
        if local_path.exists() and drive_md5:
            local_hash = _local_md5(local_path)
            if local_hash == drive_md5:
                f["skipped"] = True
                all_files.append(f)
                skipped_count += 1
                continue

        # Download the file (retry on transient network errors)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(_MAX_RETRIES):
            try:
                request = service.files().get_media(fileId=f["id"])
                with open(local_path, "wb") as fh:
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while not done:
                        _, done = downloader.next_chunk()
                break
            except (ConnectionError, OSError) as e:
                if attempt < _MAX_RETRIES - 1:
                    _log.warning("Download failed for %s (attempt %d/%d): %s — retrying in %ds...",
                                 f["relative_path"], attempt + 1, _MAX_RETRIES, e, _RETRY_DELAY)
                    time.sleep(_RETRY_DELAY)
                else:
                    _log.error("Download failed for %s after %d attempts: %s",
                               f["relative_path"], _MAX_RETRIES, e)
                    raise

        f["skipped"] = False
        all_files.append(f)
        downloaded_count += 1
        _log.info("Downloaded: %s", f["relative_path"])

    _log.info("Drive sync: %d downloaded, %d cached, %d excluded (of %d total)",
              downloaded_count, skipped_count, excluded_count, len(pdf_files))
    return all_files

"""
Google Drive integration — download PDFs from an input folder and upload CSVs to an output folder.

Uses a Google Cloud service account for authentication. Share both Drive folders
with the service account's email address (like sharing with a person).

Required environment variables:
  GDRIVE_INPUT_FOLDER_ID   — Drive folder ID where users drop PDFs
  GDRIVE_OUTPUT_FOLDER_ID  — Drive folder ID where CSVs are deposited
  GDRIVE_SERVICE_ACCOUNT_FILE — path to the JSON key file
"""

import io
import os
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _get_service():
    creds_file = os.environ.get(
        "GDRIVE_SERVICE_ACCOUNT_FILE", "credentials/service-account.json"
    )
    creds = service_account.Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def download_new_pdfs(drive_folder_id: str, local_dir: Path) -> list[str]:
    """
    Download any PDFs from the Drive folder (and subfolders) that aren't already in local_dir.

    Returns the list of newly downloaded filenames.
    """
    local_dir.mkdir(parents=True, exist_ok=True)
    existing = {f.name.lower() for f in local_dir.iterdir() if f.is_file()}

    service = _get_service()

    # Collect all PDFs recursively (searches subfolders)
    pdf_files = []
    _list_pdfs_recursive(service, drive_folder_id, pdf_files)

    downloaded = []
    for f in pdf_files:
        if f["name"].lower() in existing:
            continue
        request = service.files().get_media(fileId=f["id"])
        dest_path = local_dir / f["name"]
        with open(dest_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        downloaded.append(f["name"])

    return downloaded


def _list_pdfs_recursive(service, folder_id: str, results: list) -> None:
    """Recursively list all PDFs in a folder and its subfolders."""
    query = f"'{folder_id}' in parents and trashed=false"
    response = service.files().list(
        q=query, fields="files(id, name, mimeType)", pageSize=1000,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()

    for f in response.get("files", []):
        if f["mimeType"] == "application/pdf":
            results.append(f)
        elif f["mimeType"] == "application/vnd.google-apps.folder":
            _list_pdfs_recursive(service, f["id"], results)


def upload_csv(local_path: Path, drive_folder_id: str, overwrite_name: str | None = None) -> str:
    """
    Upload a CSV to the Drive output folder.

    If overwrite_name is provided, look for an existing file with that name in
    the folder and update it in place (for the master.csv pattern). Otherwise
    create a new file.

    Returns the Drive file ID.
    """
    service = _get_service()
    upload_name = overwrite_name or local_path.name

    # Check if a file with this name already exists in the folder
    existing_id = None
    if overwrite_name:
        query = f"'{drive_folder_id}' in parents and name='{overwrite_name}' and trashed=false"
        results = service.files().list(
            q=query, fields="files(id)",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        matches = results.get("files", [])
        if matches:
            existing_id = matches[0]["id"]

    media = MediaFileUpload(str(local_path), mimetype="text/csv")

    if existing_id:
        updated = service.files().update(
            fileId=existing_id, media_body=media, supportsAllDrives=True,
        ).execute()
        return updated["id"]
    else:
        metadata = {"name": upload_name, "parents": [drive_folder_id]}
        created = service.files().create(
            body=metadata, media_body=media, fields="id", supportsAllDrives=True,
        ).execute()
        return created["id"]

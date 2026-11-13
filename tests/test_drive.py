"""Tests for Drive integration with the Google client mocked."""

from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def mock_service():
    """Build a mock Google Drive service with common methods."""
    service = MagicMock()
    return service


def _make_file_entry(file_id, name, mime="application/pdf", parent_id="parent123"):
    return {
        "id": file_id,
        "name": name,
        "mimeType": mime,
        "relative_path": name,
        "parent_folder_id": parent_id,
    }


def test_list_pdfs_preserves_subfolder_path(mock_service):
    """PDFs in subfolders should have relative_path including the subfolder."""
    from njlead.drive import _list_pdfs_recursive

    # Root folder contains a subfolder "Passaic County"
    root_response = {
        "files": [
            {"id": "folder1", "name": "Passaic County", "mimeType": "application/vnd.google-apps.folder"},
            {"id": "file1", "name": "root_report.pdf", "mimeType": "application/pdf",
             "md5Checksum": "abc123", "size": "1000"},
        ],
        "nextPageToken": None,
    }
    # Subfolder contains a PDF
    sub_response = {
        "files": [
            {"id": "file2", "name": "sub_report.pdf", "mimeType": "application/pdf",
             "md5Checksum": "def456", "size": "2000"},
        ],
        "nextPageToken": None,
    }

    def list_side_effect(*args, **kwargs):
        mock_list = MagicMock()
        q = kwargs.get("q", "")
        if "root_folder" in q:
            mock_list.execute.return_value = root_response
        elif "folder1" in q:
            mock_list.execute.return_value = sub_response
        else:
            mock_list.execute.return_value = {"files": []}
        return mock_list

    mock_service.files().list.side_effect = list_side_effect

    results = []
    _list_pdfs_recursive(mock_service, "root_folder", "", results)

    paths = [r["relative_path"] for r in results]
    assert "root_report.pdf" in paths
    assert "Passaic County/sub_report.pdf" in paths


def test_list_pdfs_skips_folder(mock_service):
    """Folders in skip_folder_names should be excluded."""
    from njlead.drive import _list_pdfs_recursive

    root_response = {
        "files": [
            {"id": "proc_folder", "name": "Processed", "mimeType": "application/vnd.google-apps.folder"},
            {"id": "file1", "name": "new_report.pdf", "mimeType": "application/pdf",
             "md5Checksum": "abc", "size": "500"},
        ],
        "nextPageToken": None,
    }

    mock_list = MagicMock()
    mock_list.execute.return_value = root_response
    mock_service.files().list.return_value = mock_list

    results = []
    _list_pdfs_recursive(
        mock_service, "root_folder", "", results,
        skip_folder_names={"Processed"},
    )

    assert len(results) == 1
    assert results[0]["name"] == "new_report.pdf"


def test_md5_fields_requested(mock_service):
    """The files().list call should request md5Checksum for checksum-based skip."""
    from njlead.drive import _list_children

    mock_list = MagicMock()
    mock_list.execute.return_value = {"files": [], "nextPageToken": None}
    mock_service.files().list.return_value = mock_list

    _list_children(mock_service, "folder123")

    # Check that the fields parameter includes md5Checksum
    call_kwargs = mock_service.files().list.call_args
    fields = call_kwargs.kwargs.get("fields", "") if call_kwargs.kwargs else ""
    assert "md5Checksum" in fields


def test_readonly_scope():
    """The auth scope should be read-only."""
    from njlead.drive import SCOPES
    assert SCOPES == ["https://www.googleapis.com/auth/drive.readonly"]

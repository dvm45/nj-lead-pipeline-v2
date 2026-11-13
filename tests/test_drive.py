"""Tests for Drive integration with the Google client mocked."""

from unittest.mock import MagicMock, patch

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
            {"id": "file1", "name": "root_report.pdf", "mimeType": "application/pdf"},
        ],
        "nextPageToken": None,
    }
    # Subfolder contains a PDF
    sub_response = {
        "files": [
            {"id": "file2", "name": "sub_report.pdf", "mimeType": "application/pdf"},
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


def test_list_pdfs_skips_processed_folder(mock_service):
    """The Processed subfolder should be skipped when skip_folder_names includes it."""
    from njlead.drive import _list_pdfs_recursive

    root_response = {
        "files": [
            {"id": "proc_folder", "name": "Processed", "mimeType": "application/vnd.google-apps.folder"},
            {"id": "file1", "name": "new_report.pdf", "mimeType": "application/pdf"},
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


def test_move_to_processed_preserves_structure():
    """move_to_processed should create subfolder structure under Processed."""
    from njlead.drive import move_to_processed

    file_info = {
        "id": "file123",
        "relative_path": "Passaic County/report.pdf",
        "parent_folder_id": "county_folder",
    }

    with patch("njlead.drive._get_service") as mock_get:
        service = MagicMock()
        mock_get.return_value = service

        # _find_or_create_subfolder calls files().list then files().create
        # First call: find "Processed" folder -> return existing
        # Second call: find "Passaic County" under Processed -> create new
        list_mock = MagicMock()
        service.files().list.return_value = list_mock

        # Track folder creation order
        folder_ids = iter(["processed_id", "county_processed_id"])
        def list_execute(*args, **kwargs):
            return {"files": [{"id": next(folder_ids)}]}
        list_mock.execute.side_effect = list_execute

        update_mock = MagicMock()
        update_mock.execute.return_value = {}
        service.files().update.return_value = update_mock

        move_to_processed("input_folder_id", file_info)

        # The file should have been moved
        service.files().update.assert_called()

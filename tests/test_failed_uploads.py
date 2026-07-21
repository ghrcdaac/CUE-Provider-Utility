import pytest
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from cue_provider_utility.models import FailedUploadEntry, AppConfig
from cue_provider_utility.failed_uploads_handler import FailedUploadsManager, load_failed_uploads_container

@pytest.mark.asyncio
async def test_failed_uploads_manager_add_and_remove():
    with TemporaryDirectory() as tmp_dir:
        log_path = Path(tmp_dir) / "failed_uploads.json"

        entry1 = FailedUploadEntry(
            session_id="session-1",
            file_path=Path(tmp_dir) / "test1.nc",
            collection="TEST_COLLECTION",
            env="prod",
            file_size=1024,
            content_type="text/plain",
            checksum="dummy_checksum_1"
        )

        entry2 = FailedUploadEntry(
            session_id="session-2",
            file_path=Path(tmp_dir) / "test2.nc",
            collection="TEST_COLLECTION",
            env="uat",
            file_size=2048
        )

        # Add entries
        saved1 = await FailedUploadsManager.add_failed_entry(entry1, log_path)
        saved2 = await FailedUploadsManager.add_failed_entry(entry2, log_path)

        assert saved1.entry_id is not None
        assert saved1.timestamp is not None
        assert saved2.entry_id is not None

        # Verify all entries load
        all_entries = await FailedUploadsManager.get_failed_entries(log_path)
        assert len(all_entries) == 2

        # Verify filtering by session_id
        session1_entries = await FailedUploadsManager.get_failed_entries(log_path, session_id="session-1")
        assert len(session1_entries) == 1
        assert session1_entries[0].checksum == "dummy_checksum_1"

        # Remove an entry
        removed = await FailedUploadsManager.remove_failed_entry(saved1.entry_id, log_path)
        assert removed is True

        remaining = await FailedUploadsManager.get_failed_entries(log_path)
        assert len(remaining) == 1
        assert remaining[0].entry_id == saved2.entry_id

@pytest.mark.asyncio
async def test_manual_entry_without_ids():
    with TemporaryDirectory() as tmp_dir:
        log_path = Path(tmp_dir) / "failed_uploads.json"
        
        # Simulate a manually created raw JSON file without entry_id or timestamp
        json_content = """{
            "version": "1.0",
            "failed_uploads": [
                {
                    "file_path": "/tmp/manual_file.nc",
                    "collection": "MANUAL_COLLECTION",
                    "env": "prod",
                    "file_size": 500
                }
            ]
        }"""
        log_path.write_text(json_content, encoding="utf-8")

        container = await load_failed_uploads_container(log_path)
        assert len(container.failed_uploads) == 1
        manual_entry = container.failed_uploads[0]
        assert manual_entry.entry_id is not None
        assert manual_entry.timestamp is not None
        assert manual_entry.collection == "MANUAL_COLLECTION"

@pytest.mark.asyncio
async def test_get_mime_type_resolution():
    from cue_provider_utility.utils import get_mime_type
    
    assert await get_mime_type(Path("sample.nc")) == "application/x-netcdf"
    assert await get_mime_type(Path("sample.nc.gz")) == "application/x-netcdf"
    assert await get_mime_type(Path("sample.dat.gz")) == "application/gzip"
    assert await get_mime_type(Path("sample.h5")) == "application/x-hdf5"
    assert await get_mime_type(Path("sample.zip")) in ["application/zip", "application/x-zip-compressed"]


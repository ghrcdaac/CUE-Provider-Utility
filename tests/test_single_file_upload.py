import pytest
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock
from rich.progress import Progress, TaskID

from cue_provider_utility.api_client import ApiClient
from cue_provider_utility.models import AppConfig, GlobalArgs, PrepareSingleResponse, UploadCompletionResponse
from cue_provider_utility.single_file_uploader import handle_single_file_upload


@pytest.mark.asyncio
async def test_read_in_chunks_advances_progress_with_task_id_zero():
    """Verify that read_in_chunks correctly updates progress when task_id is 0."""
    with TemporaryDirectory() as tmp_dir:
        test_file = Path(tmp_dir) / "test.bin"
        file_size = 250 * 1024  # 250 KB
        test_file.write_bytes(b"A" * file_size)

        config = AppConfig()
        global_args = GlobalArgs()
        api_client = ApiClient(config=config, global_args=global_args, api_key="test_key")

        progress = Progress()
        task_id = progress.add_task("test", total=file_size)
        assert task_id == 0  # First task in Progress always has id 0

        chunks = []
        chunk_size = 64 * 1024
        async for chunk in api_client.read_in_chunks(test_file, chunk_size=chunk_size, progress=progress, task_id=task_id):
            chunks.append(chunk)

        assert b"".join(chunks) == b"A" * file_size
        assert progress.tasks[task_id].completed == file_size
        await api_client.close()


@pytest.mark.asyncio
async def test_handle_single_file_upload_streaming(httpx_mock):
    """Verify handle_single_file_upload streams file in chunks and calls complete_single_upload."""
    with TemporaryDirectory() as tmp_dir:
        test_file = Path(tmp_dir) / "test_file.dat"
        file_size = 500 * 1024  # 500 KB
        test_file.write_bytes(b"B" * file_size)

        config = AppConfig()
        global_args = GlobalArgs()
        api_client = ApiClient(config=config, global_args=global_args, api_key="test_key")

        presigned_url = "https://s3.amazonaws.com/test-bucket/test_file.dat?signature=fake"

        from uuid import uuid4
        mock_file_id = uuid4()

        # Mock API client methods
        api_client.prepare_single_upload = AsyncMock(return_value=PrepareSingleResponse(
            file_id=mock_file_id,
            presigned_url=presigned_url,
            fields=None
        ))
        api_client.complete_single_upload = AsyncMock(return_value=UploadCompletionResponse(
            message="Success",
            file_id=str(mock_file_id)
        ))

        # Mock S3 PUT endpoint
        httpx_mock.add_response(
            url=presigned_url,
            method="PUT",
            status_code=200,
            headers={"ETag": '"mock-etag-abc"'}
        )

        progress = Progress()
        task_id = progress.add_task("Upload test", total=file_size)
        assert task_id == 0

        checksum, mime_type = await handle_single_file_upload(
            file_path=test_file,
            file_size=file_size,
            collection="test_collection",
            target_sub_path=None,
            api_client=api_client,
            config=config,
            progress=progress,
            task_id=task_id
        )

        assert checksum is not None
        assert progress.tasks[task_id].completed == file_size
        assert api_client.prepare_single_upload.called
        assert api_client.complete_single_upload.called

        # Verify S3 request received full body
        s3_requests = httpx_mock.get_requests()
        assert len(s3_requests) == 1
        assert s3_requests[0].headers["content-length"] == str(file_size)

        await api_client.close()

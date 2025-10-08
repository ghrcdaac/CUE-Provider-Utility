# In cue_provider_utility/api_client.py

import httpx
import logging
import json
from typing import Optional, Dict, Any, List, AsyncGenerator
from urllib.parse import urljoin

from .models import (
    AppConfig, GlobalArgs,
    PrepareSingleRequest, PrepareSingleResponse,
    CompleteSingleRequest, UploadCompletionResponse,
    MultipartStartRequest, MultipartStartResponse,
    MultipartGetPartUrlRequest, MultipartGetPartUrlResponse,
    MultipartCompleteRequest,
    MultipartAbortRequest
)
from .exceptions import APIRequestError, ConfigError
from rich.progress import Progress
import aiofiles
from pathlib import Path

try:
    from . import __version__ as app_version
except ImportError:
    app_version = "0.1.0"

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
API_VERSION_PREFIX = "/v2"

async def log_request_details(request: httpx.Request):
    logger.info(f"--> HTTP Request: {request.method} {request.url}")
    headers_to_log = {k: (v if k.lower() != 'authorization' else 'Bearer [REDACTED]') for k, v in request.headers.items()}
    logger.info(f"     Headers: {headers_to_log}")
    
    content_type_header = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type_header:
        logger.info("     Body: [Streaming multipart/form-data for file upload]")
    else:
        try:
            body_content = request.content.decode('utf-8') if request.content else "''"
        except UnicodeDecodeError:
            body_content = f"[Binary content of length {len(request.content)} bytes]"
        logger.info(f"     Body: {body_content}")

async def log_response_details(response: httpx.Response):
    await response.aread()
    request = response.request
    logger.info(f"<-- HTTP Response for {request.method} {request.url}")
    logger.info(f"     Status: {response.status_code}")
    headers_to_log = {k: v for k, v in response.headers.items()}
    logger.info(f"     Headers: {headers_to_log}")
    try:
        body_content = response.content.decode('utf-8') if response.content else "''"
    except UnicodeDecodeError:
        body_content = f"[Binary content of length {len(response.content)} bytes]"
    logger.info(f"     Body: {body_content}")


class ApiClient:
    """Handles all communication with the CUE backend API."""
    
    # Define a default chunk size for streaming file reads
    CHUNK_SIZE = 1024 * 1024 * 4  # 4MB

    def __init__(self, config: AppConfig, global_args: GlobalArgs, api_key: str):
        self.config = config
        self.global_args = global_args
        self.api_key = api_key
        self.base_url_no_version = self._get_base_url_no_version()
        
        hooks = {}
        if self.global_args.verbose_level >= 2:
            hooks = {'request': [log_request_details], 'response': [log_response_details]}
        
        self.http_client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            event_hooks=hooks,
            http1=True
        )

  
    async def read_in_chunks(
        self,
        file_path: Path,
        chunk_size: int = CHUNK_SIZE,
        progress: Optional[Progress] = None,
        task_id: Optional[Any] = None
    ) -> AsyncGenerator[bytes, None]:
        """
        Asynchronously reads a file in chunks and yields them.
        This is a memory-efficient generator for streaming uploads.
        If a Rich progress bar and task_id are provided, it updates them.
        """
        async with aiofiles.open(file_path, "rb") as f:
            while True:
                chunk = await f.read(chunk_size)
                if not chunk:
                    break
                if progress and task_id:
                    progress.update(task_id, advance=len(chunk))
                yield chunk
   

    def _get_default_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": f"cue-upload-cli/{app_version}"
        }

    def _get_base_url_no_version(self) -> str:
        env = self.global_args.env_cli or self.config.default_env
        try:
            if env not in self.config.environments.model_fields:
                raise ConfigError(f"Environment '{env}' not defined in config.")
            base_url_obj = getattr(self.config.environments, env)
            base_url_str = str(base_url_obj)

            for version_suffix in ["/v1/", "/v1", "/v2/", "/v2"]:
                if base_url_str.endswith(version_suffix):
                    base_url_str = base_url_str[:-len(version_suffix)]
                    break

            if not base_url_str.endswith('/'):
                base_url_str += '/'
            
            return base_url_str
        except Exception as e:
            raise ConfigError(f"Could not determine base URL for environment '{env}': {e}")

    async def _request(
        self,
        method: str,
        versioned_endpoint_path: str,
        json_payload: Optional[Dict[str, Any]] = None,
        expected_status_codes: Optional[List[int]] = None,
        response_model: Optional[Any] = None,
        files: Optional[Dict[str, Any]] = None
    ) -> Any:
        if expected_status_codes is None:
            expected_status_codes = [200, 201, 204]

        full_url = urljoin(self.base_url_no_version, versioned_endpoint_path.lstrip('/'))

        headers = self._get_default_headers()
        request_content = None
        if json_payload and not files:
            headers["Content-Type"] = "application/json; charset=utf-8"
            request_content = json.dumps(json_payload).encode("utf-8")
        
        try:
            response = await self.http_client.request(
                method, full_url, headers=headers, content=request_content, files=files
            )

            if response.status_code not in expected_status_codes:
                error_content_text = response.text
                
                try:
                    error_json = response.json()
                    detail_message = error_json.get("detail")
                    if detail_message:
                        error_message = detail_message
                    else:
                        error_message = f"API Error (HTTP {response.status_code}): {error_content_text}"
                except json.JSONDecodeError:
                    error_message = f"API request failed with status {response.status_code} ({response.reason_phrase})."

                
                raise APIRequestError(
                    message=error_message, status_code=response.status_code, response_content=error_content_text
                )
            
            if response_model and response.status_code != 204:
                content_type = response.headers.get("content-type", "")
                if "application/json" not in content_type:
                    raise APIRequestError(
                        f"API returned unexpected content type '{content_type}' instead of 'application/json'. "
                        f"This often indicates a server routing error.",
                        status_code=response.status_code,
                        response_content=response.text
                    )
                return response_model.model_validate(response.json())

            return response
        
        except httpx.TimeoutException as e:
            msg = f"Request timed out connecting to {self.global_args.env_cli or self.config.default_env} environment."
            logger.error(msg, exc_info=self.global_args.verbose_level >= 2)
            raise APIRequestError(msg, original_exception=e)
        except httpx.RequestError as e:
            msg = f"A network connection error occurred: {e.__class__.__name__}"
            logger.error(msg, exc_info=self.global_args.verbose_level >= 2)
            raise APIRequestError(msg, original_exception=e)
        except Exception as e:
            msg = str(e)
            logger.error(f"An unexpected error occurred during the API request: {msg}", exc_info=self.global_args.verbose_level >= 2)
            raise APIRequestError(msg, original_exception=e)

    async def prepare_single_upload(self, payload: PrepareSingleRequest) -> PrepareSingleResponse:
        endpoint = f"{API_VERSION_PREFIX}/upload/prepare-single"
        return await self._request(
            "POST", endpoint, json_payload=payload.model_dump(),
            response_model=PrepareSingleResponse
        )

    async def complete_single_upload(self, payload: CompleteSingleRequest) -> UploadCompletionResponse:
        endpoint = f"{API_VERSION_PREFIX}/upload/complete-single"
        return await self._request(
            "POST", endpoint,
            json_payload=payload.model_dump(mode='json'),
            response_model=UploadCompletionResponse,
            expected_status_codes=[200]
        )

    async def start_multipart(self, payload: MultipartStartRequest) -> MultipartStartResponse:
        endpoint = f"{API_VERSION_PREFIX}/upload/multipart/start"
        return await self._request(
            "POST", endpoint, json_payload=payload.model_dump(),
            response_model=MultipartStartResponse
        )

    async def get_multipart_part_url(self, payload: MultipartGetPartUrlRequest) -> MultipartGetPartUrlResponse:
        endpoint = f"{API_VERSION_PREFIX}/upload/multipart/get-part-url"
        return await self._request(
            "POST", endpoint, json_payload=payload.model_dump(mode='json'),
            response_model=MultipartGetPartUrlResponse
        )

    async def complete_multipart(self, payload: MultipartCompleteRequest) -> UploadCompletionResponse:
        endpoint = f"{API_VERSION_PREFIX}/upload/multipart/complete"
        return await self._request(
            "POST", endpoint, json_payload=payload.model_dump(mode='json'),
            response_model=UploadCompletionResponse
        )

    async def abort_multipart(self, payload: MultipartAbortRequest) -> None:
        endpoint = f"{API_VERSION_PREFIX}/upload/multipart/abort"
        await self._request(
            "POST", endpoint, json_payload=payload.model_dump(mode='json'),
            expected_status_codes=[204]
        )

    async def upload_to_s3_presigned_put(self, url: str, file_data: bytes, content_length: int) -> httpx.Response:
        headers = {'Content-Length': str(content_length)}
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                response = await client.put(url, content=file_data, headers=headers)
            
            if response.status_code != 200:
                raise APIRequestError(f"S3 presigned PUT failed with status {response.status_code}.",
                                      status_code=response.status_code, response_content=response.text)
            return response
        except httpx.HTTPError as e:
            logger.error(f"S3 presigned PUT HTTP error: {e}")
            raise APIRequestError(f"S3 upload failed for {e}", original_exception=e)

    async def close(self):
        await self.http_client.aclose()
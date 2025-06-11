"""
Asynchronous HTTP client for interacting with the CUE backend API.
Handles making requests, processing responses, and error handling for API calls.
"""
import httpx
import logging
import json # Import json for manual encoding
from typing import Optional, Dict, Any, Union, List
from pathlib import Path
from urllib.parse import urljoin

from .models import (
    AppConfig, GlobalArgs,
    InitiateUploadRequest, InitiateUploadResponse,
    ConfirmSingleUploadRequest,
    MultipartStartRequest, MultipartStartResponse,
    MultipartGetPartUrlRequest, MultipartGetPartUrlResponse,
    MultipartCompleteRequest, MultipartCompleteResponse,
    MultipartAbortRequest,
    APIErrorResponse
)
from .exceptions import APIRequestError, ConfigError

try:
    from . import __version__ as app_version
except ImportError:
    app_version = "0.1.0"

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
API_VERSION_PREFIX = "/v1"

# Logging functions
async def log_request_details(request: httpx.Request):
    logger.info(f"--> HTTP Request: {request.method} {request.url}")
    headers_to_log = {k: (v if k.lower() != 'authorization' else 'Bearer [REDACTED]') for k, v in request.headers.items()}
    logger.info(f"    Headers: {headers_to_log}")
    
    content_type_header = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type_header:
        logger.info("    Body: [Streaming multipart/form-data for file upload]")
    else:
        try:
            body_content = request.content.decode('utf-8') if request.content else "''"
        except UnicodeDecodeError:
            body_content = f"[Binary content of length {len(request.content)} bytes]"
        logger.info(f"    Body: {body_content}")


async def log_response_details(response: httpx.Response):
    await response.aread()
    request = response.request
    logger.info(f"<-- HTTP Response for {request.method} {request.url}")
    logger.info(f"    Status: {response.status_code}")
    headers_to_log = {k: v for k, v in response.headers.items()}
    logger.info(f"    Headers: {headers_to_log}")
    try:
        body_content = response.content.decode('utf-8') if response.content else "''"
    except UnicodeDecodeError:
        body_content = f"[Binary content of length {len(response.content)} bytes]"
    logger.info(f"    Body: {body_content}")


class ApiClient:
    def __init__(self, config: AppConfig, global_args: GlobalArgs, auth_token: str):
        self.config = config
        self.global_args = global_args
        self.auth_token = auth_token
        self.base_url_no_version = self._get_base_url_no_version()
        # We will no longer set default headers here, but per request, for full control.
        self.http_client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            event_hooks={'request': [log_request_details], 'response': [log_response_details]}
        )

    # This method is no longer strictly needed as we set headers per request, but we can keep it.
    def _get_default_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.auth_token}",
            "Accept": "*/*",
            "User-Agent": "curl/7.81.0"
        }

    def _get_base_url_no_version(self) -> str:
        env = self.global_args.env_cli or self.config.default_env
        try:
            if env not in self.config.environments.model_fields:
                raise ConfigError(f"Environment '{env}' not defined in configuration environments.")
            base_url_obj = getattr(self.config.environments, env)
            base_url_str = str(base_url_obj)
            if base_url_str.endswith("/v1/"):
                base_url_str = base_url_str[:-len("/v1/")]
            elif base_url_str.endswith("/v1"):
                base_url_str = base_url_str[:-len("/v1")]
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
        response_model: Optional[Any] = None
    ) -> Any:
        if expected_status_codes is None:
            expected_status_codes = [200, 201, 204]

        full_url = urljoin(self.base_url_no_version, versioned_endpoint_path.lstrip('/'))

      
        # Manually construct headers and content to ensure exact Content-Type.
        headers = self._get_default_headers()
        request_content = None
        if json_payload:
            headers["Content-Type"] = "application/json; charset=utf-8"
            request_content = json.dumps(json_payload).encode("utf-8")
        
        try:
            # Pass manually constructed headers and content instead of using 'json='
            response = await self.http_client.request(
                method, full_url, headers=headers, content=request_content
            )

            if response.status_code not in expected_status_codes:
                error_content_text = response.text
                try:
                    api_error = APIErrorResponse.model_validate_json(response.content)
                    error_message = f"API Error: {api_error.error.message} (Code: {api_error.error.code or 'N/A'})"
                except Exception:
                    error_message = f"API request failed with status {response.status_code}."
                
                logger.error(f"{error_message} URL: {full_url}")
                raise APIRequestError(
                    message=error_message, status_code=response.status_code, response_content=error_content_text
                )

            if response_model:
                if response.status_code == 204:
                    return None
                return response_model.model_validate_json(response.content)
            return response
        except httpx.RequestError as e: 
            logger.error(f"Request error: {method} {full_url} - {e}")
            raise APIRequestError(f"An error occurred while requesting {full_url}: {e}", original_exception=e)
        except Exception as e: 
            logger.error(f"Unexpected error during API request or response processing: {method} {full_url} - {e}", exc_info=True)
            raise APIRequestError(f"An unexpected error occurred: {e}", original_exception=e)

    # The calling methods remain unchanged
    async def get_presigned_url_single(self, payload: InitiateUploadRequest) -> InitiateUploadResponse:
        endpoint = f"{API_VERSION_PREFIX}/upload/upload_url"
        return await self._request(
            "POST", endpoint, json_payload=payload.model_dump(), 
            response_model=InitiateUploadResponse
        )

    async def confirm_single_upload(self, payload: ConfirmSingleUploadRequest) -> None:
        endpoint = f"{API_VERSION_PREFIX}/upload/confirm_single"
        await self._request(
            "POST",
            endpoint,
            json_payload=payload.model_dump(), 
            expected_status_codes=[200, 201]
        )

    async def start_multipart_upload(self, payload: MultipartStartRequest) -> MultipartStartResponse:
        endpoint = f"{API_VERSION_PREFIX}/upload/multipart/start"
        return await self._request(
            "POST", endpoint, json_payload=payload.model_dump(), 
            response_model=MultipartStartResponse
        )

    async def get_presigned_url_for_part(self, payload: MultipartGetPartUrlRequest) -> MultipartGetPartUrlResponse:
        endpoint = f"{API_VERSION_PREFIX}/upload/multipart/get_part_url"
        return await self._request(
            "POST", endpoint, json_payload=payload.model_dump(), 
            response_model=MultipartGetPartUrlResponse
        )

    async def complete_multipart_upload(self, payload: MultipartCompleteRequest) -> MultipartCompleteResponse:
        endpoint = f"{API_VERSION_PREFIX}/upload/multipart/complete"
        return await self._request(
            "POST", endpoint, json_payload=payload.model_dump(), 
            response_model=MultipartCompleteResponse
        )

    async def abort_multipart_upload(self, payload: MultipartAbortRequest) -> None:
        endpoint = f"{API_VERSION_PREFIX}/upload/multipart/abort"
        await self._request(
            "POST", endpoint, json_payload=payload.model_dump(), 
            expected_status_codes=[204]
        )

    # S3 helpers do not need changes
    async def upload_to_s3_presigned_post(self, url: str, fields: Dict[str, str], file_path: Path, file_name: str, content_type: str) -> httpx.Response:
        with open(file_path, 'rb') as f:
            files_data = {'file': (file_name, f, content_type)}
            try:
                async with httpx.AsyncClient(timeout=None, event_hooks={'request': [log_request_details], 'response': [log_response_details]}) as client:
                    response = await client.post(url, data=fields, files=files_data)
                if response.status_code not in [200, 204]:
                    raise APIRequestError(
                        f"S3 presigned POST upload failed with status {response.status_code}.",
                        status_code=response.status_code, response_content=response.text
                    )
                return response
            except httpx.HTTPError as e:
                logger.error(f"S3 presigned POST HTTP error for {file_name}: {e}")
                raise APIRequestError(f"S3 upload failed for {file_name}: {e}", original_exception=e)

    async def upload_part_to_s3_presigned_put(self, url: str, part_data: bytes, content_length: int) -> httpx.Response:
        headers = {'Content-Length': str(content_length)}
        try:
            async with httpx.AsyncClient(timeout=None, event_hooks={'request': [log_request_details], 'response': [log_response_details]}) as client:
                response = await client.put(url, content=part_data, headers=headers)
            if response.status_code != 200:
                raise APIRequestError(
                    f"S3 presigned PUT for part upload failed with status {response.status_code}.",
                    status_code=response.status_code, response_content=response.text
                )
            return response
        except httpx.HTTPError as e:
            logger.error(f"S3 presigned PUT part HTTP error: {e}")
            raise APIRequestError(f"S3 part upload failed for {e}", original_exception=e)

    async def close(self):
        await self.http_client.aclose()

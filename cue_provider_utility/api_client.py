# In cue_provider_utility/api_client.py
# (This is the complete updated file content)

"""
Asynchronous HTTP client for interacting with the CUE backend API.
Handles making requests, processing responses, and error handling for API calls.
"""
import httpx
import logging
import json
from typing import Optional, Dict, Any, Union, List
from pathlib import Path
from urllib.parse import urljoin

# Import the new, V2-specific models
from .models import (
    AppConfig, GlobalArgs,
    PrepareSingleRequest, PrepareSingleResponse,
    S3CompletionData, UploadCompletionResponse,
    MultipartStartRequest, MultipartStartResponse,
    MultipartGetPartUrlRequest, MultipartGetPartUrlResponse,
    MultipartCompleteRequest,
    MultipartAbortRequest,
    APIErrorResponse,
    CompleteSingleRequest
)
from .exceptions import APIRequestError, ConfigError

try:
    from . import __version__ as app_version
except ImportError:
    app_version = "0.1.0"

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
# The API version prefix has been updated to '/v2'
API_VERSION_PREFIX = "/v2"

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
    # The constructor now takes 'api_key' instead of 'auth_token'.
    def __init__(self, config: AppConfig, global_args: GlobalArgs, api_key: str):
        self.config = config
        self.global_args = global_args
        # The auth token is now the API key
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

    def _get_default_headers(self) -> Dict[str, str]:
        # The Authorization header format is still 'Bearer'
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "*/*",
            "User-Agent": "curl/7.81.0"
        }

    def _get_base_url_no_version(self) -> str:
        env = self.global_args.env_cli or self.config.default_env
        try:
            if env not in self.config.environments.model_fields:
                raise ConfigError(f"Environment '{env}' not defined in config.")
            base_url_obj = getattr(self.config.environments, env)
            base_url_str = str(base_url_obj)

            # Update logic to handle both /v1/ and /v2/ suffixes
            if base_url_str.endswith("/v1/"):
                base_url_str = base_url_str[:-len("/v1/")]
            elif base_url_str.endswith("/v1"):
                base_url_str = base_url_str[:-len("/v1")]
            elif base_url_str.endswith("/v2/"):
                base_url_str = base_url_str[:-len("/v2/")]
            elif base_url_str.endswith("/v2"):
                base_url_str = base_url_str[:-len("/v2")]

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
        files: Optional[Dict[str, Any]] = None # New parameter for multipart/form-data
    ) -> Any:
        if expected_status_codes is None:
            expected_status_codes = [200, 201, 204]

        full_url = urljoin(self.base_url_no_version, versioned_endpoint_path.lstrip('/'))

        headers = self._get_default_headers()
        request_content = None
        if json_payload:
            headers["Content-Type"] = "application/json; charset=utf-8"
            request_content = json.dumps(json_payload).encode("utf-8")
        
        try:
            # Pass 'files' and 'content' appropriately
            response = await self.http_client.request(
                method, full_url, headers=headers, content=request_content, files=files
            )

            if response.status_code not in expected_status_codes:
                error_content_text = response.text
                try:
                    api_error = APIErrorResponse.model_validate_json(response.content)
                    error_message = f"API Error (HTTP {response.status_code}): {api_error.error.message}"
                except Exception:
                    error_message = f"API request failed with status {response.status_code} ({response.reason_phrase})"
                
                raise APIRequestError(
                    message=error_message, status_code=response.status_code, response_content=error_content_text
                )

            if response_model:
                if response.status_code == 204:
                    return None
                # The API returns JSON, so we use .json()
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

    # --- New V2 API Endpoints ---
    # The old methods are removed and replaced with the following.
    async def prepare_single_upload(self, payload: PrepareSingleRequest) -> PrepareSingleResponse:
        """Calls the POST /v2/upload/prepare-single endpoint."""
        endpoint = f"{API_VERSION_PREFIX}/upload/prepare-single"
        return await self._request(
            "POST", endpoint, json_payload=payload.model_dump(),
            response_model=PrepareSingleResponse
        )

    async def complete_single_upload(self, payload: CompleteSingleRequest) -> UploadCompletionResponse:
            """Calls POST /v2/upload/complete-single with a consolidated JSON body."""
            endpoint = f"{API_VERSION_PREFIX}/upload/complete-single"
            return await self._request(
                "POST", endpoint,
                json_payload=payload.model_dump(mode='json'), # Use model_dump for UUID serialization
                response_model=UploadCompletionResponse,
                expected_status_codes=[200] # The new endpoint returns 200 OK
            )

    async def start_multipart(self, payload: MultipartStartRequest) -> MultipartStartResponse:
        """Calls POST /v2/upload/multipart/start."""
        endpoint = f"{API_VERSION_PREFIX}/upload/multipart/start"
        return await self._request(
            "POST", endpoint, json_payload=payload.model_dump(),
            response_model=MultipartStartResponse
        )

    async def get_multipart_part_url(self, payload: MultipartGetPartUrlRequest) -> MultipartGetPartUrlResponse:
        """Calls POST /v2/upload/multipart/get-part-url."""
        endpoint = f"{API_VERSION_PREFIX}/upload/multipart/get-part-url"
        return await self._request(
            "POST", endpoint, json_payload=payload.model_dump(),
            response_model=MultipartGetPartUrlResponse
        )

    async def complete_multipart(self, payload: MultipartCompleteRequest) -> UploadCompletionResponse:
        """Calls POST /v2/upload/multipart/complete."""
        endpoint = f"{API_VERSION_PREFIX}/upload/multipart/complete"
        return await self._request(
            "POST", endpoint, json_payload=payload.model_dump(),
            response_model=UploadCompletionResponse
        )

    async def abort_multipart(self, payload: MultipartAbortRequest) -> None:
        """Calls POST /v2/upload/multipart/abort."""
        endpoint = f"{API_VERSION_PREFIX}/upload/multipart/abort"
        await self._request(
            "POST", endpoint, json_payload=payload.model_dump(),
            expected_status_codes=[200, 204] # The v2 docs specify a 200/204
        )

    # The `upload_to_s3_presigned_post` method is no longer needed for V2 single uploads, but
    # we'll keep it as a general utility.
    async def upload_to_s3_presigned_post(self, url: str, fields: Dict[str, str], file_path: Path, file_name: str, content_type: str) -> httpx.Response:
        with open(file_path, 'rb') as f:
            files_data = {'file': (file_name, f, content_type)}
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    response = await client.post(url, data=fields, files=files_data)
                
                if response.status_code not in [200, 204]:
                    raise APIRequestError(f"S3 presigned POST upload failed with status {response.status_code}.",
                                          status_code=response.status_code, response_content=response.text)
                return response
            except httpx.HTTPError as e:
                logger.error(f"S3 presigned POST HTTP error for {file_name}: {e}")
                raise APIRequestError(f"S3 upload failed for {file_name}: {e}", original_exception=e)

    # We will use this method for both single file PUTs and multipart PUTs
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
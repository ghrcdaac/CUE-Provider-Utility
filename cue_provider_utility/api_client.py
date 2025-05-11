"""
Asynchronous HTTP client for interacting with the CUE backend API.
Handles making requests, processing responses, and error handling for API calls.
"""
import httpx
import logging
from typing import Optional, Dict, Any, Union, List
from pathlib import Path
from urllib.parse import urljoin

from .models import (
    AppConfig, GlobalArgs,
    InitiateUploadRequest, InitiateUploadResponse,
    MultipartStartRequest, MultipartStartResponse,
    MultipartGetPartUrlRequest, MultipartGetPartUrlResponse,
    MultipartCompleteRequest, MultipartCompleteResponse, PartInfo,
    MultipartAbortRequest,
    APIErrorResponse # For parsing known error structures
)
from .exceptions import APIRequestError, ConfigError

logger = logging.getLogger(__name__)

# Default timeout for HTTP requests (seconds)
DEFAULT_TIMEOUT = 30.0 # For connect, read, write

class ApiClient:
    """
    An asynchronous client for the CUE Backend API.
    """
    def __init__(self, config: AppConfig, global_args: GlobalArgs, auth_token: str):
        self.config = config
        self.global_args = global_args
        self.auth_token = auth_token
        self.base_url = self._get_base_url()
        self.http_client = httpx.AsyncClient(
            headers=self._get_default_headers(),
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True
        )

    def _get_default_headers(self) -> Dict[str, str]:
        """Constructs default headers for API requests."""
        return {
            "Authorization": f"Bearer {self.auth_token}",
            "Accept": "application/json",
            "User-Agent": f"CUE-Provider-Utility/{self.config.model_fields['version'].default if hasattr(self.config, 'version') else '0.1.0'}" # TODO: Get version properly
        }

    def _get_base_url(self) -> str:
        """Determines the base URL for the API based on the selected environment."""
        env = self.global_args.env_cli or self.config.default_env
        try:
            # Ensure env is a valid key in the environments model
            if env not in self.config.environments.model_fields:
                raise ConfigError(f"Environment '{env}' not defined in configuration environments.")
            
            # Access the URL, Pydantic ensures it's an HttpUrl object
            base_url_obj = getattr(self.config.environments, env)
            return str(base_url_obj) # Convert HttpUrl to string
        except AttributeError:
            raise ConfigError(f"Environment URL for '{env}' not found in configuration.")
        except Exception as e:
            raise ConfigError(f"Could not determine base URL for environment '{env}': {e}")

    async def _request(
        self,
        method: str,
        endpoint: str,
        json_payload: Optional[Dict[str, Any]] = None,
        expected_status_codes: Optional[List[int]] = None,
        response_model: Optional[Any] = None # Pydantic model for response parsing
    ) -> Any: # Returns parsed Pydantic model or raw response content
        """Generic asynchronous request handler."""
        if expected_status_codes is None:
            expected_status_codes = [200, 201, 204] # Common success codes

        full_url = urljoin(self.base_url, endpoint.lstrip('/'))
        logger.debug(f"Request: {method} {full_url}")
        if json_payload:
            logger.debug(f"Payload: {json_payload}")

        try:
            response = await self.http_client.request(
                method,
                full_url,
                json=json_payload,
                # headers will include default auth token
            )
            logger.debug(f"Response: {response.status_code} {response.reason_phrase}")
            
            if response.status_code not in expected_status_codes:
                # Try to parse a known API error structure
                error_content = response.text
                try:
                    api_error = APIErrorResponse.model_validate_json(response.content)
                    error_message = f"API Error: {api_error.error.message} (Code: {api_error.error.code or 'N/A'})"
                except Exception:
                    error_message = f"API request failed with status {response.status_code}."
                
                logger.error(f"{error_message} URL: {full_url}, Response: {error_content[:500]}") # Log first 500 chars
                raise APIRequestError(
                    message=error_message,
                    status_code=response.status_code,
                    response_content=error_content
                )

            if response_model:
                if response.status_code == 204: # No content
                    return None
                return response_model.model_validate_json(response.content)
            
            return response # Or response.json() if always expecting JSON

        except httpx.TimeoutException as e:
            logger.error(f"Request timed out: {method} {full_url} - {e}")
            raise APIRequestError(f"Request to {full_url} timed out.", original_exception=e)
        except httpx.RequestError as e:
            logger.error(f"Request error: {method} {full_url} - {e}")
            raise APIRequestError(f"An error occurred while requesting {full_url}: {e}", original_exception=e)
        except Exception as e: # Catch Pydantic validation errors or other unexpected issues
            logger.error(f"Unexpected error during API request: {method} {full_url} - {e}")
            raise APIRequestError(f"An unexpected error occurred: {e}", original_exception=e)

    # --- Single File Upload Endpoints ---
    async def get_presigned_url_single(self, payload: InitiateUploadRequest) -> InitiateUploadResponse:
        """Gets a presigned URL for a single file upload."""
        # Endpoint path from original PDF: /v1/upload/upload_url
        # Our base_url already includes /v1, so endpoint is relative to that.
        endpoint = "upload/upload_url" 
        return await self._request(
            "POST",
            endpoint,
            json_payload=payload.model_dump(exclude_none=True),
            response_model=InitiateUploadResponse
        )

    # --- Multipart Upload Endpoints ---
    async def start_multipart_upload(self, payload: MultipartStartRequest) -> MultipartStartResponse:
        """Initiates a multipart upload with the backend."""
        endpoint = "multipart/start"
        return await self._request(
            "POST",
            endpoint,
            json_payload=payload.model_dump(exclude_none=True),
            response_model=MultipartStartResponse
        )

    async def get_presigned_url_for_part(self, payload: MultipartGetPartUrlRequest) -> MultipartGetPartUrlResponse:
        """Gets a presigned URL for uploading a specific part."""
        endpoint = "multipart/get_part_url"
        return await self._request(
            "POST",
            endpoint,
            json_payload=payload.model_dump(exclude_none=True),
            response_model=MultipartGetPartUrlResponse
        )

    async def complete_multipart_upload(self, payload: MultipartCompleteRequest) -> MultipartCompleteResponse:
        """Finalizes a multipart upload."""
        endpoint = "multipart/complete"
        return await self._request(
            "POST",
            endpoint,
            json_payload=payload.model_dump(exclude_none=True), # Ensure PartInfo is correctly serialized
            response_model=MultipartCompleteResponse
        )

    async def abort_multipart_upload(self, payload: MultipartAbortRequest) -> None:
        """Aborts a multipart upload."""
        endpoint = "multipart/abort"
        await self._request(
            "POST",
            endpoint,
            json_payload=payload.model_dump(exclude_none=True),
            expected_status_codes=[204] # Expecting No Content on successful abort
        )

    # --- S3 Direct Upload Helpers (using httpx directly) ---
    async def upload_to_s3_presigned_post(self, url: str, fields: Dict[str, str], file_path: Path, file_name: str, content_type: str) -> httpx.Response:
        """Uploads a file to an S3 presigned POST URL."""
        # S3 presigned POST expects multipart/form-data
        # The 'file' part must be the last field in the form.
        # Order of fields can matter for S3.
        
        # Create files dict for httpx
        # Note: httpx might not strictly enforce 'file' being last when constructing the multipart.
        # If issues arise, manual construction of MultipartEncoder might be needed.
        files_data = {'file': (file_name, open(file_path, 'rb'), content_type)}
        
        logger.debug(f"S3 POST: URL={url}, Fields={fields.keys()}, File={file_name}")
        try:
            async with httpx.AsyncClient(timeout=None) as client: # Use a longer/None timeout for file uploads
                response = await client.post(url, data=fields, files=files_data)
            
            logger.debug(f"S3 POST Response: {response.status_code} {response.reason_phrase}")
            if response.status_code not in [200, 204]: # S3 typically returns 204 on successful POST
                raise APIRequestError(
                    f"S3 presigned POST upload failed with status {response.status_code}.",
                    status_code=response.status_code,
                    response_content=response.text
                )
            return response
        except httpx.HTTPError as e:
            logger.error(f"S3 presigned POST HTTP error for {file_name}: {e}")
            raise APIRequestError(f"S3 upload failed for {file_name}: {e}", original_exception=e)


    async def upload_part_to_s3_presigned_put(self, url: str, part_data: bytes, content_length: int) -> httpx.Response:
        """Uploads a part's data to an S3 presigned PUT URL."""
        headers = {
            'Content-Length': str(content_length),
            # 'Content-Type' should NOT be included if not part of presigned URL signature (as per original PDF)
            # 'Content-MD5' or 'x-amz-content-sha256' might be needed if signed, but usually handled by S3 check if URL was signed with it.
        }
        logger.debug(f"S3 PUT Part: URL ending ...{url[-50:]}, Size={content_length}")
        try:
            async with httpx.AsyncClient(timeout=None) as client: # Long timeout for part uploads
                 response = await client.put(url, content=part_data, headers=headers)

            logger.debug(f"S3 PUT Part Response: {response.status_code} {response.reason_phrase}, ETag: {response.headers.get('ETag')}")
            if response.status_code != 200: # S3 PUT returns 200 on success
                raise APIRequestError(
                    f"S3 presigned PUT for part upload failed with status {response.status_code}.",
                    status_code=response.status_code,
                    response_content=response.text
                )
            return response
        except httpx.HTTPError as e:
            logger.error(f"S3 presigned PUT part HTTP error: {e}")
            raise APIRequestError(f"S3 part upload failed: {e}", original_exception=e)


    async def close(self):
        """Closes the underlying HTTP client."""
        await self.http_client.aclose()


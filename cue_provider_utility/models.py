"""
Pydantic models for configuration, API requests/responses, and internal data structures.
"""
from pydantic import BaseModel, Field, HttpUrl, FilePath, DirectoryPath, field_validator, field_serializer # type: ignore
from typing import List, Optional, Dict, Any
from pathlib import Path

# --- Configuration Models ---

class EnvironmentURLs(BaseModel):
    prod: HttpUrl = Field(default="https://upload.earthdata.nasa.gov/api/v1/") 
    uat: HttpUrl = Field(default="https://upload.uat.earthdata.nasa.gov/api/v1/") 
    sit: HttpUrl = Field(default="https://upload.sit.earthdata.nasa.gov/api/v1/") 
    local: HttpUrl = Field(default="http://localhost:8000/v1/") 

    @field_serializer('prod', 'uat', 'sit', 'local', when_used='json-unless-none')
    def serialize_urls_to_str(self, v: HttpUrl) -> str:
        return str(v)

class AppConfig(BaseModel):
    api_token: Optional[str] = None
    default_env: str = Field(default="prod", pattern=r"^(prod|uat|sit|local)$")
    
    multipart_threshold_gb: int = Field(default=1, gt=0)
    multipart_chunk_size_mb: int = Field(default=256, gt=0) 
    
    retry_attempts: int = Field(default=3, ge=0)
    
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    log_file_directory: Path = Field(default=Path.home() / ".cue-upload" / "logs")
    
    file_concurrency: int = Field(default=4, ge=1)
    part_concurrency: int = Field(default=4, ge=1)
    
    environments: EnvironmentURLs = Field(default_factory=EnvironmentURLs)
    user_ignored_patterns: List[str] = Field(default_factory=list)

    @field_validator('log_file_directory', mode='before') 
    def _validate_log_dir(cls, value: Any) -> Path:
        return Path(value).expanduser()
    
    @field_serializer('log_file_directory', when_used='json-unless-none')
    def serialize_path_to_str(self, v: Path) -> str:
        return str(v)

    class Config:
        validate_assignment = True

# --- CLI Context Object ---
class GlobalArgs(BaseModel):
    token_cli: Optional[str] = None
    env_cli: Optional[str] = None
    config_path_override: Optional[Path] = None
    log_file_override: Optional[Path] = None
    verbose_level: int = 0
    quiet_mode: bool = False
    config: AppConfig = Field(default_factory=AppConfig) 

# --- API Models (Client-side definitions) ---

class InitiateUploadRequest(BaseModel): # For single file presigned URL request
    file_name: str
    collection: str # Collection short_name
    size: int 
    checksum: str # Base64 SHA256
    file_type: str 
    collection_path: Optional[str] = None # User's target sub-path within the collection

class InitiateUploadResponse(BaseModel): # From backend for single file presigned URL
    url: HttpUrl 
    fields: Optional[Dict[str, str]] = None 
    s3_key: str # The S3 object key the backend has generated for this upload
    
    @field_serializer('url', when_used='json-unless-none')
    def serialize_url_to_str(self, v: HttpUrl) -> str:
        return str(v)

# NEW: For client to confirm single upload to backend
class ConfirmSingleUploadRequest(BaseModel):
    s3_key: str # The s3_key received from InitiateUploadResponse
    file_name: str # Original local filename
    collection: str # Collection short_name (for backend to re-validate if needed)
    size_bytes: int
    checksum: str # Base64 SHA256 of the uploaded file
    file_type: str # MIME type
    collection_path: Optional[str] = None # User's target sub-path, same as in InitiateUploadRequest
    s3_etag: Optional[str] = None # Optional: ETag from S3 response, if backend wants to verify


class MultipartStartRequest(BaseModel): 
    file_name: str # Original local filename
    collection: str # Collection short_name
    upload_target: Optional[str] = None # User's target sub-path within the collection
    content_type: str 
    overall_checksum: str # Base64 SHA256 of the entire file

class MultipartStartResponse(BaseModel): 
    upload_id: str # S3 UploadId
    s3_key: str    # The S3 object key the backend has generated

class MultipartGetPartUrlRequest(BaseModel):
    upload_id: str
    part_number: int
    file_name: str # This is the s3_key from MultipartStartResponse (as per backend expectation)
    collection: str # Collection short_name
    checksum: str # Part's Base64 SHA256
    content_type: str # Original file's MIME type

class MultipartGetPartUrlResponse(BaseModel):
    presigned_url: HttpUrl
    @field_serializer('presigned_url', when_used='json-unless-none')
    def serialize_url_to_str(self, v: HttpUrl) -> str:
        return str(v)

class PartInfo(BaseModel): 
    PartNumber: int
    ETag: str
    ChecksumSHA256: Optional[str] = None # As per client's MultipartCompleteRequest to backend

class MultipartCompleteRequest(BaseModel):
    upload_id: str
    parts: List[PartInfo]
    s3_key: str # The s3_key from MultipartStartResponse
    file_name: str # Original local filename (sent for backend DB record)
    collection: str # Collection short_name
    checksum: str # Overall file's Base64 SHA256 
    final_file_size: int # Final size of the assembled file in bytes
    collection_path: Optional[str] = None # User's target sub-path, same as in MultipartStartRequest's upload_target
    content_type: str # Original file's MIME type (for backend DB record)


class MultipartCompleteResponse(BaseModel): # From backend after successful completion
    Location: HttpUrl # S3 Location
    Bucket: str
    Key: str # This is the s3_key
    ETag: str # Final ETag of the assembled object
    # file_id: str # Optional: DB file ID if backend sends it back

    @field_serializer('Location', when_used='json-unless-none')
    def serialize_url_to_str(self, v: HttpUrl) -> str:
        return str(v)

class MultipartAbortRequest(BaseModel):
    upload_id: str
    s3_key: str 
    file_name: str # The s3_key (for backend compatibility)
    collection: str

class APIErrorDetail(BaseModel):
    message: str
    code: Optional[str] = None

class APIErrorResponse(BaseModel):
    error: APIErrorDetail

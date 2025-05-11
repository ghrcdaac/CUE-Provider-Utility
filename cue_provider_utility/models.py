"""
Pydantic models for configuration, API requests/responses, and internal data structures.
"""
from pydantic import BaseModel, Field, HttpUrl, FilePath, DirectoryPath, validator, field_serializer
from typing import List, Optional, Dict, Any
from pathlib import Path

# --- Configuration Models ---

class EnvironmentURLs(BaseModel):
    prod: HttpUrl = Field(default="https://upload.earthdata.nasa.gov/api/v1/")
    uat: HttpUrl = Field(default="https://upload.uat.earthdata.nasa.gov/api/v1/")
    sit: HttpUrl = Field(default="https://upload.sit.earthdata.nasa.gov/api/v1/")
    local: HttpUrl = Field(default="http://localhost:8000/v1/")
    
    # Explicitly serialize HttpUrl fields to strings when dumping to JSON-like structures
    @field_serializer('prod', 'uat', 'sit', 'local', when_used='json-unless-none')
    def serialize_urls_to_str(self, v: HttpUrl) -> str:
        return str(v)


class AppConfig(BaseModel):
    api_token: Optional[str] = None
    default_env: str = Field(default="prod", pattern=r"^(prod|uat|sit|local)$")
    
    multipart_threshold_gb: int = Field(default=1, gt=0)
    multipart_chunk_size_mb: int = Field(default=256, gt=0) # S3 part min is 5MB
    
    retry_attempts: int = Field(default=3, ge=0)
    
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    log_file_directory: Path = Field(default=Path.home() / ".cue-upload" / "logs")
    
    file_concurrency: int = Field(default=4, ge=1)
    part_concurrency: int = Field(default=4, ge=1)
    
    environments: EnvironmentURLs = Field(default_factory=EnvironmentURLs)
    user_ignored_patterns: List[str] = Field(default_factory=list)

    @validator('log_file_directory', pre=True, always=True)
    def _validate_log_dir(cls, value):
        # Expand user tilde
        return Path(value).expanduser()

    class Config:
        validate_assignment = True # Ensure validators run on assignment too

# --- CLI Context Object ---
class GlobalArgs(BaseModel):
    """Holds global CLI arguments and resolved configuration."""
    token_cli: Optional[str] = None
    env_cli: Optional[str] = None
    config_path_override: Optional[Path] = None
    log_file_override: Optional[Path] = None
    verbose_level: int = 0
    quiet_mode: bool = False
    config: AppConfig = Field(default_factory=AppConfig) # Will be populated after initial parsing

# --- API Models (Placeholders - to be defined based on actual backend API spec) ---

# Example for initiating an upload (single file)
class InitiateUploadRequest(BaseModel):
    file_name: str
    collection: str
    size: int # bytes
    checksum: str # Base64 encoded SHA256
    file_type: str # MIME type
    collection_path: Optional[str] = None # Target sub-path

class InitiateUploadResponse(BaseModel):
    url: HttpUrl # Presigned URL for S3 PUT/POST
    fields: Optional[Dict[str, str]] = None # For S3 presigned POST
    # For S3 presigned PUT, this might be empty and URL is self-contained
    # Or specific headers might be needed, or an upload_id for backend tracking

# Example for multipart start
class MultipartStartRequest(BaseModel):
    file_name: str
    collection: str
    upload_target: Optional[str] = None # User's target sub-path
    content_type: str # MIME type of original file

class MultipartStartResponse(BaseModel):
    upload_id: str # Backend's internal upload ID or S3's UploadId
    s3_key: str # The final S3 object key (or base key)

# Example for getting part URL
class MultipartGetPartUrlRequest(BaseModel):
    upload_id: str
    part_number: int
    # file_name: str # Or s3_key from MultipartStartResponse
    s3_key: str
    collection: str
    checksum: str # Part's Base64 SHA256
    content_type: str # MIME type of original file

class MultipartGetPartUrlResponse(BaseModel):
    presigned_url: HttpUrl

# Example for completing multipart
class PartInfo(BaseModel):
    PartNumber: int
    ETag: str
    ChecksumSHA256: Optional[str] = None # As per original PDF

class MultipartCompleteRequest(BaseModel):
    upload_id: str
    parts: List[PartInfo]
    # file_name: str # Original filename
    s3_key: str
    collection: str
    checksum: str # Entire file's Base64 SHA256

class MultipartCompleteResponse(BaseModel):
    # Based on S3's response, e.g.
    Location: HttpUrl
    Bucket: str
    Key: str
    ETag: str

# Example for aborting multipart
class MultipartAbortRequest(BaseModel):
    upload_id: str
    # file_name: str
    s3_key: str
    collection: str

# General API error response (example)
class APIErrorDetail(BaseModel):
    message: str
    code: Optional[str] = None

class APIErrorResponse(BaseModel):
    error: APIErrorDetail

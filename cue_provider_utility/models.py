# In cue_provider_utility/models.py
# (This is the complete updated file content)

from pydantic import BaseModel, Field, HttpUrl, FilePath, DirectoryPath, field_validator, field_serializer
from typing import List, Optional, Dict, Any
from uuid import UUID
from pathlib import Path

# --- Configuration Models ---

class EnvironmentURLs(BaseModel):
    prod: HttpUrl = Field(default="https://upload.earthdata.nasa.gov/api")
    uat: HttpUrl = Field(default="https://upload.uat.earthdata.nasa.gov/api")
    sit: HttpUrl = Field(default="https://upload.sit.earthdata.nasa.gov/api")
    local: HttpUrl = Field(default="http://localhost:8000")

    @field_serializer('prod', 'uat', 'sit', 'local', when_used='json-unless-none')
    def serialize_urls_to_str(self, v: HttpUrl) -> str:
        return str(v)

class AppConfig(BaseModel):
    # Field changed from 'api_token' to 'api_key' to match the new authentication scheme
    api_key: Optional[str] = None
    default_env: str = Field(default="prod", pattern=r"^(prod|uat|sit|local)$")
    
    multipart_threshold_gb: int = Field(default=1, gt=0)
    multipart_chunk_size_mb: int = Field(default=256, gt=0)
    
    # Enforce a max retry limit of 5.
    retry_attempts: int = Field(default=3, ge=0, le=5)
    
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    log_file_directory: Path = Field(default=Path.home() / ".cue-upload" / "logs")
    
    file_concurrency: int = Field(default=4, ge=1)
    part_concurrency: int = Field(default=4, ge=1)
    
    environments: EnvironmentURLs = Field(default_factory=EnvironmentURLs)
    user_ignored_patterns: List[str] = Field(default_factory=list)

    # Add MIME type validation controls.
    # List of disallowed MIME types that will always be rejected.
    denied_mime_types: List[str] = Field(default_factory=lambda: [
        "application/x-dosexec", # Windows .exe/.dll
        "application/x-mach-binary", # MacOS executable
        "application/x-elf", # Linux executable
    ])
    # Optional list of allowed MIME types. If empty, all types not in denied_mime_types are allowed.
    # If populated, only MIME types on this list will be allowed (after checking against the deny list).
    allowed_mime_types: Optional[List[str]] = None

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
    # Field changed from 'token_cli' to 'api_key_cli' to match the new authentication scheme
    api_key_cli: Optional[str] = None
    env_cli: Optional[str] = None
    config_path_override: Optional[Path] = None
    log_file_override: Optional[Path] = None
    verbose_level: int = 0
    quiet_mode: bool = False
    config: AppConfig = Field(default_factory=AppConfig)

# --- API Models ---


class PrepareSingleRequest(BaseModel):
    collection_name: str
    file_name: str
    file_size_bytes: int
    checksum: str
    collection_path: Optional[str] = None
    content_type: str

class PrepareSingleResponse(BaseModel):
    file_id: UUID
    presigned_url: HttpUrl
    @field_serializer('presigned_url', when_used='json-unless-none')
    def serialize_url_to_str(self, v: HttpUrl) -> str:
        return str(v)

class CompleteSingleRequest(BaseModel):
    """A single, consolidated model for the 'complete' step."""
    file_id: UUID
    collection_name: str
    file_name: str
    file_size_bytes: int
    checksum: str
    collection_path: Optional[str] = None
    content_type: str
    s3_etag: str

class UploadCompletionResponse(BaseModel):
    file_id: UUID
    status: str
    message: str

class MultipartStartRequest(BaseModel):
    collection_name: str
    file_name: str
    content_type: str
    collection_path: Optional[str] = None

class MultipartStartResponse(BaseModel):
    file_id: UUID
    upload_id: str

class MultipartGetPartUrlRequest(BaseModel):
    file_id: UUID
    upload_id: str
    part_number: int

class MultipartGetPartUrlResponse(BaseModel):
    presigned_url: HttpUrl
    @field_serializer('presigned_url', when_used='json-unless-none')
    def serialize_url_to_str(self, v: HttpUrl) -> str:
        return str(v)

class PartInfo(BaseModel):
    PartNumber: int
    ETag: str

class MultipartCompleteRequest(BaseModel):
    file_id: UUID
    upload_id: str
    parts: List[PartInfo]
    file_name: str
    collection_name: str
    collection_path: Optional[str] = None
    content_type: str
    checksum: str
    final_file_size: int

class MultipartAbortRequest(BaseModel):
    file_id: UUID
    upload_id: str

class APIErrorDetail(BaseModel):
    message: str
    code: Optional[str] = None

class APIErrorResponse(BaseModel):
    error: APIErrorDetail
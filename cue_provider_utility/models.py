"""
Pydantic models for configuration, API requests/responses, and internal data structures.
"""
from pydantic import BaseModel, Field, HttpUrl, FilePath, DirectoryPath, field_validator, field_serializer
from typing import List, Optional, Dict, Any
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

class InitiateUploadRequest(BaseModel):
    file_name: str
    collection: str
    size: int
    checksum: str
    file_type: str
    collection_path: Optional[str] = None

class InitiateUploadResponse(BaseModel):
    url: HttpUrl
    fields: Optional[Dict[str, str]] = None
    s3_key: str

    @field_serializer('url', when_used='json-unless-none')
    def serialize_url_to_str(self, v: HttpUrl) -> str:
        return str(v)


class ConfirmSingleUploadRequest(BaseModel):
    s3_key: str
    file_name: str
    collection: str
    size_bytes: int
    checksum: str
    file_type: str
    collection_path: Optional[str] = None
    s3_etag: Optional[str] = None


class MultipartStartRequest(BaseModel):
    file_name: str
    collection: str
    upload_target: Optional[str] = None
    content_type: str
    overall_checksum: str

class MultipartStartResponse(BaseModel):
    upload_id: str
    s3_key: str

class MultipartGetPartUrlRequest(BaseModel):
    upload_id: str
    part_number: int
    file_name: str # This is the s3_key from the backend's perspective for this request
    collection: str
    checksum: str
    content_type: str

class MultipartGetPartUrlResponse(BaseModel):
    presigned_url: HttpUrl
    @field_serializer('presigned_url', when_used='json-unless-none')
    def serialize_url_to_str(self, v: HttpUrl) -> str:
        return str(v)

class PartInfo(BaseModel):
    PartNumber: int
    ETag: str
    ChecksumSHA256: Optional[str] = None

class MultipartCompleteRequest(BaseModel):
    upload_id: str
    parts: List[PartInfo]
    s3_key: str
    file_name: str
    collection: str
    checksum: str
    final_file_size: int
    collection_path: Optional[str] = None
    content_type: str


class MultipartCompleteResponse(BaseModel): # From backend after successful completion
    Location: HttpUrl
    Bucket: str
    Key: str
    ETag: str


    @field_serializer('Location', when_used='json-unless-none')
    def serialize_url_to_str(self, v: HttpUrl) -> str:
        return str(v)

class MultipartAbortRequest(BaseModel):
    upload_id: str
    s3_key: str
    file_name: str # This is the s3_key from the backend's perspective for this request
    collection: str

class APIErrorDetail(BaseModel):
    message: str
    code: Optional[str] = None

class APIErrorResponse(BaseModel):
    error: APIErrorDetail

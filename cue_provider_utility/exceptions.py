"""
Custom exceptions for the CUE Provider Utility.
"""
from typing import Optional

class CUEProviderError(Exception):
    """Base exception for all application-specific errors."""
    def __init__(self, message: str, original_exception: Optional[Exception] = None): # <<< Optional used here
        super().__init__(message)
        self.original_exception = original_exception

class ConfigError(CUEProviderError):
    """Errors related to configuration loading or validation."""
    pass

class AuthError(CUEProviderError):
    """Errors related to authentication or token handling."""
    pass

class APIRequestError(CUEProviderError):
    """Errors occurring during API requests to the backend."""
    def __init__(self, message: str, status_code: Optional[int] = None, response_content: Optional[str] = None, original_exception: Optional[Exception] = None): # <<< Optional used here
        super().__init__(message, original_exception)
        self.status_code = status_code
        self.response_content = response_content

class FileProcessingError(CUEProviderError):
    """Errors related to local file processing (reading, hashing, etc.)."""
    pass

class UploadError(CUEProviderError):
    """General errors during the upload process not covered by more specific exceptions."""
    pass

class IgnoredFileError(CUEProviderError):
    """Errors specific to handling ignored file patterns."""
    pass

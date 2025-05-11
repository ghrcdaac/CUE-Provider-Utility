"""
Custom exceptions for the CUE Provider Utility.
"""

class CUEProviderError(Exception):
    """Base exception for all application-specific errors."""
    def __init__(self, message: str, original_exception: Exception | None = None):
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
    def __init__(self, message: str, status_code: int | None = None, response_content: str | None = None, original_exception: Exception | None = None):
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

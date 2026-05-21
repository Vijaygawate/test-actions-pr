"""Input Handler component for file upload and URL validation."""

import os
import shutil
import hashlib
import requests
from pathlib import Path
from typing import Optional
from fastapi import UploadFile
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding

from app.config import settings
from app.models.input_models import InputType
from app.models.validation_models import InputValidationResult, URLValidationResult


class InputHandler:
    """Handles input validation and temporary storage for scan requests."""
    
    # Supported file extensions for different input types
    CODEBASE_EXTENSIONS = {'.zip'}
    DEPENDENCY_EXTENSIONS = {
        '.txt',  # requirements.txt
        '.json',  # package.json
        '.xml',  # pom.xml
        '.lock'  # Gemfile.lock
    }
    DEPENDENCY_FILENAMES = {
        'requirements.txt',
        'package.json',
        'pom.xml',
        'gemfile',
        'gemfile.lock'
    }
    
    def __init__(self):
        """Initialize the Input Handler."""
        self.max_size_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        self.temp_storage_path = Path(settings.TEMP_STORAGE_PATH)
        self.temp_storage_path.mkdir(parents=True, exist_ok=True)
    
    def validate_file_upload(self, file: UploadFile) -> InputValidationResult:
        """
        Validates uploaded files (ZIP, dependency manifests).
        
        Args:
            file: The uploaded file to validate
            
        Returns:
            InputValidationResult with type and validation status
        """
        try:
            # Check file size
            file.file.seek(0, 2)  # Seek to end
            file_size = file.file.tell()
            file.file.seek(0)  # Reset to beginning
            
            file_size_mb = file_size / (1024 * 1024)
            
            if file_size > self.max_size_bytes:
                return InputValidationResult(
                    valid=False,
                    error_message=f"FILE_TOO_LARGE: File size {file_size_mb:.2f}MB exceeds maximum {settings.MAX_UPLOAD_SIZE_MB}MB",
                    file_size_mb=file_size_mb
                )
            
            # Detect input type from filename and extension
            filename = file.filename.lower() if file.filename else ""
            file_ext = Path(filename).suffix
            
            input_type = self._detect_input_type(filename, file_ext)
            
            if input_type is None:
                return InputValidationResult(
                    valid=False,
                    error_message=f"INVALID_FILE_FORMAT: Unsupported file type. Expected ZIP or dependency manifest file.",
                    file_size_mb=file_size_mb
                )
            
            return InputValidationResult(
                valid=True,
                input_type=input_type,
                file_size_mb=file_size_mb
            )
            
        except Exception as e:
            return InputValidationResult(
                valid=False,
                error_message=f"Validation error: {str(e)}"
            )
    
    def _detect_input_type(self, filename: str, file_ext: str) -> Optional[InputType]:
        """
        Detects input type from filename and extension.
        
        Args:
            filename: The filename (lowercase)
            file_ext: The file extension (lowercase)
            
        Returns:
            InputType or None if not recognized
        """
        # Check for codebase (ZIP files)
        if file_ext in self.CODEBASE_EXTENSIONS:
            return InputType.CODEBASE
        
        # Check for dependency files by name or extension
        if filename in self.DEPENDENCY_FILENAMES or file_ext in self.DEPENDENCY_EXTENSIONS:
            return InputType.DEPENDENCY
        
        return None
    
    def validate_url(self, url: str) -> URLValidationResult:
        """
        Validates URL format and accessibility.
        
        Args:
            url: The URL to validate
            
        Returns:
            URLValidationResult with accessibility status
        """
        try:
            # Basic format validation
            if not url or not url.strip():
                return URLValidationResult(
                    valid=False,
                    accessible=False,
                    error_message="URL_INVALID: Empty URL provided"
                )
            
            # Check URL scheme
            if not url.startswith(('http://', 'https://')):
                return URLValidationResult(
                    valid=False,
                    accessible=False,
                    error_message="URL_INVALID: URL must start with http:// or https://"
                )
            
            # Test connectivity with 10 second timeout
            import time
            start_time = time.time()
            
            try:
                response = requests.head(url, timeout=10, allow_redirects=True)
                response_time_ms = int((time.time() - start_time) * 1000)
                
                # Consider 2xx, 3xx, and some 4xx as accessible
                # (4xx means the server is reachable, just the resource might not exist)
                accessible = response.status_code < 500
                
                if not accessible:
                    return URLValidationResult(
                        valid=True,
                        url=url,
                        accessible=False,
                        error_message=f"URL_UNREACHABLE: Server returned status {response.status_code}",
                        response_time_ms=response_time_ms
                    )
                
                return URLValidationResult(
                    valid=True,
                    url=url,
                    accessible=True,
                    response_time_ms=response_time_ms
                )
                
            except requests.Timeout:
                return URLValidationResult(
                    valid=True,
                    url=url,
                    accessible=False,
                    error_message="URL_UNREACHABLE: Connection timeout after 10 seconds"
                )
            except requests.RequestException as e:
                return URLValidationResult(
                    valid=True,
                    url=url,
                    accessible=False,
                    error_message=f"URL_UNREACHABLE: {str(e)}"
                )
                
        except Exception as e:
            return URLValidationResult(
                valid=False,
                accessible=False,
                error_message=f"Validation error: {str(e)}"
            )
    
    def store_temporary_file(self, file: UploadFile, scan_id: str) -> str:
        """
        Stores uploaded file in encrypted temporary storage.
        
        Args:
            file: The uploaded file to store
            scan_id: Unique scan identifier for organizing storage
            
        Returns:
            Temporary file path
        """
        # Create scan-specific directory
        scan_dir = self.temp_storage_path / scan_id
        scan_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate encryption key from scan_id (deterministic for this scan)
        key = hashlib.sha256(scan_id.encode()).digest()
        
        # Generate IV (initialization vector)
        iv = os.urandom(16)
        
        # Create cipher
        cipher = Cipher(
            algorithms.AES(key),
            modes.CBC(iv),
            backend=default_backend()
        )
        encryptor = cipher.encryptor()
        
        # Determine output filename
        original_filename = file.filename if file.filename else "upload"
        encrypted_filename = f"{original_filename}.enc"
        encrypted_path = scan_dir / encrypted_filename
        
        # Store IV in a separate file
        iv_path = scan_dir / f"{encrypted_filename}.iv"
        with open(iv_path, 'wb') as iv_file:
            iv_file.write(iv)
        
        # Read, pad, encrypt, and write file
        file.file.seek(0)
        file_content = file.file.read()
        
        # Apply PKCS7 padding
        padder = padding.PKCS7(128).padder()
        padded_data = padder.update(file_content) + padder.finalize()
        
        # Encrypt
        encrypted_data = encryptor.update(padded_data) + encryptor.finalize()
        
        # Write encrypted file
        with open(encrypted_path, 'wb') as encrypted_file:
            encrypted_file.write(encrypted_data)
        
        return str(encrypted_path)
    
    def retrieve_temporary_file(self, encrypted_path: str, scan_id: str, output_path: str) -> str:
        """
        Retrieves and decrypts a temporary file.
        
        Args:
            encrypted_path: Path to the encrypted file
            scan_id: Unique scan identifier for decryption key
            output_path: Path where decrypted file should be written
            
        Returns:
            Path to decrypted file
        """
        # Generate decryption key from scan_id
        key = hashlib.sha256(scan_id.encode()).digest()
        
        # Read IV
        iv_path = f"{encrypted_path}.iv"
        with open(iv_path, 'rb') as iv_file:
            iv = iv_file.read()
        
        # Create cipher
        cipher = Cipher(
            algorithms.AES(key),
            modes.CBC(iv),
            backend=default_backend()
        )
        decryptor = cipher.decryptor()
        
        # Read encrypted file
        with open(encrypted_path, 'rb') as encrypted_file:
            encrypted_data = encrypted_file.read()
        
        # Decrypt
        padded_data = decryptor.update(encrypted_data) + decryptor.finalize()
        
        # Remove padding
        unpadder = padding.PKCS7(128).unpadder()
        file_content = unpadder.update(padded_data) + unpadder.finalize()
        
        # Write decrypted file
        output_path_obj = Path(output_path)
        output_path_obj.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'wb') as output_file:
            output_file.write(file_content)
        
        return output_path
    
    def cleanup_scan_artifacts(self, scan_id: str) -> None:
        """
        Deletes all files and directories for a scan.
        
        Args:
            scan_id: Unique scan identifier
        """
        scan_dir = self.temp_storage_path / scan_id
        
        if scan_dir.exists():
            shutil.rmtree(scan_dir)

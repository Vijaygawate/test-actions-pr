"""FastAPI application entry point."""

import logging
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import routes
from app.api import auth_routes
from app.config import settings
from app.models.error_models import ErrorResponse

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/tmp/securescan/app.log'),
    ]
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="SecureScan Platform",
    description="DevSecOps Scan-as-a-Service platform for comprehensive security analysis",
    version="1.0.0",
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Handle uncaught exceptions."""
    logger.exception(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            error_code="INTERNAL_ERROR",
            error_message="An unexpected error occurred",
            timestamp=datetime.utcnow(),
        ).model_dump(mode='json'),
    )


# Include API routes
app.include_router(auth_routes.router, prefix="/api/v1")
app.include_router(routes.router, prefix="/api/v1")


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "message": "SecureScan Platform API",
        "version": "1.0.0",
        "docs": "/docs",
    }


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.on_event("startup")
async def startup_event():
    """Initialize application on startup."""
    import os
    
    # Create necessary directories
    os.makedirs(settings.TEMP_STORAGE_PATH, exist_ok=True)
    os.makedirs(os.path.join(settings.TEMP_STORAGE_PATH, "reports"), exist_ok=True)
    os.makedirs(os.path.join(settings.TEMP_STORAGE_PATH, "uploads"), exist_ok=True)
    
    logger.info("SecureScan Platform started successfully")

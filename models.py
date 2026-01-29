"""
SQLAlchemy models for CFDI Verifier.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, JSON, Enum as SQLEnum
from sqlalchemy.orm import relationship
import enum

from database import Base


class VerificationStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class VerificationMethod(str, enum.Enum):
    FOLIO = "folio"
    XML = "xml"


class Verification(Base):
    """Individual CFDI verification record."""
    __tablename__ = "verifications"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(String(36), unique=True, index=True, nullable=False)

    # Request data
    method = Column(SQLEnum(VerificationMethod), nullable=False)
    folio_fiscal = Column(String(36), index=True, nullable=True)
    rfc_emisor = Column(String(13), index=True, nullable=True)
    rfc_receptor = Column(String(13), index=True, nullable=True)
    xml_hash = Column(String(64), nullable=True)  # SHA256 of XML content

    # Status
    status = Column(SQLEnum(VerificationStatus), default=VerificationStatus.PENDING, nullable=False)

    # Results
    valid = Column(Boolean, nullable=True)
    sat_response = Column(JSON, nullable=True)  # Full SAT response
    error_message = Column(Text, nullable=True)

    # Webhook
    webhook_url = Column(String(500), nullable=True)
    webhook_sent = Column(Boolean, default=False)

    # Batch reference
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=True)
    batch_index = Column(Integer, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Relationship
    batch = relationship("Batch", back_populates="verifications")

    def __repr__(self):
        return f"<Verification {self.job_id} - {self.status}>"


class Batch(Base):
    """Batch of verifications."""
    __tablename__ = "batches"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(String(36), unique=True, index=True, nullable=False)

    # Stats
    total_items = Column(Integer, nullable=False)
    completed_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)

    # Status
    status = Column(SQLEnum(VerificationStatus), default=VerificationStatus.PENDING, nullable=False)

    # Webhook
    webhook_url = Column(String(500), nullable=True)
    webhook_sent = Column(Boolean, default=False)

    # Celery task ID
    celery_group_id = Column(String(36), nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    # Relationship
    verifications = relationship("Verification", back_populates="batch", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Batch {self.batch_id} - {self.completed_count}/{self.total_items}>"


class APILog(Base):
    """API request/response logging."""
    __tablename__ = "api_logs"

    id = Column(Integer, primary_key=True, index=True)

    # Request info
    endpoint = Column(String(100), nullable=False)
    method = Column(String(10), nullable=False)
    request_body = Column(JSON, nullable=True)

    # Response info
    response_status = Column(Integer, nullable=True)
    response_body = Column(JSON, nullable=True)

    # Metadata
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)

    # Timing
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    duration_ms = Column(Integer, nullable=True)

    def __repr__(self):
        return f"<APILog {self.method} {self.endpoint} - {self.response_status}>"

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import declarative_base, relationship


Base = declarative_base()


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=True)
    handle = Column(String, nullable=True)
    platform = Column(String, nullable=False)
    url = Column(Text, nullable=False)
    message = Column(Text, nullable=True)
    location = Column(String, nullable=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    keywords = Column(JSON, default=list)
    score = Column(Float, default=0.0)
    created_at = Column(DateTime, server_default=func.now())
    seen_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    converted = Column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("platform", "url", name="u_platform_url"),
        Index("ix_score_created", "score", "created_at"),
    )


class Qualification(Base):
    __tablename__ = "qualifications"

    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False, unique=True)
    name = Column(String, nullable=True)
    zip = Column(String, nullable=True)
    primary = Column(JSON, default=dict)
    spouse = Column(JSON, default=dict)
    child1 = Column(JSON, default=dict)
    child2 = Column(JSON, default=dict)
    child3 = Column(JSON, default=dict)
    income = Column(String, nullable=True)
    start_date = Column(String, nullable=True)
    rate = Column(String, nullable=True)
    carrier = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    lead = relationship("Lead", lazy="joined")

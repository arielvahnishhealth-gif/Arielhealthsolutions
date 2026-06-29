from typing import Optional

from pydantic import BaseModel, Field


class Person(BaseModel):
    dob: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    height_in: Optional[int] = None
    weight_lb: Optional[int] = None
    smoker: Optional[bool] = None
    hbp_hc: Optional[bool] = None
    meds: Optional[bool] = None
    surgeries: Optional[bool] = None


class QualifyIn(BaseModel):
    name: Optional[str] = None
    zip: Optional[str] = None
    primary: Optional[Person] = None
    spouse: Optional[Person] = None
    child1: Optional[Person] = None
    child2: Optional[Person] = None
    child3: Optional[Person] = None
    income: Optional[str] = None
    start_date: Optional[str] = None
    rate: Optional[str] = None
    carrier: Optional[str] = None
    notes: Optional[str] = None

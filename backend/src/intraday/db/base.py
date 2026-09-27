"""Declarative base, naming conventions, and column helpers."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from sqlalchemy import CheckConstraint, DateTime, MetaData, Numeric, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, mapped_column

from intraday.core.ids import uuid7

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


UUIDPK = Annotated[
    uuid.UUID, mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
]
TS = Annotated[datetime, mapped_column(DateTime(timezone=True))]
TSNow = Annotated[
    datetime, mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
]
Price = Annotated[Decimal, mapped_column(Numeric(14, 4))]
MoneyCol = Annotated[Decimal, mapped_column(Numeric(14, 2))]


def check_in(column: str, values: tuple[str, ...] | list[str], name: str | None = None
             ) -> CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name or f"{column}_valid")

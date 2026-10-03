#!/usr/bin/python
# -*- coding: utf-8 -*-
from typing import ClassVar, Self

from pydantic import BaseModel, model_validator


class PatchModel(BaseModel):
    """Схема частичного обновления (PATCH): поле можно не передавать, но явный null для поля, которое в БД
    NOT NULL, — 422. Без этого null доходил до commit и давал 500 (IntegrityError).

    NULLABLE — поля, которые действительно можно стереть (например, description).
    """

    NULLABLE: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def _reject_nulls(self) -> Self:
        nulls = sorted(f for f in self.model_fields_set if getattr(self, f) is None and f not in self.NULLABLE)
        if nulls:
            raise ValueError(f"Fields cannot be null: {', '.join(nulls)}")
        return self

"""Routes versionnées de l'API."""

from app.api.v1.accounts import router as accounts_router
from app.api.v1.categories import router as categories_router
from app.api.v1.imports import router as imports_router
from app.api.v1.transactions import router as transactions_router

__all__ = ["accounts_router", "categories_router", "imports_router", "transactions_router"]

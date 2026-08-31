"""Product catalogue routes (versioned products + effective-dated rate tables)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from insurance.api.deps import IdentityDep, JsonDict, SessionDep, require_policy
from insurance.api.schemas import ProductCreate, RateTableCreate
from insurance.models import Product, RateTable
from insurance.services import lifecycle
from insurance.services.lifecycle import LifecycleError

router = APIRouter(prefix="/v1/products", tags=["products"])


def _err(exc: LifecycleError) -> HTTPException:
    status = 409 if exc.reason in ("bad-state", "rate-window-overlap", "no-rate-table") else 400
    return HTTPException(status_code=status, detail={"reason": exc.reason, "detail": str(exc)})


def _product_view(p: Product) -> dict[str, Any]:
    return {
        "code": p.code, "version": p.version, "kind": p.kind, "name": p.name,
        "status": p.status, "definition": p.definition, "createdBy": p.created_by,
        "createdAt": p.created_at.isoformat(),
    }


@router.post("", status_code=201)
async def create_product(request: Request, body: ProductCreate, identity: IdentityDep, session: SessionDep) -> JsonDict:
    require_policy(request, identity, "product", "create", "CONFIDENTIAL")
    try:
        product = await lifecycle.create_product(
            session, code=body.code, kind=body.kind, name=body.name,
            definition=body.definition, principal=identity.subject,
        )
        await session.commit()
    except LifecycleError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return _product_view(product)


@router.post("/{code}/versions/{version}/activate")
async def activate_product(
    code: str,
    version: int,
    request: Request,
    identity: IdentityDep,
    session: SessionDep
) -> JsonDict:
    require_policy(request, identity, "product", "activate", "CONFIDENTIAL")
    product = (
        await session.execute(select(Product).where(Product.code == code, Product.version == version))
    ).scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=404, detail={"reason": "unknown-product"})
    try:
        await lifecycle.activate_product(session, product, identity.subject)
        await session.commit()
    except LifecycleError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return _product_view(product)


@router.post("/{code}/versions/{version}/rate-tables", status_code=201)
async def add_rate_table(
    code: str, version: int, request: Request, body: RateTableCreate,
    identity: IdentityDep, session: SessionDep,
) -> dict[str, Any]:
    require_policy(request, identity, "product", "rate", "CONFIDENTIAL")
    product = (
        await session.execute(select(Product).where(Product.code == code, Product.version == version))
    ).scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=404, detail={"reason": "unknown-product"})
    try:
        table = await lifecycle.add_rate_table(
            session, product=product, effective_from=body.effective_from,
            effective_to=body.effective_to, rates=body.rates, principal=identity.subject,
        )
        await session.commit()
    except LifecycleError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return {
        "id": str(table.id), "productCode": code, "productVersion": version,
        "effectiveFrom": table.effective_from.isoformat(),
        "effectiveTo": table.effective_to.isoformat() if table.effective_to else None,
    }


@router.get("")
async def list_products(
    request: Request, identity: IdentityDep, session: SessionDep, limit: int = 500
) -> dict[str, Any]:
    require_policy(request, identity, "product", "read", "INTERNAL")
    # Bounded catalog read: deterministic (code, version) ordering makes the
    # cap stable; limit is clamped to a hard ceiling.
    limit = max(1, min(limit, 5000))
    rows = (
        await session.execute(select(Product).order_by(Product.code, Product.version).limit(limit))
    ).scalars().all()
    return {"products": [_product_view(p) for p in rows]}


@router.get("/{code}/versions/{version}/rate-tables")
async def list_rate_tables(
    code: str,
    version: int,
    request: Request,
    identity: IdentityDep,
    session: SessionDep
) -> JsonDict:
    require_policy(request, identity, "product", "read", "INTERNAL")
    product = (
        await session.execute(select(Product).where(Product.code == code, Product.version == version))
    ).scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=404, detail={"reason": "unknown-product"})
    rows = (
        await session.execute(
            select(RateTable).where(RateTable.product_id == product.id).order_by(RateTable.effective_from)
        )
    ).scalars().all()
    return {
        "rateTables": [
            {
                "id": str(t.id), "effectiveFrom": t.effective_from.isoformat(),
                "effectiveTo": t.effective_to.isoformat() if t.effective_to else None,
                "rates": t.rates,
            }
            for t in rows
        ]
    }

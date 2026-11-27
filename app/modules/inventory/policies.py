"""Inventory permissions (ADR 0028)."""

from app.authz.permissions import ORG_ONLY, Permission
from app.authz.sod import SodRule

ITEM_READ = Permission(
    "inventory.item.read",
    "View inventory",
    "See items, stock on hand and its value, warehouses and the stock ledger.",
    scopes=ORG_ONLY,
)
ITEM_MANAGE = Permission(
    "inventory.item.manage",
    "Manage items and warehouses",
    "Add and change items, categories, units of measure, warehouses and bins. Receives "
    "the daily low-stock digest.",
    scopes=ORG_ONLY,
)
STOCK_MOVE = Permission(
    "inventory.stock.move",
    "Move stock",
    "Issue and transfer stock, record adjustments and counts, and issue approved material "
    "requests.",
    scopes=ORG_ONLY,
)
STOCK_APPROVE = Permission(
    "inventory.stock.approve",
    "Approve stock adjustments",
    "Post adjustments and count variances someone else recorded.",
    scopes=ORG_ONLY,
)
REQUEST_CREATE = Permission(
    "inventory.request.create",
    "Request materials",
    "Raise material requests for your own work.",
)
REQUEST_READ = Permission(
    "inventory.request.read",
    "View material requests",
    "See material requests within scope.",
)
REQUEST_APPROVE = Permission(
    "inventory.request.approve",
    "Approve material requests",
    "Decide the material requests you are named on as line manager, or by delegation.",
)
REQUEST_APPROVE_ANY = Permission(
    "inventory.request.approve_any",
    "Step in on material requests",
    "Decide any material request, e.g. when the requester has no line manager.",
    scopes=ORG_ONLY,
)

PERMISSIONS = (
    ITEM_READ,
    ITEM_MANAGE,
    STOCK_MOVE,
    STOCK_APPROVE,
    REQUEST_CREATE,
    REQUEST_READ,
    REQUEST_APPROVE,
    REQUEST_APPROVE_ANY,
)

SOD_RULES = (
    SodRule(
        key="inventory.adjust_four_eyes",
        permission=STOCK_APPROVE.key,
        conflict="creator_is_actor",
        message="Someone else posts a stock adjustment or count you recorded.",
    ),
    *(
        SodRule(
            key=f"inventory.no_self_{p.key.rsplit('.', 1)[1]}",
            permission=p.key,
            conflict="owner_is_actor",
            message="You can't approve your own material request.",
        )
        for p in (REQUEST_APPROVE, REQUEST_APPROVE_ANY)
    ),
)

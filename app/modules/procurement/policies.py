"""Procurement permissions (ADR 0030)."""

from app.authz.permissions import ORG_ONLY, Permission
from app.authz.sod import SodRule

REQUISITION_CREATE = Permission(
    "procurement.requisition.create",
    "Raise purchase requisitions",
    "Ask for goods or services to be bought for your department.",
)
REQUISITION_READ = Permission(
    "procurement.requisition.read",
    "View purchase requisitions",
    "See purchase requisitions within scope, with their approvals.",
)
REQUISITION_APPROVE = Permission(
    "procurement.requisition.approve",
    "Approve requisitions as line manager",
    "Decide the line-manager step of requisitions you are named on, or by delegation.",
)
REQUISITION_APPROVE_ANY = Permission(
    "procurement.requisition.approve_any",
    "Step in on requisitions",
    "Decide the line-manager step when the requester has no line manager.",
    scopes=ORG_ONLY,
)
REQUISITION_APPROVE_FINANCE = Permission(
    "procurement.requisition.approve_finance",
    "Approve requisitions as Finance",
    "Decide the finance step of requisitions over the finance tier's amount.",
    scopes=ORG_ONLY,
)
REQUISITION_APPROVE_DIRECTOR = Permission(
    "procurement.requisition.approve_director",
    "Approve requisitions as Director",
    "Decide the director step of requisitions over the director tier's amount.",
    scopes=ORG_ONLY,
)
RFQ_READ = Permission(
    "procurement.rfq.read",
    "View RFQs and supplier performance",
    "See requests for quotation, the quotes received, awards and supplier performance.",
    scopes=ORG_ONLY,
)
RFQ_MANAGE = Permission(
    "procurement.rfq.manage",
    "Run RFQs",
    "Invite suppliers to quote on approved requisitions, record their quotes, award one and "
    "draft the purchase order.",
    scopes=ORG_ONLY,
)
SUPPLIER_RATE = Permission(
    "procurement.supplier.rate",
    "Rate suppliers",
    "Rate a supplier's purchase order once it has been received.",
    scopes=ORG_ONLY,
)
SETTINGS_MANAGE = Permission(
    "procurement.settings.manage",
    "Manage procurement settings",
    "Change the approval tiers and the amount over which three quotes are needed.",
    scopes=ORG_ONLY,
)

PERMISSIONS = (
    REQUISITION_CREATE,
    REQUISITION_READ,
    REQUISITION_APPROVE,
    REQUISITION_APPROVE_ANY,
    REQUISITION_APPROVE_FINANCE,
    REQUISITION_APPROVE_DIRECTOR,
    RFQ_READ,
    RFQ_MANAGE,
    SUPPLIER_RATE,
    SETTINGS_MANAGE,
)

SOD_RULES = tuple(
    SodRule(
        key=f"procurement.no_self_{p.key.rsplit('.', 1)[1]}",
        permission=p.key,
        conflict="owner_is_actor",
        message="You can't approve your own requisition.",
    )
    for p in (
        REQUISITION_APPROVE,
        REQUISITION_APPROVE_ANY,
        REQUISITION_APPROVE_FINANCE,
        REQUISITION_APPROVE_DIRECTOR,
    )
)

"""What other modules may use from finance (ADR 0023): hand over a balanced journal in the
same transaction as the document behind it, see how those journals stand, and pick
accounts. Inventory posts its own documents' journals through `post_document_journal`
(source `stock`, ADR 0028). Procurement drafts purchase orders and reads suppliers' order
history through `procurement_link` (ADR 0030)."""

from app.modules.finance.accounts import all_accounts, postable
from app.modules.finance.customers import choices as customer_choices
from app.modules.finance.journals import (
    SystemLine,
    post_document_journal,
    post_system_journal,
    statuses,
)
from app.modules.finance.models import VAT_CODES
from app.modules.finance.procurement_link import (
    OrderFacts,
    OrderLineDraft,
    SupplierInfo,
    all_suppliers,
    draft_order,
    order_status,
    orders_by_supplier,
    purchase_accounts,
    supplier_choices,
    supplier_history,
    suppliers_by_id,
)
from app.modules.finance.project_link import (
    ProjectLink,
    ProjectPosting,
    ProjectTotal,
    customer_email,
    customer_names,
    project_postings,
    project_totals,
)
from app.modules.finance.project_link import check as check_project
from app.modules.finance.project_link import link as project_link
from app.modules.finance.project_link import set_link as set_project_link
from app.modules.finance.sales import VAT_LABELS, organisation_name
from app.modules.finance.stock_link import Source as StockSource
from app.modules.finance.stock_link import StockItem, StockLine, link, set_link
from app.modules.finance.tax import vat_amount, vat_rate_on
from app.modules.finance.vehicle_link import VehicleCost, VehicleLink, vehicle_costs
from app.modules.finance.vehicle_link import check as check_vehicle
from app.modules.finance.vehicle_link import link as vehicle_link
from app.modules.finance.vehicle_link import set_link as set_vehicle_link

__all__ = [
    "VAT_CODES",
    "VAT_LABELS",
    "OrderFacts",
    "OrderLineDraft",
    "ProjectLink",
    "ProjectPosting",
    "ProjectTotal",
    "StockItem",
    "StockLine",
    "StockSource",
    "SupplierInfo",
    "SystemLine",
    "VehicleCost",
    "VehicleLink",
    "all_accounts",
    "all_suppliers",
    "check_project",
    "check_vehicle",
    "customer_choices",
    "customer_email",
    "customer_names",
    "draft_order",
    "link",
    "order_status",
    "orders_by_supplier",
    "organisation_name",
    "post_document_journal",
    "post_system_journal",
    "postable",
    "project_link",
    "project_postings",
    "project_totals",
    "purchase_accounts",
    "set_link",
    "set_project_link",
    "set_vehicle_link",
    "statuses",
    "supplier_choices",
    "supplier_history",
    "suppliers_by_id",
    "vat_amount",
    "vat_rate_on",
    "vehicle_costs",
    "vehicle_link",
]

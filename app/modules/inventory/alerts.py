"""The daily low-stock digest (ADR 0028): one email to everyone allowed to manage items,
listing items at or below their reorder level in each warehouse. An item and warehouse
is listed at most once a day."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import engine
from app.identity.models import User
from app.mail import service as mail
from app.modules.inventory import stock
from app.modules.inventory.items import MANAGE, low_places
from app.modules.inventory.models import Item, ItemValue, LowStockAlert, Unit, Warehouse


def send_digest(db: Session, today: date, organisation: str) -> int:
    """Queue today's digest. Returns how many people it went to."""
    sent_today = {
        (a.item_id, a.warehouse_id)
        for a in db.scalars(select(LowStockAlert).where(LowStockAlert.alert_date == today))
    }
    places = [p for p in low_places(db) if p not in sent_today]
    if not places:
        return 0
    recipients = [
        u
        for u in db.scalars(select(User).where(User.status == "active").order_by(User.email))
        if engine.check(db, u.id, MANAGE)
    ]
    if not recipients:
        return 0
    rows = []
    for item_id, warehouse_id in places:
        item = db.get(Item, item_id)
        warehouse = db.get(Warehouse, warehouse_id)
        unit = db.get(Unit, item.unit_id) if item else None
        worth = db.scalars(
            select(ItemValue).where(
                ItemValue.item_id == item_id, ItemValue.warehouse_id == warehouse_id
            )
        ).first()
        if item is None or warehouse is None or worth is None:
            continue
        rows.append(
            {
                "code": item.code,
                "name": item.name,
                "warehouse": warehouse.name,
                "on_hand": stock.show(worth.quantity),
                "level": stock.show(item.reorder_level),
                "reorder": stock.show(item.reorder_quantity),
                "unit": unit.code if unit else "",
            }
        )
        db.add(LowStockAlert(alert_date=today, item_id=item_id, warehouse_id=warehouse_id))
    rows.sort(key=lambda r: (r["warehouse"], r["code"]))
    for user in recipients:
        mail.queue(
            db,
            to=user.email,
            template="inventory_low_stock",
            context={
                "name": user.full_name,
                "organisation": organisation,
                "today": f"{today:%d %b %Y}",
                "rows": rows,
                "count": len(rows),
            },
        )
    db.flush()
    return len(recipients)

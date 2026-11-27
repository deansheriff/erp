"""Fleet permissions (ADR 0033). Fleet Managers run the fleet; employees log fuel on the
vehicles they drive, report incidents and request bookings; cost per vehicle is for
Fleet Managers, Finance and Directors."""

from app.authz.permissions import ORG_ONLY, Permission

VEHICLE_READ = Permission(
    "fleet.vehicle.read",
    "View the fleet",
    "See every vehicle with its documents, services, fuel, incidents and bookings.",
    scopes=ORG_ONLY,
)
VEHICLE_MANAGE = Permission(
    "fleet.vehicle.manage",
    "Manage the fleet",
    "Add and change vehicles, documents, drivers and service plans; record services; "
    "approve bookings; review and close incidents; change the fleet settings.",
    scopes=ORG_ONLY,
)
COST_READ = Permission(
    "fleet.cost.read",
    "View cost per vehicle",
    "See each vehicle's costs from the ledger, distance, fuel and cost per kilometre.",
    scopes=ORG_ONLY,
)
USE = Permission(
    "fleet.vehicle.use",
    "Use fleet vehicles",
    "Log fuel on the vehicles you drive, report incidents and request pool vehicles.",
)

PERMISSIONS = (VEHICLE_READ, VEHICLE_MANAGE, COST_READ, USE)
SOD_RULES = ()

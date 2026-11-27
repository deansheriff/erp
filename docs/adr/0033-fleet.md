# 0033: Fleet

Status: proposed (Phase 11f), decisions confirmed 2026-11-27

## Context

The brief (9.13) asks for:

- vehicles with documents (insurance, registration, roadworthiness) and expiry alerts;
- assigned drivers, fuel logs, and scheduled and ad-hoc maintenance;
- incidents and vehicle reservations;
- cost per vehicle.

## Decisions

**Module.** The work goes in `app/modules/fleet`.

**Cost on the ledger.** A vehicle is an optional dimension on journal lines, like a
project (ADR 0031). Direct bill lines, expense claim lines and manual journal lines can
name an active vehicle, and the vehicle travels into their journals. Finance reaches
vehicles through `finance/vehicle_link.py`, which fleet implements; `app/wiring.py`
connects them.

Cost per vehicle is read from the ledger: posted expense lines naming the vehicle, by
account. Fleet's fuel logs and maintenance records may quote the bill or claim that paid
for them, but their amounts aren't added again. Distance and litres come from fuel logs,
which gives cost per kilometre and kilometres per litre.

**Vehicles (`VEH-`)** have:

- registration number, make, model, year, type, fuel and identifiers;
- purchase date and starting odometer;
- whether they're a pool vehicle (bookable);
- an assigned driver;
- a status: active, in the workshop, or retired.

**Documents.** Each vehicle has insurance, a vehicle licence (registration),
roadworthiness and other documents. Each has a number, an issuer, dates, and a scan
(file purpose `fleet_document`).

**Drivers.** Drivers are employees with a licence number, class and expiry. An expired
licence blocks assigning a vehicle or a booking to them.

**Alerts.** A daily task emails the Fleet Managers 30 and 7 days before a document or
licence expires, and on the day; licence alerts also go to the driver. Each alert goes
once per level. A vehicle with an expired document can't be booked. Both levels are
editable, to confirm.

**Service plans** repeat by distance, by time, or both, whichever comes first, for example
oil every 5,000 km or 3 months.

- **Due:** a plan is due from the odometer (the latest fuel log or service) and the last
  service. The same daily task emails the Fleet Managers when a plan falls due.
- **Services:** they are recorded as scheduled (against a plan, resetting it) or ad hoc.
  Each has a date, odometer, vendor, description, cost and payment reference.

**Fuel.**

- **Who:** the assigned driver, a driver on an approved booking that day, or a Fleet
  Manager logs fuel. A log has date, litres, amount, odometer, station, receipt and
  payment reference.
- **Odometer:** it can't go backwards.
- **Low efficiency:** a log is flagged when its kilometres per litre is below 70% of the
  vehicle's average (to confirm).

**Incidents (`INC-`).**

1. A driver reports one: when, where, what happened, injuries, police report and photos.
2. A Fleet Manager reviews it: repair cost, insurance claim number, claim status (not
   claimed, filed, approved, rejected, paid) and amount recovered.
3. The Fleet Manager closes it.

**Bookings.**

- Any employee requests a pool vehicle for a time slot, with purpose and destination.
- A Fleet Manager approves it (optionally naming a licensed driver) or rejects it with a
  reason.
- Overlapping approved bookings and vehicles with expired documents are refused.

**Permissions.**

| Permission | Who has it |
|---|---|
| `fleet.vehicle.manage` | Fleet Managers |
| `fleet.vehicle.read` | Fleet Managers, Auditors |
| `fleet.cost.read` | Fleet Managers, Finance Managers, Directors, Auditors |
| `fleet.vehicle.use` | Employees, for themselves: fuel logs, incident reports and booking requests. Fleet Managers, organisation-wide |

**Files.** Document scans attach to the vehicle (Fleet Managers attach, anyone with
`fleet.vehicle.read` sees them). Fuel receipts attach to the fuel log and incident
photos to the incident, owned by the employee who logged or reported it, so a driver
can attach and open their own and Fleet Managers can open all of them.

## Consequences

- Journal lines, bill lines, expense lines and expense journal lines gain a `vehicle_id`.
  `SystemLine` gains an optional sixth element, and lines are merged by account, cost
  centre, project and vehicle.
- The screens were requested from Stitch and are marked `stitch_pending` until they
  arrive.

# Fleet

Phase 11f (ADR 0033). Permissions are in `docs/PERMISSIONS.md`.

## Tables

| Table | What it holds |
|---|---|
| `vehicles` | The register (`VEH-`): registration, make, model, year, type, fuel, colour, chassis number, purchase date, starting odometer, pool flag, assigned driver and status (active, in the workshop, retired). |
| `vehicle_documents` | Insurance, vehicle licence, roadworthiness and other papers, with number, issuer, dates and a scan (file purpose `fleet_document`). |
| `fleet_drivers` | Employees allowed to drive: licence number, class and expiry, and whether they're driving. |
| `service_plans`, `vehicle_maintenance` | Plans repeating by distance, time or both; services and repairs with date, odometer, workshop, cost and the bill reference. |
| `fuel_logs` | Fills: date, litres, amount, odometer, station, receipt (file purpose `fleet_receipt`), km/L and the low-efficiency flag. |
| `vehicle_incidents`, `vehicle_incident_photos` | Incidents (`INC-`) with the report, the review (repair cost, claim, recovered) and photos (file purpose `fleet_photo`). |
| `vehicle_bookings` | Pool vehicle requests with the slot, purpose, destination, decision, driver and reason. |
| `fleet_settings`, `fleet_expiry_alerts` | Alert lead times and the efficiency threshold; which alerts have gone out. |

## Who does what

| Who | What |
|---|---|
| Fleet Manager (`fleet_manager`) | Vehicles, papers, drivers, service plans and services; approving bookings; reviewing and closing incidents; settings; cost per vehicle. Can log fuel on any vehicle. |
| Every employee | Logs fuel on a vehicle they drive today (assigned to them, or on an approved booking), books pool vehicles and reports incidents. Sees their own bookings, fuel logs and incidents. |
| Finance Managers, Directors, Auditors | Cost per vehicle. Auditors can also view the fleet. |

## Screens

- **My fleet** (`/fleet`): the vehicles you drive today with their odometer, logging fuel
  with a receipt photo, booking a pool vehicle, reporting an incident, your bookings
  (cancel while requested or approved) and your recent fuel logs.
- **Vehicles** (`/fleet/vehicles`): the register with driver, odometer, papers and the
  next service, filtered by status and pool, and papers coming up for renewal.
- **A vehicle** (`/fleet/vehicles/{id}`): papers with days left, service plans with their
  next due point, recording a service, the service history, fuel with km/L, incidents
  and this year's costs from the ledger.
- **Bookings** (`/fleet/bookings`): requests waiting for approval, and what's booked over
  the next two weeks.
- **Incidents** (`/fleet/incidents` and each incident): the report, photos, and the review
  with the insurance claim.
- **Drivers** (`/fleet/drivers`), **Cost per vehicle** (`/fleet/costs`) and
  **Settings** (`/fleet/settings`).

## Rules

- **Drivers.** Only someone with an active driver record and a licence in date can be
  assigned a vehicle or named on a booking.
- **Odometer.** The current reading is the highest of the starting odometer, fuel logs and
  services. A fuel log or service can't record a lower reading.
- **Efficiency.** A fill's km/L is the distance since the previous fill over its litres.
  It's flagged when it falls below 70% of the vehicle's average.
- **Service plans.** A plan is due at the last service's reading plus its distance, or
  the last service's date plus its months, whichever comes first. Before the first
  service it counts from the starting odometer and the purchase date. Recording a service
  against a plan moves the plan on.
- **Bookings.** A booking can't overlap an approved one for the same vehicle, and a
  vehicle with expired papers can't be booked. Rejecting needs a reason.
- **Incidents.** Reported, then reviewed (repair cost, claim number and status, amount
  recovered), then closed. A claim marked filed or later needs its number. Photos can be
  added until it's closed.

## Alerts

`fleet.daily_alerts` runs at 06:30 UTC (07:30 Lagos).

- **Papers and licences.** Fleet Managers are emailed 30 days and 7 days before a vehicle
  paper or a driver's licence expires, and on the day. The driver also hears about their
  own licence.
- **Services.** Fleet Managers are emailed when a service plan falls due.

Each alert goes once.

## Cost per vehicle

Costs come from the ledger: posted expense lines naming the vehicle, by account. Bills,
expense claims and manual journals can name a vehicle on a line (see `docs/FINANCE.md`
and `docs/EXPENSES.md`).

The fuel and service amounts recorded in fleet are for reference and aren't added again.
Distance and litres come from fuel logs in the period, which gives cost per kilometre and
kilometres per litre.

## Defaults (confirm before go-live)

- Expiry alerts 30 and 7 days before, and on the day.
- Flag a fill below 70% of the vehicle's average km/L.
- Receipts, scans and photos kept six years.

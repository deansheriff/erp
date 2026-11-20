"""Attendance permissions (ADR 0019)."""

from app.authz.permissions import ORG_ONLY, Permission, Scope
from app.authz.sod import SodRule

CLOCK_USE = Permission(
    "attendance.clock.use",
    "Clock in and out",
    "Clock in and out for yourself.",
    scopes=(Scope.SELF,),
)
RECORD_READ = Permission(
    "attendance.record.read",
    "View attendance",
    "See timesheets, who is in today and the monthly summary within scope.",
)
CORRECTION_REQUEST = Permission(
    "attendance.correction.request",
    "Ask for attendance corrections",
    "Ask to fix a day's clock-in and clock-out times (for yourself, or others in scope).",
)
RECORD_APPROVE = Permission(
    "attendance.record.approve",
    "Approve attendance",
    "Decide correction requests and overtime within scope.",
)
# Not a ".read" permission, so the Auditor template doesn't pick it up: photos are seen
# only by HR and the person themself (confirmed 2026-11-19).
PHOTO_VIEW = Permission(
    "attendance.photo.view",
    "See clock-in photos",
    "Open the photos taken when people clock in.",
    sensitive=True,
)
SCHEDULE_MANAGE = Permission(
    "attendance.schedule.manage",
    "Manage shifts and rosters",
    "Give people their usual shift and change single days on the roster.",
)
SETTINGS_MANAGE = Permission(
    "attendance.settings.manage",
    "Manage attendance settings",
    "Shift patterns and branch locations (geofence and photo).",
    scopes=ORG_ONLY,
)

PERMISSIONS = (
    CLOCK_USE,
    RECORD_READ,
    CORRECTION_REQUEST,
    RECORD_APPROVE,
    PHOTO_VIEW,
    SCHEDULE_MANAGE,
    SETTINGS_MANAGE,
)

SOD_RULES = (
    SodRule(
        key="attendance.no_self_approval",
        permission=RECORD_APPROVE.key,
        conflict="owner_is_actor",
        message="You can't approve your own attendance.",
    ),
)

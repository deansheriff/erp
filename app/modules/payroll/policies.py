"""Payroll permissions (ADR 0021, 0022). Pay amounts are `compensation` class: every payroll
permission that shows them is sensitive."""

from app.authz.permissions import ORG_ONLY, Permission
from app.authz.sod import SodRule

PAY_READ = Permission(
    "payroll.pay.read",
    "View people's pay",
    "See pay records, components and amounts within scope.",
    sensitive=True,
)
PAY_MANAGE = Permission(
    "payroll.pay.manage",
    "Manage people's pay",
    "Set people's pay group, salary structure, amounts and recurring deductions.",
    sensitive=True,
    scopes=ORG_ONLY,
)
STRUCTURE_MANAGE = Permission(
    "payroll.structure.manage",
    "Manage salary structures",
    "Pay components, salary structures and pay groups.",
    scopes=ORG_ONLY,
)
SETTINGS_MANAGE = Permission(
    "payroll.settings.manage",
    "Manage statutory settings",
    "PAYE bands, reliefs, pension, NHF and other statutory rates.",
    scopes=ORG_ONLY,
)
RUN_READ = Permission(
    "payroll.run.read",
    "View payroll runs",
    "See runs and everyone's calculated pay in them.",
    sensitive=True,
    scopes=ORG_ONLY,
)
RUN_PREPARE = Permission(
    "payroll.run.prepare",
    "Prepare payroll runs",
    "Create runs and calculate them.",
    sensitive=True,
    scopes=ORG_ONLY,
)
RUN_REVIEW = Permission(
    "payroll.run.review",
    "Review payroll runs",
    "Mark a calculated run reviewed, or send it back.",
    sensitive=True,
    scopes=ORG_ONLY,
)
RUN_APPROVE = Permission(
    "payroll.run.approve",
    "Approve payroll runs",
    "Approve a reviewed run (payslips are generated), or send it back.",
    sensitive=True,
    scopes=ORG_ONLY,
)
RUN_POST = Permission(
    "payroll.run.post",
    "Post payroll runs",
    "Post an approved run.",
    sensitive=True,
    scopes=ORG_ONLY,
)
RUN_PAY = Permission(
    "payroll.run.pay",
    "Mark payroll runs paid",
    "Mark a posted run paid; employees then see their payslips.",
    sensitive=True,
    scopes=ORG_ONLY,
)
PAYSLIP_READ = Permission(
    "payroll.payslip.read",
    "View payslips",
    "See payslips within scope (your own once the run is paid).",
    sensitive=True,
)
RELIEF_DECLARE = Permission(
    "payroll.relief.declare",
    "Declare rent for tax relief",
    "Declare annual rent with proof, for PAYE rent relief.",
)
RELIEF_VERIFY = Permission(
    "payroll.relief.verify",
    "Verify rent relief",
    "Verify or reject rent declarations.",
    sensitive=True,
    scopes=ORG_ONLY,
)
REMITTANCE_EXPORT = Permission(
    "payroll.remittance.export",
    "Download remittance files",
    "The bank payment file and PAYE, pension and NHF schedules, with account numbers, "
    "TINs, RSA PINs and NHF numbers.",
    sensitive=True,
    scopes=ORG_ONLY,
)
LOAN_READ = Permission(
    "payroll.loan.read",
    "View staff loans",
    "See loans and salary advances within scope, with their repayments.",
    sensitive=True,
)
LOAN_MANAGE = Permission(
    "payroll.loan.manage",
    "Record staff loans",
    "Record loans and salary advances, and cancel ones not yet repaid.",
    sensitive=True,
    scopes=ORG_ONLY,
)
LOAN_APPROVE = Permission(
    "payroll.loan.approve",
    "Approve staff loans",
    "Approve or reject recorded loans and salary advances.",
    sensitive=True,
    scopes=ORG_ONLY,
)
CERTIFICATE_READ = Permission(
    "payroll.certificate.read",
    "Download tax certificates",
    "Year-end tax certificates within scope (your own once December is paid).",
    sensitive=True,
)

PERMISSIONS = (
    PAY_READ,
    PAY_MANAGE,
    STRUCTURE_MANAGE,
    SETTINGS_MANAGE,
    RUN_READ,
    RUN_PREPARE,
    RUN_REVIEW,
    RUN_APPROVE,
    RUN_POST,
    RUN_PAY,
    PAYSLIP_READ,
    RELIEF_DECLARE,
    RELIEF_VERIFY,
    REMITTANCE_EXPORT,
    LOAN_READ,
    LOAN_MANAGE,
    LOAN_APPROVE,
    CERTIFICATE_READ,
)

SOD_RULES = (
    SodRule(
        key="payroll.no_self_pay_change",
        permission=PAY_MANAGE.key,
        conflict="subject_is_actor",
        message="You can't change your own pay.",
    ),
    SodRule(
        key="payroll.no_self_relief",
        permission=RELIEF_VERIFY.key,
        conflict="owner_is_actor",
        message="You can't verify your own rent declaration.",
    ),
    SodRule(
        key="payroll.no_self_loan",
        permission=LOAN_APPROVE.key,
        conflict="owner_is_actor",
        message="You can't approve your own loan.",
    ),
    SodRule(
        key="payroll.loan_four_eyes",
        permission=LOAN_APPROVE.key,
        conflict="creator_is_actor",
        message="Someone else approves a loan you recorded.",
    ),
)

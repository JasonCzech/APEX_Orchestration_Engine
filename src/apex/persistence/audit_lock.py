"""Shared PostgreSQL advisory-lock identity for the audit hash chain."""

AUDIT_CHAIN_LOCK_KEY = 0x4150455841554449  # "APEXAUDI" as a signed int64.

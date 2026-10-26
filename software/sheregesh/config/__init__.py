# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Конфигурация: схема, атомарные ревизии, подтверждение, откат, резервные копии (ADR-0004)."""
from .defaults import console_defaults, remote_defaults
from .schema import Schema, ValidationIssue
from .store import (ApplyResult, BackupError, BackupInfo, ConfigError, ConfigStore, ConfirmationRequired, FsOps,
                    NoValidConfiguration, RecoveryReport, Revision)

__all__ = ["ApplyResult", "BackupError", "BackupInfo", "ConfigError", "ConfigStore", "ConfirmationRequired", "FsOps",
           "NoValidConfiguration", "RecoveryReport", "Revision", "Schema", "ValidationIssue", "console_defaults",
           "remote_defaults"]

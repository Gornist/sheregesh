# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""sheregesh-ota-client — клиент агента обновления A/B для пользователя sheregesh-ui (по SSH). См. ota.py."""
import sys

from .ota import client_main

if __name__ == "__main__":
    sys.exit(client_main())

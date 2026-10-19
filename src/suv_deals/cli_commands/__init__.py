"""Operator CLI command groups (``suv-deals ...``; spec section 27).

Each module registers one click command or group. Modules import only ``click`` and the standard
library at import time; every application, database and HTTP import happens inside the command
function, so ``suv-deals --help`` stays fast and works without a database or configuration.
"""

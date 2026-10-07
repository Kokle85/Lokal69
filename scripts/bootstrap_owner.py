#!/usr/bin/env python3
"""Link an EXISTING Supabase Auth user to a workspace as its owner (spec 12, 27; ADR 0001).

Thin wrapper around ``suv-deals bootstrap owner`` so the step can be run from a checkout::

    MAINTENANCE_DATABASE_URL=postgresql://... uv run python scripts/bootstrap_owner.py \\
        --email owner@example.com --workspace-name "Vasko deals" --yes

It never creates or invites Auth users and never takes a connection string as an argument:
the privileged maintenance URL comes from ``MAINTENANCE_DATABASE_URL`` (or ``--url-env NAME``).
The target is printed without the password and nothing is written without ``--yes``.
See ``suv-deals bootstrap owner --help``.
"""

from __future__ import annotations

import sys

from suv_deals.cli import main

if __name__ == "__main__":
    main(["bootstrap", "owner", *sys.argv[1:]])

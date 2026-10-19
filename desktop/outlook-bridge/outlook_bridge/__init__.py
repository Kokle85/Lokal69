"""Local classic-Outlook reply worker for the SUV deal-discovery system (spec v1.1 sections 37.6-37.8).

The worker runs as the signed-in interactive Windows user next to classic Outlook for Windows. It
matches seller replies to this system's inquiries *locally* and uploads only correlated replies to
the authenticated backend; unrelated personal mail never leaves the machine. For the
``outlook_local`` send route it also submits backend send intents through ``MailItem.Send``.

Everything that touches Outlook (COM/OOM) runs on one dedicated STA thread with a live message
pump (``sta_runtime``); pywin32 is imported lazily so the package and its tests run on Linux with
in-memory fakes (``outlook_bridge.testing``).
"""

from __future__ import annotations

__version__ = "0.1.0"
WORKER_SOFTWARE: str = f"suv-deals-outlook-bridge/{__version__}"

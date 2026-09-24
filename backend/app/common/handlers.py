"""Import side-effect registrations (job handlers, mock AI handlers) in one place so the API
process, workers and tests all see the same set."""

from app.conversations import assist as _assist  # noqa: F401
from app.integrations import teams_service as _teams_service  # noqa: F401
from app.postcall import service as _postcall  # noqa: F401
from app.privacy import retention as _retention  # noqa: F401

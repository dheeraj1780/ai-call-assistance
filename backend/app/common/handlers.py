"""Import side-effect registrations (job handlers, mock AI handlers) in one place so the API
process, workers and tests all see the same set."""

import app.agendas.service
import app.copilot.engine
import app.knowledge.service
import app.privacy.retention  # noqa: F401  retention.sweep job

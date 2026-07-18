"""Correlation ID middleware.

Injects a unique correlation ID into every request for end-to-end
request tracing across the proxy, backend, and executor layers.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Middleware that ensures every request has a correlation ID.

    - If the X-Correlation-ID header is present (from Nginx proxy),
      it is passed through.
    - Otherwise, a new UUIDv4 is generated.
    - The correlation ID is attached to request.state and echoed in
      the response header.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        corr_id = request.headers.get("X-Correlation-ID", str(uuid4()))
        request.state.correlation_id = corr_id

        response = await call_next(request)
        response.headers["X-Correlation-ID"] = corr_id
        return response

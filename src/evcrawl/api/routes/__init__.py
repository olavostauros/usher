"""The API's routers; ``evcrawl.api.app.create_app`` mounts them."""

from fastapi import Request

from evcrawl.api.db import Reader


def get_db(request: Request) -> Reader:
    """The app's ``Reader`` (``app.state.db``), as a dependency tests can override."""
    db: Reader = request.app.state.db
    return db

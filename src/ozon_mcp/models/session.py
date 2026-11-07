"""Session and login DTOs."""

from collections.abc import Callable

from pydantic import model_serializer

from ozon_mcp.models.base import OzonModel
from ozon_mcp.models.catalog import OwnedSearchProbe
from ozon_mcp.models.enums import LoginStage


class SessionStatus(OzonModel):
    """Whether the stored session can still act as the account, and what for.

    ``backup_available`` matters when it cannot: a kept copy is restored
    automatically, so a signed-out session with a backup usually recovers
    without anyone being asked for a code.

    The ``*_enabled`` flags are the operator's permissions, not something a
    caller can change. ``cart_enabled`` permits quantity and selection changes;
    ``writes_enabled`` also permits favorites, lists, checkout options,
    cancellations and existing-order payments. ``orders_enabled`` independently
    permits placing an order. They are reported before a tool refuses halfway
    through a plan.
    """

    signed_in: bool = False
    user_id: str | None = None
    backup_available: bool = False
    cart_enabled: bool = False
    writes_enabled: bool = False
    orders_enabled: bool = False
    detail: str | None = None
    owned_search: OwnedSearchProbe | None = None
    """Owned-page probe. Present only when ``OZON_PUBLIC_DIAGNOSTICS`` is set."""

    @model_serializer(mode="wrap")
    def _omit_unset_probe(self, handler: Callable[[object], dict[str, object]]) -> dict[str, object]:
        data = handler(self)
        if data.get("owned_search") is None:
            data.pop("owned_search")
        return data


class LoginStep(OzonModel):
    """Where an interactive login has got to.

    Ozon sends a one-time code out of band, so the login is necessarily two
    calls with a person in between; ``stage`` says which one is due.
    """

    stage: LoginStage
    login: str | None = None
    detail: str | None = None

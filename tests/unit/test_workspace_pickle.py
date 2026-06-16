"""Unit tests for ``Workspace`` picklability (WS2 — sandbox state persistence).

The desktop sandbox persists per-thread Python state by pickling the user
namespace, which can hold a live :class:`Workspace`. A naive pickle is unsafe
for two reasons:

1. The lazily-built service handles wrap an :class:`httpx.Client` (sockets) and
   async clients — unpicklable.
2. ``pydantic.SecretStr`` pickles to **plaintext**, so a naive snapshot would
   write the service-account secret / OAuth token into ``threads.db``.

``Workspace.__getstate__``/``__setstate__`` must therefore:

- keep the picklable scalars (``_session`` / ``_account_name`` /
  ``_initial_workspace_id``);
- drop the unpicklable service handles (rebuilt lazily on next use);
- **scrub the credential** so no secret ever reaches the pickle bytes.

On restore the worker re-injects credentials from its environment, so the
secret never needs to travel.

Reference: ``../mixpanel-lab/SANDBOX-STATE-PLAN.md`` WS2 §1 + Decision #3.
"""

from __future__ import annotations

import pickle

import pytest
from pydantic import SecretStr

from mixpanel_headless._internal import api_client as api_client_mod
from mixpanel_headless._internal.api_client import MixpanelAPIClient
from mixpanel_headless._internal.auth.account import (
    OAuthTokenAccount,
    ServiceAccount,
)
from mixpanel_headless._internal.auth.session import (
    Project,
    Session,
    WorkspaceRef,
)
from mixpanel_headless._internal.pyodide_transport import PyfetchTransport
from mixpanel_headless.workspace import Workspace

# A unique, easily-greppable secret. The security gate asserts these exact
# bytes never appear in the pickle stream.
_SENTINEL_SECRET = "SENTINEL_SECRET_d41d8cd98f00b204"
_SENTINEL_TOKEN = "SENTINEL_TOKEN_e3b0c44298fc1c14"


def _sa_session() -> Session:
    """Build a service-account session carrying the sentinel secret.

    Returns:
        A :class:`Session` with a :class:`ServiceAccount` whose secret is the
        module-level sentinel, a project, and a pinned workspace.
    """
    return Session(
        account=ServiceAccount(
            name="team",
            region="us",
            username="sa.user",
            secret=SecretStr(_SENTINEL_SECRET),
            default_project="3713224",
        ),
        project=Project(id="12345"),
        workspace=WorkspaceRef(id=42),
    )


class TestWorkspacePickleRoundTrip:
    """Scalars survive, services reset to ``None``, lazy rebuild works."""

    def test_scalars_survive_round_trip(self) -> None:
        """Session / account / workspace-id survive ``dumps`` → ``loads``."""
        ws = Workspace(session=_sa_session())
        restored = pickle.loads(pickle.dumps(ws))

        assert restored.account.name == "team"
        assert restored.account.region == "us"
        assert restored.account.default_project == "3713224"
        assert restored._session.project.id == "12345"
        assert restored._account_name == "team"
        assert restored._initial_workspace_id == 42

    def test_service_handles_are_none_after_unpickle(self) -> None:
        """All lazily-built service handles reset to ``None`` after restore."""
        ws = Workspace(session=_sa_session())
        # Touch lazy services so they are non-None before pickling.
        ws._get_api_client()
        assert ws._api_client is not None

        restored = pickle.loads(pickle.dumps(ws))

        assert restored._api_client is None
        assert restored._discovery is None
        assert restored._live_query is None
        assert restored._me_service is None
        assert restored._replays_svc is None

    def test_lazy_rebuild_reregisters_pyfetch_under_emscripten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh ``_get_api_client`` rebuilds and re-registers PyfetchTransport.

        Under a stubbed ``is_emscripten`` the rebuilt client must self-register
        :class:`PyfetchTransport`, mirroring the live runtime path.
        """
        monkeypatch.setattr(api_client_mod, "is_emscripten", lambda: True)

        ws = Workspace(session=_sa_session())
        restored = pickle.loads(pickle.dumps(ws))

        assert restored._api_client is None
        client = restored._get_api_client()
        assert isinstance(client, MixpanelAPIClient)
        assert isinstance(client._transport, PyfetchTransport)
        client.close()


class TestWorkspacePickleSecretScrub:
    """The security gate: no secret material reaches the pickle stream."""

    def test_service_account_secret_absent_from_pickle_bytes(self) -> None:
        """The service-account secret never appears in ``pickle.dumps(ws)``."""
        ws = Workspace(session=_sa_session())
        # Sanity: the live object still holds the real secret.
        assert ws.account.secret.get_secret_value() == _SENTINEL_SECRET  # type: ignore[union-attr]

        blob = pickle.dumps(ws)

        assert _SENTINEL_SECRET.encode() not in blob

    def test_oauth_token_absent_from_pickle_bytes(self) -> None:
        """An inline OAuth bearer token never appears in the pickle stream."""
        session = Session(
            account=OAuthTokenAccount(
                name="ci",
                region="us",
                token=SecretStr(_SENTINEL_TOKEN),
                default_project="1",
            ),
            project=Project(id="2"),
        )
        ws = Workspace(session=session)

        blob = pickle.dumps(ws)

        assert _SENTINEL_TOKEN.encode() not in blob

    def test_restored_workspace_carries_no_live_secret(self) -> None:
        """After restore (no env creds), the scrubbed secret is blank."""
        ws = Workspace(session=_sa_session())
        restored = pickle.loads(pickle.dumps(ws))

        # Identity survives, but the secret was scrubbed (re-injected from env
        # on the worker; absent here, so it stays blank).
        assert restored.account.name == "team"
        assert restored.account.secret.get_secret_value() == ""


class TestWorkspacePickleCredentialRehydration:
    """On restore the secret is re-injected from the worker environment."""

    def test_service_account_secret_rehydrated_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``MP_SECRET`` in the worker env re-hydrates the scrubbed secret."""
        ws = Workspace(session=_sa_session())
        blob = pickle.dumps(ws)
        # The worker injects the real secret into its environment at restore.
        monkeypatch.setenv("MP_SECRET", "reinjected-secret")

        restored = pickle.loads(blob)

        assert restored.account.secret.get_secret_value() == "reinjected-secret"
        # The sentinel still never traveled in the bytes.
        assert _SENTINEL_SECRET.encode() not in blob

    def test_oauth_token_rehydrated_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``MP_OAUTH_TOKEN`` re-hydrates a scrubbed inline bearer token."""
        session = Session(
            account=OAuthTokenAccount(
                name="ci",
                region="us",
                token=SecretStr(_SENTINEL_TOKEN),
                default_project="1",
            ),
            project=Project(id="2"),
        )
        ws = Workspace(session=session)
        blob = pickle.dumps(ws)
        monkeypatch.setenv("MP_OAUTH_TOKEN", "reinjected-token")

        restored = pickle.loads(blob)

        assert restored.account.token.get_secret_value() == "reinjected-token"
        assert _SENTINEL_TOKEN.encode() not in blob

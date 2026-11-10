import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from illo.connectors.config import Connector
from tests.fakes import FakeGoogleClient


@pytest.fixture
async def google_app(app):
    ctx = app.state.ctx
    ctx.config.vault_key = SecretStr(Fernet.generate_key().decode())
    ctx.workspace = ctx.workspace.model_copy(
        update={"connectors": [Connector(key="google", kind="google", scope="personal")]}
    )
    ctx.google_client = FakeGoogleClient()
    return app

"""Firestore emulator helpers shared by the emulator-marked tests.

The emulator tests run only when FIRESTORE_EMULATOR_HOST is set (`gcloud emulators
firestore start`); otherwise they are skipped so the default `uv run pytest` needs
no Google tooling. CI starts the emulator in a dedicated job.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

EMULATOR_HOST_VAR = "FIRESTORE_EMULATOR_HOST"
EMULATOR_PROJECT = "emulator-test"
DATABASE = "(default)"


def emulator_host() -> str | None:
    return os.environ.get(EMULATOR_HOST_VAR) or None


requires_emulator = pytest.mark.skipif(
    emulator_host() is None, reason=f"{EMULATOR_HOST_VAR} is not set"
)


async def wipe_emulator(host: str, project: str = EMULATOR_PROJECT) -> None:
    """Delete every document in the emulator database (emulator-only REST endpoint)."""
    url = f"http://{host}/emulator/v1/projects/{project}/databases/{DATABASE}/documents"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.delete(url)
    response.raise_for_status()


@pytest.fixture
async def firestore_client() -> AsyncIterator[Any]:
    """A `google.cloud.firestore.AsyncClient` bound to the emulator, emptied afterwards."""
    host = emulator_host()
    if host is None:
        pytest.skip(f"{EMULATOR_HOST_VAR} is not set")
    from google.cloud import firestore

    client = firestore.AsyncClient(project=EMULATOR_PROJECT, database=DATABASE)
    try:
        yield client
    finally:
        client.close()
        await wipe_emulator(host)

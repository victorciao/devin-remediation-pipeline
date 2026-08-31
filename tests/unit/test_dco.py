"""DCO trailer recognition for live publication."""

from __future__ import annotations

import pytest

from pipeline.__main__ import _SIGNOFF_TRAILER


@pytest.mark.parametrize(
    "message",
    (
        "Signed-off-by: Devin AI <158243242+devin-ai-integration[bot]@users.noreply.github.com>",
        "Signed-off-by: Devin <devin@example.invalid>",
    ),
)
def test_signoff_trailer_accepts_single_and_multiword_names(message: str) -> None:
    assert _SIGNOFF_TRAILER.search(message) is not None


def test_signoff_trailer_accepts_one_of_several_trailers() -> None:
    message = (
        "Change the implementation\n\n"
        "Co-Authored-By: Another Contributor <another@example.invalid>\n"
        "Signed-off-by: Devin AI <158243242+devin-ai-integration[bot]@users.noreply.github.com>\n"
    )

    assert _SIGNOFF_TRAILER.search(message) is not None


@pytest.mark.parametrize(
    "message",
    (
        "Change the implementation without a trailer\n",
        "Signed-off-by: Devin AI\n",
    ),
)
def test_signoff_trailer_rejects_missing_or_unbracketed_address(message: str) -> None:
    assert _SIGNOFF_TRAILER.search(message) is None

"""A deliberately tiny fixture used to demonstrate an agentic bug fix."""


def normalize_tag(tag: str) -> str:
    """Normalize a user-supplied tag for comparison."""

    return tag.lower()

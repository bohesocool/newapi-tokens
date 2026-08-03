"""Shared model-group matching rules for dashboard statistics."""

# Keep the existing Mini behaviour (model name contains ``-mini``) and add
# models whose name ends in ``-luna`` to the same group.
MINI_MODEL_LIKE_PATTERNS = ("%-mini%", "%-luna")


def mini_model_sql(column="model_name"):
    """Return a parameterized PostgreSQL condition for Mini-family models."""
    return "(" + " OR ".join(f"{column} ILIKE %s" for _ in MINI_MODEL_LIKE_PATTERNS) + ")"


def model_group(model_name):
    """Return ``mini``, ``other``, or ``None`` for a nullable model name."""
    if model_name is None:
        return None
    normalized = str(model_name).lower()
    if "-mini" in normalized or normalized.endswith("-luna"):
        return "mini"
    return "other"

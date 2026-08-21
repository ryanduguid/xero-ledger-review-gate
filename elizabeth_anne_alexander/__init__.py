"""A synthetic, fixed-policy boundary for review-only trial-balance analysis."""

from .gateway import evaluate, validate_review, write_evaluation
from .version import __version__

__all__ = ["__version__", "evaluate", "validate_review", "write_evaluation"]

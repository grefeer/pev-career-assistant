"""Compatibility shim - source of truth lives in the skill runtime package."""

import sys

from skill.job_discovery.runtime import classify_url as _impl

sys.modules[__name__] = _impl

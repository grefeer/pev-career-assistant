"""Compatibility shim - source of truth lives in the skill runtime package."""

import sys

from skill.job_discovery.runtime import job_discovery as _impl

sys.modules[__name__] = _impl

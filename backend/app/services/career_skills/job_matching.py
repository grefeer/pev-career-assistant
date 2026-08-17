"""Compatibility shim - source of truth lives in the skill runtime package."""

import sys

from skill.job_matching.runtime import job_matching as _impl

sys.modules[__name__] = _impl

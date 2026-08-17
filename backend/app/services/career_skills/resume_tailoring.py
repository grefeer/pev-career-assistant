"""Compatibility shim - source of truth lives in the skill runtime package."""

import sys

from skill.resume_tailoring.runtime import resume_tailoring as _impl

sys.modules[__name__] = _impl

"""Compatibility shim - source of truth lives in the skill runtime package."""

import sys

from skill.career_planning.runtime import career_planning as _impl

sys.modules[__name__] = _impl

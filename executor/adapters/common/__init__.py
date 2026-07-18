"""Common control fill strategies shared across site adapters.

Strategies are extracted only when attested by regression tests on
at least two independent site adapters.  Each module exports a single
``fill_*`` function that returns ``FillResult``.
"""

from executor.adapters.common.text_input import fill_text_input  # noqa: F401
from executor.adapters.common.select import fill_select  # noqa: F401
from executor.adapters.common.file_upload import upload_via_input  # noqa: F401
from executor.adapters.common.wait_utils import wait_for_text_present  # noqa: F401

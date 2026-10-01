# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Wait for an eventually consistent system to converge."""

import logging
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


def until[T](
    check: Callable[[], T],
    *,
    timeout: float,
    what: str,
    retry: tuple[type[Exception], ...] = (AssertionError,),
    interval: float = 5,
) -> T:
    """Call check until it stops raising one of the retry exceptions, and return what it returns.

    Once timeout seconds pass, re-raise check's last exception, so the failure
    says what was still wrong rather than only that time ran out.
    """
    log.info("Waiting up to %ds for %s", timeout, what)
    start = time.monotonic()
    while True:
        try:
            result = check()
        except retry as e:
            if time.monotonic() - start + interval > timeout:
                e.add_note(f"Still failing after waiting {timeout:.0f}s for {what}.")
                raise
            time.sleep(interval)
            continue
        log.info("Done after %.0fs", time.monotonic() - start)
        return result

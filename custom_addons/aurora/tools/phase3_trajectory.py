import logging
from typing import Optional

from .util import AuroraPipelineError

_logger = logging.getLogger(__name__)


def main(
    phase2_report: str,
    output_dir: str,
    org: str,
    repo: str,
    lang: str,
    log_callback: Optional[callable] = None,
) -> dict:
    raise AuroraPipelineError(
        "Phase 3 (Trajectory Generation) is not yet implemented. "
        "This phase will handle inference with AI agents, evaluation, "
        "and pass@k summary generation."
    )

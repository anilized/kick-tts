"""Engine factory. This module must never import torch; EmaEngine imports it lazily inside app.engine.ema."""
from __future__ import annotations

import logging

from app.interfaces import Engine

log = logging.getLogger(__name__)


def get_engine(settings) -> Engine:
    if settings.FAKE_ENGINE:
        from app.engine.fake import FakeEngine

        log.info("FAKE_ENGINE=1: using FakeEngine (sine tone)")
        return FakeEngine()
    try:
        # app.engine.ema imports nothing heavy; torch/ema_lightning are imported inside the constructor,
        # after the OMP/MKL env is set. So the ImportError (torch or ema_lightning not installed) surfaces here.
        from app.engine.ema import EmaEngine

        # Missing weights raise WeightsNotFoundError (a FileNotFoundError, not an ImportError) and are NOT
        # caught: in real mode the pod must fail visibly instead of quietly serving a sine tone.
        return EmaEngine(settings)
    except ImportError as exc:
        from app.engine.fake import FakeEngine

        log.warning(
            "EMA engine unavailable (%s: %s); FALLING BACK TO FakeEngine - no real speech will be produced",
            type(exc).__name__,
            exc,
        )
        return FakeEngine()

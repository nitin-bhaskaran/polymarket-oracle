"""Application-level TLS setup using the operating system trust store."""

import logging

logger = logging.getLogger("core.tls")


def configure_system_trust() -> bool:
    """
    Make Python HTTPS clients use the native OS certificate store.

    This is application startup code, which is the supported place to use
    truststore.inject_into_ssl(). Verification remains enabled.
    """
    try:
        import truststore
    except ImportError:
        return False
    try:
        truststore.inject_into_ssl()
        return True
    except Exception as exc:
        logger.warning("Could not initialize system TLS trust store: %s", exc)
        return False

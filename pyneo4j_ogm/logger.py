import logging

from pyneo4j_ogm.env import EnvVariable, from_env

enable_logging = int(from_env(EnvVariable.LOGGING_ENABLED, 1))
log_level = int(from_env(EnvVariable.LOGLEVEL, logging.WARNING))

logger = logging.getLogger("pyneo4j-ogm")
logger.setLevel(logging.WARNING)

handler = logging.StreamHandler()
handler.setLevel(logging.WARNING)

formatter = logging.Formatter("[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

if not bool(enable_logging):
    logger.disabled = True

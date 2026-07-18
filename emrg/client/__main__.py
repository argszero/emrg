"""Entry point for EMRG client: python -m emrg.client"""
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
from emrg.config import ensure_config
from emrg.client.app import run_client

ensure_config()
run_client()

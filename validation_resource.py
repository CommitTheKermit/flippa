"""Coordinate existing web actions with the remote validation worker."""
from contextlib import nullcontext
import os
from pathlib import Path
from validation_jobs import load_config, resource_lock


def remote_resource():
    config=os.environ.get('PLIPA_VALIDATION_CONFIG')
    return resource_lock(Path(load_config(config)['state_dir'])/'device.lock') if config else nullcontext()

import inspect
import logging
from functools import wraps
from logging import (  # pylint: disable=unused-import
    DEBUG,
    ERROR,
    INFO,
    WARNING,
)
from typing import List

from tqdm import tqdm

from ..utility import temp_attr


LOGGER = logging.getLogger(__name__)

_PROGRESSBARS: List[tqdm] = []


@wraps(LOGGER.log)
def log(*args, **kwargs):
    # pylint: disable=missing-function-docstring
    for pbar in _PROGRESSBARS:
        pbar._tqdm_instance.clear()  # pylint: disable=protected-access
    msg_frame = inspect.currentframe().f_back
    with temp_attr(
        LOGGER,
        "findCaller",
        lambda self, stack_info=None: (
            msg_frame.f_code.co_filename,
            msg_frame.f_lineno,
            msg_frame.f_code.co_name,
            None,
        ),
    ):
        LOGGER.log(*args, **kwargs)
    for pbar in _PROGRESSBARS:
        pbar._tqdm_instance.refresh()  # pylint: disable=protected-access


def set_level(level: int):
    r"""Set logging level"""
    LOGGER.setLevel(level)


class Progressbar:
    r"""
    Context manager for creating progress bars compatible with the logging
    environment
    """

    def __init__(self, iterable, /, *, position=-1, **kwargs):
        self._iterable = iterable
        self._position = position
        self._kwargs = kwargs
        self._tqdm_instance = None

    def __enter__(self):
        # pylint: disable=no-member,attribute-defined-outside-init
        # ^ disable false positive linting errors
        self._tqdm_instance = tqdm(self._iterable, **self._kwargs)
        _PROGRESSBARS.insert(self._position % (len(_PROGRESSBARS) + 1), self)
        for i, pbar in enumerate(reversed(_PROGRESSBARS)):
            pbar._tqdm_instance.pos = i
            pbar._tqdm_instance.refresh()
        return self._tqdm_instance

    def __exit__(self, err_type, err, tb):
        _PROGRESSBARS.remove(self)
        self._tqdm_instance.close()

from __future__ import annotations

import copy
import zipfile


def copy_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo, data: bytes, filename=None):
    """Write data under a copy of info, keeping its original external attributes.

    zipfile replaces zero external attributes with 0o600 << 16 while writing. They are
    recorded only in the central directory, which is written on close, so restoring
    them here keeps the original entry record.
    """
    entry = copy.copy(info)
    if filename is not None:
        entry.filename = filename
    archive.writestr(entry, data)
    entry.external_attr = info.external_attr
    return entry

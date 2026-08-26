"""
packing.py — pack a modified Health Connect DB into the import ZIP.

Provides:
- ``pack_zip()`` — create a ZIP with a single entry ``health_connect_export.db``
  (ZIP_DEFLATED compression).

The ZIP format matches the Health Connect import expectation exactly:
  - Single entry named ``health_connect_export.db``
  - Compression: ZIP_DEFLATED
"""

import zipfile
from pathlib import Path


def pack_zip(db_path: str | Path, out_zip: str | Path) -> None:
    """
    Pack a Health Connect database file into the import ZIP format.

    Creates a ZIP file at ``out_zip`` containing exactly one entry:
    ``health_connect_export.db`` (deflated).

    Parameters
    ----------
    db_path : str | Path
        Path to the (modified) ``health_connect_export.db`` file.
    out_zip : str | Path
        Path for the output ZIP file.

    Raises
    ------
    OSError
        If the source db file does not exist or cannot be read.
    zipfile.BadZipFile
        If the ZIP creation fails.

    Example
    -------
    >>> pack_zip("/tmp/health_connect_export_modified.db",
    ...          "/tmp/Health Connect full.zip")
    """
    db_path = Path(db_path)
    out_zip = Path(out_zip)

    with zipfile.ZipFile(
        out_zip, mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as zf:
        # The entry name must be exactly "health_connect_export.db"
        # to be recognized by the Health Connect import pipeline.
        zf.write(db_path, arcname="health_connect_export.db")

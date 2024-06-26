from __future__ import annotations

__all__ = ["CBRArchive"]

import logging
from pathlib import Path

from rarfile import RarExecError, RarFile

from perdoo.archives._base import BaseArchive

LOGGER = logging.getLogger(__name__)


class CBRArchive(BaseArchive):
    def list_filenames(self: CBRArchive) -> list[str]:
        try:
            with RarFile(self.path) as stream:
                return stream.namelist()
        except RarExecError:
            LOGGER.exception("Unable to read %s", self.path.name)
            return []

    def read_file(self: CBRArchive, filename: str) -> bytes:
        try:
            with RarFile(self.path) as stream:
                return stream.read(filename)
        except RarExecError:
            LOGGER.exception("Unable to read %s", self.path.name)
            return b""

    def extract_files(self: CBRArchive, destination: Path) -> bool:
        try:
            with RarFile(self.path, "r") as stream:
                stream.extractall(path=destination)
            return True
        except RarExecError:
            LOGGER.exception("")
            return False

    @classmethod
    def archive_files(
        cls: type[CBRArchive], src: Path, output_name: str, files: list[Path] | None = None
    ) -> Path | None:
        raise NotImplementedError("Unable to create archive in CBR format")

    @staticmethod
    def convert(old_archive: BaseArchive) -> CBRArchive | None:
        raise NotImplementedError("Unable to convert archive to CBR format")

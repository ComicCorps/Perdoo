from __future__ import annotations

import logging
import shutil
from argparse import ArgumentParser, Namespace
from datetime import date
from pathlib import Path
from platform import python_version
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from perdoo import ARCHIVE_EXTENSIONS, IMAGE_EXTENSIONS, __version__, setup_logging
from perdoo.archives import BaseArchive, CB7Archive, CBTArchive, CBZArchive, get_archive
from perdoo.console import CONSOLE
from perdoo.models import ComicInfo, Metadata, MetronInfo
from perdoo.models.metadata import Format, Meta, Tool
from perdoo.services import Comicvine, League, Marvel, Metron
from perdoo.settings import OutputFormat, Settings
from perdoo.utils import (
    comic_to_metadata,
    create_metadata,
    list_files,
    metadata_to_comic,
    metadata_to_metron,
    metron_to_metadata,
    sanitize,
)

LOGGER = logging.getLogger("perdoo")


def parse_arguments() -> Namespace:
    parser = ArgumentParser(prog="Perdoo", allow_abbrev=False)
    parser.version = __version__
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--version", action="version")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def convert_collection(path: Path, output: OutputFormat) -> None:
    format_, archive_type = {
        OutputFormat.CB7: (".cb7", CB7Archive),
        OutputFormat.CBT: (".cbt", CBTArchive),
    }.get(output, (".cbz", CBZArchive))
    formats = list(ARCHIVE_EXTENSIONS)
    formats.remove(format_)
    for file in list_files(path, *formats):
        LOGGER.info("Converting %s to %s format", file.name, output.name)
        archive = get_archive(path=file)
        archive_type.convert(old_archive=archive)


def read_archive(archive: BaseArchive) -> tuple[Metadata, MetronInfo, ComicInfo]:
    filenames = archive.list_filenames()
    metadata = None
    try:
        if "/Metadata.xml" in filenames:
            metadata = Metadata.from_bytes(content=archive.read_file(filename="/Metadata.xml"))
        elif "Metadata.xml" in filenames:
            metadata = Metadata.from_bytes(content=archive.read_file(filename="Metadata.xml"))
    except ValidationError:
        LOGGER.error("%s contains an invalid Metadata file", archive.path.name)  # noqa: TRY400
    metron_info = None
    try:
        if "/MetronInfo.xml" in filenames:
            metron_info = MetronInfo.from_bytes(
                content=archive.read_file(filename="/MetronInfo.xml")
            )
        elif "MetronInfo.xml" in filenames:
            metron_info = MetronInfo.from_bytes(
                content=archive.read_file(filename="MetronInfo.xml")
            )
    except ValidationError:
        LOGGER.error("%s contains an invalid MetronInfo file", archive.path.name)  # noqa: TRY400
    comic_info = None
    try:
        if "/ComicInfo.xml" in filenames:
            comic_info = ComicInfo.from_bytes(content=archive.read_file(filename="/ComicInfo.xml"))
        elif "ComicInfo.xml" in filenames:
            comic_info = ComicInfo.from_bytes(content=archive.read_file(filename="ComicInfo.xml"))
    except ValidationError:
        LOGGER.error("%s contains an invalid ComicInfo file", archive.path.name)  # noqa: TRY400

    if not metadata:
        if metron_info:
            metadata = metron_to_metadata(metron_info=metron_info)
        elif comic_info:
            metadata = comic_to_metadata(comic_info=comic_info)
        else:
            metadata = create_metadata(archive=archive)
    if not metron_info:
        metron_info = metadata_to_metron(metadata=metadata)
    if not comic_info:
        comic_info = metadata_to_comic(metadata=metadata)
    return metadata, metron_info, comic_info


def fetch_from_services(
    settings: Settings, metainfo: tuple[Metadata, MetronInfo, ComicInfo]
) -> None:
    marvel = None
    if settings.marvel and settings.marvel.public_key and settings.marvel.private_key:
        marvel = Marvel(settings=settings.marvel)
    metron = None
    if settings.metron and settings.metron.username and settings.metron.password:
        metron = Metron(settings=settings.metron)
    comicvine = None
    if settings.comicvine and settings.comicvine.api_key:
        comicvine = Comicvine(settings.comicvine)
    league = None
    if (
        settings.league_of_comic_geeks
        and settings.league_of_comic_geeks.client_id
        and settings.league_of_comic_geeks.client_secret
    ):
        league = League(settings.league_of_comic_geeks)
    if not marvel and not metron and not comicvine and not league:
        LOGGER.warning("No external services configured")
        return

    success = any(
        service and service.fetch(*metainfo) for service in (marvel, metron, comicvine, league)
    )
    if not success:
        LOGGER.warning("Unable to fetch information fron any service")


def generate_filename(root: Path, extension: str, metadata: Metadata) -> Path:
    publisher_filename = metadata.issue.series.publisher.title
    series_filename = (
        f"{metadata.issue.series.title} v{metadata.issue.series.volume}"
        if metadata.issue.series.volume > 1
        else metadata.issue.series.title
    )

    number_str = (
        f"_#{metadata.issue.number.zfill(3 if metadata.issue.format == Format.COMIC else 2)}"
        if metadata.issue.number
        else ""
    )
    format_str = {
        Format.ANNUAL: "_Annual",
        Format.DIGITAL_CHAPTER: "_Chapter",
        Format.GRAPHIC_NOVEL: "_GN",
        Format.HARDCOVER: "_HC",
        Format.TRADE_PAPERBACK: "_TP",
    }.get(metadata.issue.format, "")
    if metadata.issue.format in {Format.ANNUAL, Format.DIGITAL_CHAPTER}:
        issue_filename = sanitize(value=series_filename) + format_str + number_str
    elif metadata.issue.format in {Format.GRAPHIC_NOVEL, Format.HARDCOVER, Format.TRADE_PAPERBACK}:
        issue_filename = sanitize(value=series_filename) + number_str + format_str
    else:
        issue_filename = sanitize(value=series_filename) + number_str

    return (
        root
        / sanitize(value=publisher_filename)
        / sanitize(value=series_filename)
        / f"{issue_filename}.{extension}"
    )


def rename_images(folder: Path, filename: str) -> None:
    image_list = list_files(folder, *IMAGE_EXTENSIONS)
    pad_count = len(str(len(image_list)))
    for index, img_file in enumerate(image_list):
        new_filename = f"{filename}_{str(index).zfill(pad_count)}{img_file.suffix}"
        if img_file.name != new_filename:
            LOGGER.info("Renamed %s to %s", img_file.name, new_filename)
            img_file.rename(folder / f"{filename}-{str(index).zfill(pad_count)}{img_file.suffix}")


def process_pages(
    folder: Path, metadata: Metadata, metron_info: MetronInfo, comic_info: ComicInfo, filename: str
) -> None:
    from perdoo.models.comic_info import Page as ComicPage
    from perdoo.models.metadata import Page as MetadataPage
    from perdoo.models.metron_info import Page as MetronPage

    rename_images(folder=folder, filename=filename)
    image_list = list_files(folder, *IMAGE_EXTENSIONS)
    metadata_pages = set()
    metron_info_pages = set()
    comic_info_pages = set()
    for index, img_file in enumerate(image_list):
        is_final_page = index == len(image_list) - 1
        page = next((x for x in metadata.pages if x.index == index), None)
        metadata_pages.add(
            MetadataPage.from_path(
                file=img_file, index=index, is_final_page=is_final_page, page=page
            )
        )
        page = next((x for x in metron_info.pages if x.image == index), None)
        metron_info_pages.add(
            MetronPage.from_path(file=img_file, index=index, is_final_page=is_final_page, page=page)
        )
        page = next((x for x in comic_info.pages if x.image == index), None)
        comic_info_pages.add(
            ComicPage.from_path(file=img_file, index=index, is_final_page=is_final_page, page=page)
        )
    metadata.pages = sorted(metadata_pages)
    metron_info.pages = sorted(metron_info_pages)
    comic_info.pages = sorted(comic_info_pages)


def start(settings: Settings, force: bool = False) -> None:
    LOGGER.info("Starting Perdoo")
    convert_collection(path=settings.collection_folder, output=settings.output.format)
    for file in list_files(settings.collection_folder, f".{settings.output.format}"):
        archive = get_archive(path=file)
        metadata, metron_info, comic_info = read_archive(archive=archive)

        if not force:
            difference = abs(date.today() - metadata.meta.date_)
            if metadata.meta.tool == Tool() and difference.days < 28:
                continue

        CONSOLE.rule(file.stem)
        LOGGER.info("Processing %s", file.name)
        fetch_from_services(settings=settings, metainfo=(metadata, metron_info, comic_info))
        new_file = generate_filename(
            root=settings.collection_folder,
            extension=settings.output.format.value,
            metadata=metadata,
        )
        with TemporaryDirectory(prefix=f"{new_file.stem}_") as temp_str:
            temp_folder = Path(temp_str)
            if not archive.extract_files(destination=temp_folder):
                return
            process_pages(
                folder=temp_folder,
                metadata=metadata,
                metron_info=metron_info,
                comic_info=comic_info,
                filename=new_file.stem,
            )
            metadata.meta = Meta(date_=date.today())
            if settings.output.create_metadata:
                metadata.to_file(file=temp_folder / "Metadata.xml")
            if settings.output.create_metron_info:
                metron_info.to_file(file=temp_folder / "MetronInfo.xml")
            if settings.output.create_comic_info:
                comic_info.to_file(file=temp_folder / "ComicInfo.xml")
            archive_file = archive.archive_files(src=temp_folder, filename=archive.path.stem)
            if not archive_file:
                LOGGER.critical("Unable to re-archive images")
                continue
            archive.path.unlink(missing_ok=True)
            shutil.move(archive_file, archive.path)
        if file.relative_to(settings.collection_folder) != new_file.relative_to(
            settings.collection_folder
        ):
            LOGGER.info(
                "Organizing comic, moving file to %s",
                new_file.relative_to(settings.collection_folder),
            )
            new_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(file, new_file)


def main() -> None:
    try:
        CONSOLE.print(f"Perdoo v{__version__}")
        CONSOLE.print(f"Python v{python_version()}")

        args = parse_arguments()
        if args.debug:
            CONSOLE.print(f"Args: {args}")
        setup_logging(debug=args.debug)

        settings = Settings.load().save()
        if args.debug:
            CONSOLE.print(f"Settings: {settings}")
        start(settings=settings, force=args.force)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""Provides a CLI util to make updates to normalizer database."""

import logging
from collections.abc import Collection
from timeit import default_timer as timer

import click
from disease.cli import _update_sources as update_disease_sources
from disease.database import create_db as create_disease_db
from disease.schemas import SourceName as DiseaseSourceName

from therapy import SOURCES
from therapy.database.database import (
    AbstractDatabase,
    DatabaseReadError,
    DatabaseWriteError,
    create_db,
)
from therapy.log import configure_logs
from therapy.schemas import SourceName

_logger = logging.getLogger(__name__)


@click.command()
@click.option("--db_url", help="URL endpoint for the application database.")
@click.option("--verbose", "-v", is_flag=True, help="Print result to console if set.")
def check_db(db_url: str, verbose: bool = False) -> None:
    """Perform basic checks on DB health and population. Exits with status code 1
    if DB schema is uninitialized or if critical tables appear to be empty.

    \f
    :param db_url: URL to normalizer database
    :param verbose: if true, print result to console
    """  # noqa: D301
    configure_logs()
    db = create_db(db_url, False)
    if not db.check_schema_initialized():
        if verbose:
            click.echo("Health check failed: DB schema uninitialized.")
        click.get_current_context().exit(1)
    if verbose:
        click.echo("DB health check successful: tables appear complete.")


def _delete_source(n: SourceName, db: AbstractDatabase) -> float:
    """Delete individual source data.

    :param n: name of source to delete
    :param db: database instance
    :return: time taken (in seconds) to delete
    """
    msg = f"Deleting {n.value}..."
    click.echo(f"\n{msg}")
    _logger.info(msg)
    start_delete = timer()
    db.delete_source(n)
    end_delete = timer()
    delete_time = end_delete - start_delete
    msg = f"Deleted {n.value} in {delete_time:.5f} seconds."
    click.echo(f"{msg}\n")
    _logger.info(msg)
    return delete_time


def _load_source(
    name: SourceName,
    db: AbstractDatabase,
    delete_time: float,
    processed_ids: list[str],
    use_existing: bool,
) -> None:
    """Load individual source data.

    :param n: name of source
    :param db: database instance
    :param delete_time: time taken (in seconds) to run deletion
    :param processed_ids: in-progress list of processed therapy IDs
    :param use_existing: if True, use most recent local data files instead of
        fetching from remote
    """
    msg = f"Loading {name.value}..."
    click.echo(msg)
    _logger.info(msg)
    start_load = timer()

    # used to get source class name from string
    try:
        from therapy.etl import (  # noqa: F401
            ChEMBL,
            ChemIDplus,
            DrugBank,
            DrugsAtFDA,
            EtlError,
            GuideToPHARMACOLOGY,
            HemOnc,
            NCIt,
            RxNorm,
            Wikidata,
        )
    except ModuleNotFoundError as e:
        click.echo(
            f"Encountered ModuleNotFoundError attempting to import {e.name}. Are ETL dependencies installed?"
        )
        click.get_current_context().exit()
    SourceClass = eval(name.value)  # noqa: N806, S307

    source = SourceClass(database=db, silent=False)
    try:
        processed_ids += source.perform_etl(use_existing)
    except EtlError as e:
        _logger.exception("Encountered ETL error while loading source %s", name)
        click.echo(f"Encountered error while loading {name}: {e}.")
        click.get_current_context().exit()
    end_load = timer()
    load_time = end_load - start_load
    msg = f"Loaded {name.value} in {load_time:.5f} seconds."
    click.echo(msg)
    _logger.info(msg)
    msg = f"Total time for {name.value}: {(delete_time + load_time):.5f} seconds."
    click.echo(msg)
    _logger.info(msg)


def _delete_normalized_data(database: AbstractDatabase) -> None:
    """Delete normalized concepts

    :param database: DB instance
    """
    click.echo("\nDeleting normalized records...")
    start_delete = timer()
    try:
        database.delete_normalized_concepts()
    except (DatabaseReadError, DatabaseWriteError) as e:
        click.echo(f"Encountered exception during normalized data deletion: {e}")
    end_delete = timer()
    delete_time = end_delete - start_delete
    click.echo(f"Deleted normalized records in {delete_time:.5f} seconds.")


def _load_merge(db: AbstractDatabase, processed_ids: set[str]) -> None:
    """Load merged concepts

    :param db: database instance
    :param processed_ids: in-progress list of processed therapy IDs
    """
    start = timer()
    _delete_normalized_data(db)
    if not processed_ids:
        processed_ids = db.get_all_concept_ids()
    try:
        from therapy.etl.merge import Merge
    except ModuleNotFoundError as e:
        click.echo(
            f"Encountered ModuleNotFoundError attempting to import {e.name}. Are ETL dependencies installed?"
        )
        click.get_current_context().exit()

    merge = Merge(database=db, silent=False)
    click.echo("Constructing normalized records...")
    merge.create_merged_concepts(processed_ids)
    end = timer()
    click.echo(
        f"Merged concept generation completed in " f"{(end - start):.5f} seconds"
    )


def _update_normalizer(
    sources: Collection[SourceName],
    db: AbstractDatabase,
    update_merged: bool,
    use_existing: bool,
) -> None:
    """Update selected normalizer sources.

    :param sources: names of sources to update
    :param db: database instance
    :param update_merged: if true, retain processed records to use in updating merged
        records
    :param use_existing: if True, use most recent local version of source data instead of
        fetching from remote
    """
    processed_ids: list[str] = []
    for n in sources:
        delete_time = _delete_source(n, db)
        _load_source(n, db, delete_time, processed_ids, use_existing)

    if update_merged:
        _load_merge(db, set(processed_ids))


def _ensure_diseases_updated(from_local: bool) -> None:
    """Check disease DB. If it appears unpopulate, run a full update.

    :param from_local: if True, update from most recent locally available data
    """
    disease_db = create_disease_db()
    if (
        not disease_db.check_schema_initialized()
        or not disease_db.check_tables_populated()
    ):
        update_disease_sources(list(DiseaseSourceName), disease_db, True, from_local)


@click.command()
@click.option("--sources", help="The source(s) you wish to update separated by spaces.")
@click.option("--aws_instance", is_flag=True, help="Using AWS DynamodDB instance.")
@click.option("--db_url", help="URL endpoint for the application database.")
@click.option("--update_all", is_flag=True, help="Update all normalizer sources.")
@click.option(
    "--update_merged",
    is_flag=True,
    help="Update concepts for normalize endpoint from accepted sources.",
)
@click.option(
    "--use_existing",
    is_flag=True,
    default=False,
    help="Use most recent local source data instead of fetching latest version",
)
def update_normalizer_db(
    sources: str,
    aws_instance: bool,
    db_url: str,
    update_all: bool,
    update_merged: bool,
    use_existing: bool,
) -> None:
    """Update selected normalizer source(s) in the therapy database. For example, the
    following command will update RxNorm and Wikidata data, using a database connection at port 8001:

    % therapy_norm_update --sources="rxnorm wikidata" --db_url=http://localhost:8001

    \f
    :param sources: names of sources to update, comma-separated
    :param aws_instance: if true, use cloud instance
    :param db_url: URI pointing to database
    :param update_all: if true, update all sources (ignore `normalizer` parameter)
    :param update_merged: if true, update normalized records
    :param use_existing: if True, use most recent local data instead of fetching latest version
    """  # noqa: D301
    configure_logs()
    db = create_db(db_url, aws_instance)

    if update_all:
        _ensure_diseases_updated(use_existing)
        _update_normalizer(list(SourceName), db, update_merged, use_existing)
    elif not sources:
        if update_merged:
            _load_merge(db, set())
        else:
            ctx = click.get_current_context()
            click.echo(
                "Must either enter 1 or more sources, or use `--update_all` parameter"
            )
            click.echo(ctx.get_help())
            ctx.exit()
    else:
        sources_split = sources.lower().split()

        if len(sources_split) == 0:
            msg = "Must enter 1 or more source names to update"
            raise ValueError(msg)

        non_sources = set(sources_split) - set(SOURCES)

        if len(non_sources) != 0:
            msg = f"Not valid source(s): {non_sources}"
            raise ValueError(msg)

        parsed_source_names = {SourceName(SOURCES[s]) for s in sources_split}
        if (
            SourceName.CHEMBL in parsed_source_names
            or SourceName.HEMONC in parsed_source_names
        ):
            _ensure_diseases_updated(use_existing)
        _update_normalizer(parsed_source_names, db, update_merged, use_existing)


if __name__ == "__main__":
    update_normalizer_db()

"""Pytest test config tools."""

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from therapy.database.database import AWS_ENV_VAR_NAME, AbstractDatabase, create_db
from therapy.etl.base import Base
from therapy.query import QueryHandler
from therapy.schemas import MatchType, SourceSearchMatches, Therapy

_logger = logging.getLogger(__name__)


def pytest_collection_modifyitems(items):
    """Modify test items in place to ensure test modules run in a given order.
    When creating new test modules, be sure to add them here.
    """
    MODULE_ORDER = [  # noqa: N806
        "test_schemas",
        "test_chembl",
        "test_chemidplus",
        "test_drugbank",
        "test_drugsatfda",
        "test_guidetopharmacology",
        "test_hemonc",
        "test_ncit",
        "test_rxnorm",
        "test_wikidata",
        "test_merge",
        "test_database",
        "test_query",
        "test_emit_warnings",
        "test_disease_indication",
    ]
    items.sort(key=lambda i: MODULE_ORDER.index(i.module.__name__))


def pytest_addoption(parser):
    """Add custom commands to pytest invocation.

    See https://docs.pytest.org/en/7.1.x/reference/reference.html#parser
    """
    parser.addoption(
        "--verbose-logs",
        action="store_true",
        default=False,
        help="show noisy module logs",
    )


def pytest_configure(config):
    """Configure pytest setup."""
    logging.getLogger(__name__).error(config.getoption("--verbose-logs"))
    if not config.getoption("--verbose-logs"):
        logging.getLogger("botocore").setLevel(logging.ERROR)
        logging.getLogger("boto3").setLevel(logging.ERROR)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)


TEST_ROOT = Path(__file__).resolve().parents[1]
TEST_DATA_DIRECTORY = TEST_ROOT / "tests" / "data"
IS_TEST_ENV = os.environ.get("THERAPY_TEST", "").lower() == "true"


def pytest_sessionstart():
    """Wipe DB before testing if in test environment."""
    if IS_TEST_ENV:
        if os.environ.get(AWS_ENV_VAR_NAME):
            pytest.fail(f"Cannot have both THERAPY_TEST and {AWS_ENV_VAR_NAME} set.")
        db = create_db()
        db.drop_db()
        db.initialize_db()


@pytest.fixture(scope="session")
def is_test_env():
    """If true, currently in test environment (i.e. okay to overwrite DB). Downstream
    users should also make sure to check if in a production environment.

    Provided here to be accessible directly within test modules.
    """
    return IS_TEST_ENV


@pytest.fixture(scope="session")
def test_data():
    """Provide test data location to test modules"""
    return TEST_DATA_DIRECTORY


@pytest.fixture(scope="module")
def database():
    """Provide a database instance to be used by tests."""
    db = create_db()
    yield db
    db.close_connection()


@pytest.fixture(scope="session")
def disease_normalizer():
    """Provide mock disease normalizer."""
    with (TEST_DATA_DIRECTORY / "disease_normalization.json").open() as f:
        disease_data = json.load(f)

        def _normalize_disease(query: str):
            return disease_data.get(query.lower())

    return _normalize_disease


@pytest.fixture(scope="module")
def test_source(
    database: AbstractDatabase,
    is_test_env: bool,
    disease_normalizer: Callable,
    test_data: Path,
):
    """Provide query endpoint for testing sources. If THERAPY_TEST is set, will try to
    load DB from test data.

    :param database: test database instance
    :param is_test_env: if true, load from test data
    :param disease_normalizer: mock disease normalizer callback
    :return: factory function that takes an ETL class instance and returns a query
    endpoint.
    """

    def test_source_factory(EtlClass: Base):  # noqa: N803
        if is_test_env:
            _logger.debug("Reloading DB with data from %s", test_data)
            test_class = EtlClass(database, test_data / EtlClass.__name__.lower())  # type: ignore
            test_class._normalize_disease = disease_normalizer  # type: ignore
            test_class.perform_etl(use_existing=True)

        class QueryGetter:
            def __init__(self):
                self._query_handler = QueryHandler(database)
                self._src_name = EtlClass.__name__  # type: ignore

            def search(self, query_str: str):
                resp = self._query_handler.search(query_str, incl=self._src_name)
                return resp.source_matches[self._src_name]

        return QueryGetter()

    return test_source_factory


def _compare_records(actual: Therapy, fixt: Therapy):
    """Check that identity records are identical."""
    assert actual.concept_id == fixt.concept_id
    assert actual.label == fixt.label

    assert (actual.aliases is None) == (fixt.aliases is None)
    if (actual.aliases is not None) and (fixt.aliases is not None):
        assert set(actual.aliases) == set(fixt.aliases)

    assert (actual.trade_names is None) == (fixt.trade_names is None)
    if (actual.trade_names is not None) and (fixt.trade_names is not None):
        assert set(actual.trade_names) == set(fixt.trade_names)

    assert (actual.xrefs is None) == (fixt.xrefs is None)
    if (actual.xrefs is not None) and (fixt.xrefs is not None):
        assert set(actual.xrefs) == set(fixt.xrefs)

    assert (actual.associated_with is None) == (fixt.associated_with is None)
    if (actual.associated_with is not None) and (fixt.associated_with is not None):
        assert set(actual.associated_with) == set(fixt.associated_with)

    assert (not actual.approval_ratings) == (not fixt.approval_ratings)
    if (actual.approval_ratings) and (fixt.approval_ratings):
        assert set(actual.approval_ratings) == set(fixt.approval_ratings)

    assert (actual.approval_year is None) == (fixt.approval_year is None)
    if (actual.approval_year is not None) and (fixt.approval_year is not None):
        assert set(actual.approval_year) == set(fixt.approval_year)

    assert (actual.has_indication is None) == (fixt.has_indication is None)
    if (actual.has_indication is not None) and (fixt.has_indication is not None):
        actual_inds = actual.has_indication.copy()
        fixture_inds = fixt.has_indication.copy()
        assert len(actual_inds) == len(fixture_inds)
        actual_inds.sort(key=lambda x: x.disease_id)
        fixture_inds.sort(key=lambda x: x.disease_id)
        for i in range(len(actual_inds)):
            assert actual_inds[i] == fixture_inds[i]


@pytest.fixture(scope="session")
def compare_records():
    """Provide record comparison function"""
    return _compare_records


def _compare_response(
    response: SourceSearchMatches,
    match_type: MatchType,
    fixture: Therapy | None = None,
    fixture_list: list[Therapy] | None = None,
    num_records: int = 0,
):
    """Check that test response is correct. Only 1 of {fixture, fixture_list}
    should be passed as arguments. num_records should only be passed with fixture_list.

    :param Dict response: response object returned by QueryHandler
    :param MatchType match_type: expected match type
    :param Drug fixture: single Drug object to match response against
    :param List[Drug] fixture_list: multiple Drug objects to match response against
    :param int num_records: expected number of records in response. If not given, tests
        for number of fixture Drugs given (ie, 1 for single fixture and length of
        fixture_list otherwise)
    """
    if fixture and fixture_list:
        msg = "Args provided for both `fixture` and `fixture_list`"
        pytest.fail(msg)
    if not fixture and not fixture_list:
        msg = "Must pass 1 of {fixture, fixture_list}"
        pytest.fail(msg)
    if fixture and num_records:
        msg = "`num_records` should only be given with " "`fixture_list`."
        pytest.fail(msg)

    assert response.match_type == match_type
    if fixture:
        assert len(response.records) == 1
        _compare_records(response.records[0], fixture)
    elif fixture_list:
        if not num_records:
            assert len(response.records) == len(fixture_list)
        else:
            assert len(response.records) == num_records
        for fixt in fixture_list:
            for record in response.records:
                if fixt.concept_id == record.concept_id:
                    _compare_records(record, fixt)
                    break
            else:
                pytest.fail("Fixture not found in response")


@pytest.fixture(scope="session")
def compare_response():
    """Provide response comparison function"""
    return _compare_response

"""Main application for FastAPI"""

import html
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from fastapi.openapi.utils import get_openapi

from therapy import __version__
from therapy.database.database import create_db
from therapy.query import InvalidParameterError, QueryHandler
from therapy.schemas import (
    NormalizationService,
    SearchService,
    UnmergedNormalizationService,
)

query_handler = QueryHandler(create_db())
app = FastAPI(
    docs_url="/therapy",
    openapi_url="/therapy/openapi.json",
    swagger_ui_parameters={"tryItOutEnabled": True},
)


def custom_openapi() -> dict:
    """Generate custom fields for OpenAPI response"""
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="The VICC Therapy Normalizer",
        version=__version__,
        openapi_version="3.0.3",
        description="Normalize drugs and other therapy terms.",
        routes=app.routes,
    )
    #    openapi_schema["info"]["license"] = {  # TODO
    #        "name": "Name-of-license",
    #        "url": "http://www.to-be-determined.com"
    #    }
    openapi_schema["info"]["contact"] = {
        "name": "Alex H. Wagner",
        "email": "Alex.Wagner@nationwidechildrens.org",
        "url": "https://www.nationwidechildrens.org/specialties/institute-for-genomic-medicine/research-labs/wagner-lab",
    }
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]

# endpoint description text
get_matches_summary = "Given query, provide highest matches from each source."
response_descr = "A response to a validly-formed query."
q_descr = "Therapy to search."
incl_descr = (
    "Comma-separated list of source names to include in response. Will "
    "exclude all other sources. Will return HTTP status code 422: "
    'Unprocessable Entity if both "incl" and "excl" parameters are given.'
)
excl_descr = (
    "Comma-separated list of source names to exclude in response. Will "
    "include all other sources. Will return HTTP status code 422: "
    'Unprocessable Entity if both "incl" and "excl" parameters are given.'
)
infer_descr = (
    "If true, attempt namespace inference when queries match known "
    "Local Unique Identifier patterns."
)
search_description = (
    "For each source, return strongest-match concepts for query "
    "string provided by user"
)
normalize_description = (
    "Return merged strongest-match concept for query string " "provided by user."
)
merged_matches_summary = (
    "Given query, provide merged normalized record as a Therapy Mappable Concept."
)
merged_response_descr = "A response to a validly-formed query."
normalize_q_descr = "Therapy to normalize."
unmerged_matches_summary = (
    "Given query, provide source records corresponding to " "normalized concept."
)
unmerged_response_descr = (
    "Response containing source records contained within " "normalized concept."
)
unmerged_normalize_description = (
    "Return unmerged records associated with the "
    "normalized result of the user-provided query "
    "string."
)


@app.get(
    "/therapy/search",
    summary=get_matches_summary,
    operation_id="getQueryResponse",
    response_description=response_descr,
    response_model_exclude_none=True,
    description=search_description,
)
def search(
    q: Annotated[str, Query(..., description=q_descr)],
    incl: Annotated[str | None, Query(None, description=incl_descr)],
    excl: Annotated[str | None, Query(None, description=excl_descr)],
    infer_namespace: Annotated[bool, Query(True, description=infer_descr)],
) -> SearchService:
    """For each source, return strongest-match concepts for query string
    provided by user.

    :param q: therapy search term
    :param str incl: comma-separated list of sources to include, with all others
        excluded. Raises HTTPException if both `incl` and `excl` are given.
    :param str excl: comma-separated list of sources exclude, with all others included.
        Raises HTTPException if both `incl` and `excl` are given.
    :param bool infer_namespace: if True, try to infer namespace from query term.
    :returns: JSON response with matched records and source metadata
    """
    try:
        response = query_handler.search(
            html.unescape(q),
            incl=incl,  # type: ignore[arg-type]
            excl=excl,  # type: ignore[arg-type]
            infer=infer_namespace,
        )
    except InvalidParameterError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return response


@app.get(
    "/therapy/normalize",
    summary=merged_matches_summary,
    operation_id="getMergedRecord",
    response_description=merged_response_descr,
    response_model_exclude_none=True,
    description=normalize_description,
)
def normalize(
    q: Annotated[str, Query(..., description=normalize_q_descr)],
    infer_namespace: Annotated[bool, Query(True, description=infer_descr)],
) -> NormalizationService:
    """Return merged strongest-match concept for query string provided by
    user.

    :param q: therapy search term
    :param bool infer_namespace: if True, try to infer namespace from query term.
    :returns: JSON response with matching normalized record provided as a
    Therapy Mappable Concept, and source metadata
    """
    try:
        response = query_handler.normalize(html.unescape(q), infer_namespace)
    except InvalidParameterError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return response


@app.get(
    "/therapy/normalize_unmerged",
    summary=unmerged_matches_summary,
    operation_id="getUnmergedRecords",
    response_description=unmerged_response_descr,
    description=unmerged_normalize_description,
)
def normalize_unmerged(
    q: Annotated[str, Query(True, description=normalize_q_descr)],
    infer_namespace: Annotated[bool, Query(True, description=infer_descr)],
) -> UnmergedNormalizationService:
    """Return all individual records associated with a normalized concept.

    :param q: therapy search term
    :param bool infer_namespace: if True, try to infer namespace from query term.
    :returns: JSON response with matching normalized record and source metadata
    """
    try:
        response = query_handler.normalize_unmerged(html.unescape(q), infer_namespace)
    except InvalidParameterError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return response

"""Add xrefs attribute to therapy concepts in the therapy_concepts
DynamoDB Table.
"""
import sys
from pathlib import Path
import click
from os import environ
from timeit import default_timer as timer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(f"{PROJECT_ROOT}")

from therapy.database import Database  # noqa: E402
from therapy.schemas import NamespacePrefix, SourceName  # noqa: E402


def update_xrefs_other_ids(db):
    """Add xrefs attribute to therapy identity concepts.

    :param Database db: DynamoDB object
    """
    last_evaluated_key = None
    normalizer_srcs = {
        NamespacePrefix[src].value for src in SourceName.__members__}

    while True:
        if last_evaluated_key:
            response = db.ddb_client.scan(
                TableName=db.therapies.name,
                ExclusiveStartKey=last_evaluated_key,
                FilterExpression='src_name <> :src_name',
                ExpressionAttributeValues={
                    ':src_name': {'S': 'ChEMBL'}
                }
            )
        else:
            response = db.ddb_client.scan(
                TableName=db.therapies.name,
                FilterExpression='src_name <> :src_name',
                ExpressionAttributeValues={
                    ':src_name': {'S': 'ChEMBL'}
                }
            )
        last_evaluated_key = response.get('LastEvaluatedKey')

        records = response['Items']
        for record in records:
            record_identity = record['label_and_type']['S']
            record_concept_id = record['concept_id']['S']
            if '##identity' in record_identity:
                other_ids = []
                xrefs = []
                for attr in ['other_identifiers', 'xrefs']:
                    _add_xrefs_other_ids(record, attr, other_ids, xrefs,
                                         normalizer_srcs)
                update_item(db, record_identity, record_concept_id,
                            other_ids, xrefs)

        if not last_evaluated_key:
            break


def _add_xrefs_other_ids(record, attr, other_ids, xrefs, normalizer_srcs):
    if attr in record:
        if 'NULL' not in record[attr]:
            prev_ids = record[attr]['L']
            for prev_id in prev_ids:
                other_id_xref = prev_id['S']
                if other_id_xref.split(':')[0] in normalizer_srcs:
                    other_ids.append(other_id_xref)
                else:
                    xrefs.append(other_id_xref)


def update_item(db, record_identity, record_concept_id, other_ids, xrefs):
    """Update therapy identity concept with associated other_identifiers
    and xrefs attributes.

    :param Database db: DynamoDB object
    :param str record_identity: The therapy identity concept label_and_type
                                value
    :param str record_concept_id: The therapy identity concept id
    :param list other_ids: The other identifiers for the therapy concept
    :param list xrefs: The xrefs for the therapy concept
    """
    db.therapies.update_item(
        Key={
            'label_and_type': record_identity,
            'concept_id': record_concept_id
        },
        UpdateExpression="set other_identifiers=:o, xrefs=:x",
        ExpressionAttributeValues={
            ':o': other_ids,
            ':x': xrefs,
        },
        ReturnValues="UPDATED_NEW"
    )

    if not xrefs:
        db.therapies.update_item(
            Key={
                'label_and_type': record_identity,
                'concept_id': record_concept_id
            },
            UpdateExpression="remove xrefs",
            ReturnValues="UPDATED_NEW"
        )

    if not other_ids:
        db.therapies.update_item(
            Key={
                'label_and_type': record_identity,
                'concept_id': record_concept_id
            },
            UpdateExpression="remove other_identifiers",
            ReturnValues="UPDATED_NEW"
        )


if __name__ == '__main__':
    if 'THERAPY_NORM_DB_URL' not in environ.keys():
        if click.confirm("Are you sure you want to update"
                         " the production database?", default=False):
            click.echo("Updating production db...")
        else:
            click.echo("Exiting.")
            sys.exit()
    click.echo("Adding xrefs attribute...")
    start = timer()
    update_xrefs_other_ids(Database())
    end = timer()
    click.echo(f"Added xrefs attribute in {end-start} seconds.")

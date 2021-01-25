"""This module defines the Wikidata ETL methods."""
from .base import Base, IDENTIFIER_PREFIXES
from therapy import PROJECT_ROOT
import json
from therapy.schemas import SourceName, NamespacePrefix, \
    SourceIDAfterNamespace, Meta
from therapy.database import Database
import logging
from typing import Dict

logger = logging.getLogger('therapy')
logger.setLevel(logging.DEBUG)


class Wikidata(Base):
    """Extract, transform, and load the Wikidata source into therapy.db.
    SPARQL_QUERY:
    SELECT ?item ?itemLabel ?casRegistry ?pubchemCompound
           ?pubchemSubstance ?chembl
           ?rxnorm ?drugbank ?alias WHERE {
      ?item (wdt:P31/(wdt:P279*)) wd:Q12140.
      OPTIONAL {
        ?item skos:altLabel ?alias.
        FILTER((LANG(?alias)) = "en")
      }
      OPTIONAL { ?item p:P231 ?wds1.
                 ?wds1 ps:P231 ?casRegistry.
               }
      OPTIONAL { ?item p:P662 ?wds2.
                 ?wds2 ps:P662 ?pubchemCompound.
               }
      OPTIONAL { ?item p:P2153 ?wds3.
                 ?wds3 ps:P2153 ?pubchemSubstance.
               }
      OPTIONAL { ?item p:P592 ?wds4.
                 ?wds4 ps:P592 ?chembl
               }
      OPTIONAL { ?item p:P3345 ?wds5.
                 ?wds5 ps:P3345 ?rxnorm.
               }
      OPTIONAL { ?item p:P715 ?wds6.
                 ?wds6 ps:P715 ?drugbank
               }
      SERVICE wikibase:label {
        bd:serviceParam wikibase:language "[AUTO_LANGUAGE],en".
      }
    }
    """

    def __init__(self, database: Database, *args, **kwargs):
        """Initialize wikidata ETL class"""
        self.database = database
        self._extract_data(*args, **kwargs)
        self._add_meta()
        self._transform_data()

    def _extract_data(self, *args, **kwargs):
        """Extract data from the Wikidata source."""
        if 'data_path' in kwargs:
            self._data_src = kwargs['data_path']
        else:
            wd_dir = PROJECT_ROOT / 'data' / 'wikidata'
            wd_dir.mkdir(exist_ok=True, parents=True)  # TODO needed?
            try:
                self._data_src = sorted(list(wd_dir.iterdir()))[-1]
            except IndexError:
                raise FileNotFoundError  # TODO wikidata update function here
        self._version = self._data_src.stem.split('_')[1]

    def _add_meta(self):
        """Add Wikidata metadata."""
        metadata = Meta(src_name=SourceName.WIKIDATA.value,
                        data_license='CC0 1.0',
                        data_license_url='https://creativecommons.org/publicdomain/zero/1.0/',  # noqa: E501
                        version=self._version,
                        data_url=None,
                        rdp_url=None,
                        data_license_attributes={
                            'non_commercial': False,
                            'share_alike': False,
                            'attribution': False
                        })
        params = dict(metadata)
        params['src_name'] = SourceName.WIKIDATA.value
        self.database.metadata.put_item(Item=params)

    def _transform_data(self):
        """Transform the Wikidata source data.
        Currently, gather all items in memory and then batch-load into
        DynamoDB. Worth considering whether adding record directly and then
        issuing update statements to append additional aliases would be better.
        """
        with open(self._data_src, 'r') as f:
            records = json.load(f)

            items = dict()
            normalizer_srcs = {src for src in SourceName.__members__}

            for record in records:
                record_id = record['item'].split('/')[-1]
                concept_id = f"{NamespacePrefix.WIKIDATA.value}:{record_id}"
                if concept_id not in items.keys():
                    item = dict()
                    item['label_and_type'] = f"{concept_id.lower()}##identity"
                    item['concept_id'] = concept_id
                    item['src_name'] = SourceName.WIKIDATA.value

                    other_ids = []
                    xrefs = []
                    for key in IDENTIFIER_PREFIXES.keys():
                        if key in record.keys():
                            other_id = record[key]

                            if key.upper() == 'CASREGISTRY':
                                key = SourceName.CHEMIDPLUS.value

                            if key.upper() in normalizer_srcs:
                                if key != 'chembl':
                                    fmted_other_id = \
                                        f"{IDENTIFIER_PREFIXES[key]}:" \
                                        f"{SourceIDAfterNamespace[key.upper()].value}{other_id}"  # noqa: E501
                                else:
                                    fmted_other_id = \
                                        f"{IDENTIFIER_PREFIXES[key]}:" \
                                        f"{other_id}"
                                other_ids.append(fmted_other_id)
                            else:
                                fmted_xref = f"{IDENTIFIER_PREFIXES[key]}:" \
                                             f"{other_id}"
                                xrefs.append(fmted_xref)
                    item['other_identifiers'] = other_ids
                    item['xrefs'] = xrefs
                    if 'itemLabel' in record.keys():
                        item['label'] = record['itemLabel']
                    items[concept_id] = item
                if 'alias' in record.keys():
                    if 'aliases' in items[concept_id].keys():
                        items[concept_id]['aliases'].append(record['alias'])
                    else:
                        items[concept_id]['aliases'] = [record['alias']]

        with self.database.therapies.batch_writer() as batch:
            for item in items.values():
                self._load_therapy(item, batch)

    def _load_therapy(self, item: Dict, batch):
        """Load individual therapy record into DynamoDB
        Args:
            item: dict containing, at minimum, label_and_type and concept_id
                keys.
            batch: boto3 batch writer
        """
        if 'aliases' in item:
            item['aliases'] = list(set(item['aliases']))

            if len({a.casefold() for a in item['aliases']}) > 20:  # noqa: E501
                del item['aliases']

        batch.put_item(Item=item)
        concept_id_lower = item['concept_id'].lower()

        if 'aliases' in item.keys():
            aliases = {alias.lower() for alias in item['aliases']}
            for alias in aliases:
                pk = f"{alias}##alias"
                batch.put_item(Item={
                    'label_and_type': pk,
                    'concept_id': concept_id_lower,
                    'src_name': SourceName.WIKIDATA.value
                })

        if 'label' in item.keys():
            pk = f"{item['label'].lower()}##label"
            batch.put_item(Item={
                'label_and_type': pk,
                'concept_id': concept_id_lower,
                'src_name': SourceName.WIKIDATA.value
            })

    def _sqlite_str(self, string):
        """Sanitizes string to use as value in SQL statement.
        Some wikidata entries include items with single quotes,
        like wikidata:Q80863 alias: 5'-(Tetrahydrogen triphosphate) Adenosine
        """
        if string == "NULL":
            return "NULL"
        else:
            sanitized = string.replace("'", "''")
            return f"{sanitized}"

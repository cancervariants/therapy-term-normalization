"""This module defines the DrugBank ETL methods."""
from therapy.etl.base import Base
from therapy import PROJECT_ROOT
import logging
from therapy import schemas  # noqa: F401
from therapy.schemas import SourceName, NamespacePrefix, ApprovalStatus
from therapy.etl.base import IDENTIFIER_PREFIXES
from lxml import etree

logger = logging.getLogger('therapy')
logger.setLevel(logging.DEBUG)

DRUGBANK_IDENTIFIER_PREFIXES = {
    'ChEBI': NamespacePrefix.CHEBI.value,
    'ChEMBL': NamespacePrefix.CHEMBL.value,
    'PubChem Compound': NamespacePrefix.PUBCHEMCOMPOUND.value,
    'PubChem Substance': NamespacePrefix.PUBCHEMSUBSTANCE.value,
    'KEGG Compound': NamespacePrefix.KEGGCOMPOUND.value,
    'KEGG Drug': NamespacePrefix.KEGGDRUG.value,
    'ChemSpider': NamespacePrefix.CHEMSPIDER.value,
    'BindingDB': NamespacePrefix.BINDINGDB.value,
    'PharmGKB': NamespacePrefix.PHARMGKB.value,
    'ZINC': NamespacePrefix.ZINC.value,
    'RxCUI': NamespacePrefix.RXNORM.value,
    'PDB': NamespacePrefix.PDB.value,
    'Therapeutic Targets Database': NamespacePrefix.THERAPEUTICTARGETSDB.value,
    'IUPHAR': NamespacePrefix.IUPHAR.value,
    'Guide to Pharmacology': NamespacePrefix.GUIDETOPHARMACOLOGY.value
}


class DrugBank(Base):
    """ETL the DrugBank source into therapy.db."""

    def _extract_data(self, *args, **kwargs):
        """Extract data from the DrugBank source."""
        if 'data_path' in kwargs:
            self._data_src = kwargs['data_path']
        else:
            wd_dir = PROJECT_ROOT / 'data' / 'drugbank'
            try:
                self._data_src = sorted(list(wd_dir.iterdir()))[-1]
            except IndexError:
                raise FileNotFoundError  # TODO drugbank update function here

    def _transform_data(self):
        """Transform the DrugBank source."""
        xmlns = "{http://www.drugbank.ca}"
        tree = etree.parse(f"{self._data_src}")
        root = tree.getroot()
        batch = self.database.therapies.batch_writer()

        for drug in root:
            params = {
                'label_and_type': None,
                'concept_id': None,
                'label': None,
                'approval_status': None,
                'aliases': [],
                'other_identifiers': [],
                'trade_names': [],
                'src_name': SourceName.DRUGBANK.value
            }
            for element in drug:
                # Concept ID  / Aliases
                if element.tag == f"{xmlns}drugbank-id":
                    self._load_drugbank_id(element, params)

                # Label
                if element.tag == f"{xmlns}name":
                    params['label'] = element.text

                # Aliases
                if element.tag == f"{xmlns}synonyms":
                    self._load_synonyms(element, params)
                if element.tag == f"{xmlns}international-brands":
                    self._load_international_brands(element, params, xmlns)

                # Trade Names
                if element.tag == f"{xmlns}products":
                    self._load_products(element, params, xmlns)

                # Other Identifiers
                if element.tag == f"{xmlns}external-identifiers":
                    self._load_external_identifiers(element, params, xmlns)
                if element.tag == f"{xmlns}cas-number":
                    self._load_cas_number(element, params)

                # Approval status
                if element.tag == f"{xmlns}groups":
                    self._load_approval_status(element, params)

            self._load_therapy(batch, params)

            if params['label']:
                self._load_label(params['label'], params['concept_id'],
                                 batch)

            if 'aliases' in params:
                if params['aliases']:
                    self._load_aliases(params['aliases'], params['concept_id'],
                                       batch)

            if 'trade_names' in params:
                if params['trade_names']:
                    self._load_trade_names(params['trade_names'],
                                           params['concept_id'], batch)

    def _load_data(self, *args, **kwargs):
        """Load the DrugBank source into normalized database."""
        self._extract_data()
        self._transform_data()
        self._add_meta()

    def _load_therapy(self, batch, params):
        """Filter out trade names and aliases that exceed 20 and add item to
        therapies table.
        """
        if not params['other_identifiers']:
            del params['other_identifiers']

        for label_type in ['trade_names', 'aliases']:
            if label_type in params:
                if not params[label_type] or len(
                        {a.casefold() for a in params[label_type]}) > 20:
                    del params[label_type]
        batch.put_item(Item=params)

    def _load_drugbank_id(self, element, params):
        """Load drugbank id as concept id or alias."""
        # Concept ID
        if 'primary' in element.attrib:
            params['concept_id'] = \
                f"{NamespacePrefix.DRUGBANK.value}:{element.text}"
            params['label_and_type'] = \
                f"{params['concept_id'].lower()}##identity"
        else:
            # Aliases
            params['aliases'].append(element.text)

    def _load_synonyms(self, element, params):
        """Load synonyms as aliases."""
        for alias in element:
            if alias.text not in params['aliases'] and \
                    alias.attrib['language'] == 'english':
                params['aliases'].append(alias.text)

    def _load_international_brands(self, element, params, xmlns):
        """Load international brands as aliases."""
        for international_brand in element:
            name = international_brand.find(f"{xmlns}name").text
            if name not in params['aliases']:
                params['aliases'].append(name)

    def _load_approval_status(self, element, params):
        """Load approval status."""
        group_type = []
        for group in element:
            group_type.append(group.text)
        if "withdrawn" in group_type:
            params['approval_status'] = \
                ApprovalStatus.WITHDRAWN.value
        elif "approved" in group_type:
            params['approval_status'] = \
                ApprovalStatus.APPROVED.value
        elif "investigational" in group_type:
            params['approval_status'] = \
                ApprovalStatus.INVESTIGATIONAL.value

    def _load_cas_number(self, element, params):
        """Load cas number as other identifiers."""
        if element.text:
            params['other_identifiers'].append(
                f"{IDENTIFIER_PREFIXES['casRegistry']}:"
                f"{element.text}")

    def _load_external_identifiers(self, element, params, xmlns):
        """Load external identifiers as other identifiers."""
        for external_identifier in element:
            src = external_identifier.find(f"{xmlns}resource").text
            identifier = external_identifier.find(
                f"{xmlns}identifier").text
            if src in DRUGBANK_IDENTIFIER_PREFIXES.keys():
                params['other_identifiers'].append(
                    f"{DRUGBANK_IDENTIFIER_PREFIXES[src]}:"
                    f"{identifier}")

    def _load_products(self, element, params, xmlns):
        """Load products as trade names."""
        for product in element:
            name = product.find(f"{xmlns}name").text
            generic = product.find(f"{xmlns}generic").text
            approved = product.find(f"{xmlns}approved").text
            over_the_counter = \
                product.find(f"{xmlns}over-the-counter").text

            if generic == "true" or approved == "true" or \
                    over_the_counter == "true":
                if name not in params['trade_names']:
                    params['trade_names'].append(name)

    def _load_label(self, label, concept_id, batch):
        """Insert label data into the database."""
        label = {
            'label_and_type':
                f"{label.lower()}##label",
            'concept_id': f"{concept_id.lower()}",
            'src_name': SourceName.DRUGBANK.value
        }
        batch.put_item(Item=label)

    def _load_aliases(self, aliases, concept_id, batch):
        """Insert alias data into the database."""
        aliases = list(set({a.casefold(): a for a in aliases}.values()))
        for alias in aliases:
            alias = {
                'label_and_type': f"{alias.lower()}##alias",
                'concept_id': f"{concept_id.lower()}",
                'src_name': SourceName.DRUGBANK.value
            }
            batch.put_item(Item=alias)

    def _load_trade_names(self, trade_names, concept_id, batch):
        """Insert trade_name data into the database."""
        trade_names = \
            list(set({t.casefold(): t for t in trade_names}.values()))
        for trade_name in trade_names:
            trade_name = {
                'label_and_type': f"{trade_name.lower()}##trade_name",
                'concept_id': f"{concept_id.lower()}",
                'src_name': SourceName.DRUGBANK.value
            }
            batch.put_item(Item=trade_name)

    def _add_meta(self):
        """Add DrugBank metadata."""
        self.database.metadata.put_item(
            Item={
                'src_name': SourceName.DRUGBANK.value,
                'data_license': 'CC BY-NC 4.0',
                'data_license_url':
                    'https://creativecommons.org/licenses/by-nc/4.0/legalcode',
                'version': '5.1.7',
                'data_url':
                    'https://go.drugbank.com/releases/5-1-7/downloads/all-full-database'  # noqa E501
            }
        )

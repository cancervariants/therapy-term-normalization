"""This module defines the RxNorm ETL methods.

"This product uses publicly available data courtesy of the U.S. National
Library of Medicine (NLM), National Institutes of Health, Department of Health
 and Human Services; NLM is not responsible for the product and does not
 endorse or recommend this or any other product."
"""
import csv
import logging
import shutil
import zipfile
import re
from os import environ, remove
from typing import List, Dict, Any
from pathlib import Path

import yaml
import bioversions
from boto3.dynamodb.table import BatchWriter
import requests

from therapy import DownloadException, XREF_SOURCES, ASSOC_WITH_SOURCES, ITEM_TYPES
from therapy.schemas import SourceName, NamespacePrefix, SourceMeta, ApprovalRating
from therapy.etl.base import Base

logger = logging.getLogger("therapy")
logger.setLevel(logging.DEBUG)

# Designated Alias, Designated Syn, Tall Man Syn, Machine permutation
# Generic Drug Name, Designated Preferred Name, Preferred Entry Term,
# Clinical Drug, Entry Term, Rxnorm Preferred
ALIASES = ["SYN", "SY", "TMSY", "PM", "GN", "PT", "PEP", "CD", "ET", "RXN_PT"]

# Fully-specified drug brand name that can be prescribed
# Fully-specified drug brand name that can not be prescribed,
# Semantic branded drug
TRADE_NAMES = ["BD", "BN", "SBD"]

# Allowed rxnorm xrefs that have Source Level Restriction 0 or 1
RXNORM_XREFS = ["ATC", "CVX", "DRUGBANK", "MMSL", "MSH", "MTHCMSFRF", "MTHSPL",
                "RXNORM", "USP", "VANDF"]

THERAPY_FIELDS = set(ITEM_TYPES.keys()) | {"concept_id", "approval_ratings"}


class RxNorm(Base):
    """Class for RxNorm ETL methods."""

    def _create_drug_form_yaml(self) -> None:
        """Create a YAML file containing RxNorm drug form values."""
        self._drug_forms_file = self._src_dir / f"rxnorm_drug_forms_{self._version}.yaml"  # noqa: E501
        dfs = []
        with open(self._src_file) as f:  # type: ignore
            data = csv.reader(f, delimiter="|")
            for row in data:
                if row[12] == "DF" and row[11] == "RXNORM":
                    if row[14] not in dfs:
                        dfs.append(row[14])
        with open(self._drug_forms_file, "w") as file:
            yaml.dump(dfs, file)

    def _zip_handler(self, dl_path: Path, outfile_path: Path) -> None:
        """Extract required files from RxNorm zip. This method should be passed to
        the base class's _http_download method.
        :param Path dl_path: path to RxNorm zip file in tmp directory
        :param Path outfile_path: path to RxNorm data directory
        """
        rrf_path = outfile_path / f"rxnorm_{self._version}.RRF"
        with zipfile.ZipFile(dl_path, "r") as zf:
            rrf = zf.open("rrf/RXNCONSO.RRF")
            target = open(rrf_path, "wb")
            with rrf, target:
                shutil.copyfileobj(rrf, target)
        remove(dl_path)
        self._src_file = rrf_path
        self._create_drug_form_yaml()
        logger.info("Successfully retrieved source data for RxNorm")

    def _download_data(self) -> None:
        """Download latest RxNorm data file.

        :raises DownloadException: if API Key is not defined in the environment.
        """
        logger.info("Retrieving source data for RxNorm")
        api_key = environ.get("RXNORM_API_KEY")
        if api_key is None:
            logger.error("Could not find RXNORM_API_KEY in environment variables.")
            raise DownloadException("RXNORM_API_KEY not found.")

        url = bioversions.resolve("rxnorm").homepage
        if not url:
            raise DownloadException("Could not resolve RxNorm homepage")

        tgt_data = {"apikey": api_key}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        api_url = "https://utslogin.nlm.nih.gov/cas/v1/api-key"
        tgt_r = requests.post(api_url,
                              data=tgt_data, headers=headers)
        tgt_matches = re.findall(r'https://.+(TGT.+)" m', tgt_r.text)
        if not tgt_matches:
            raise DownloadException("Unable to retrieve TGT")
        tgt_value = tgt_matches[0]

        st_data = {"service": url}
        st_url = f"https://utslogin.nlm.nih.gov/cas/v1/tickets/{tgt_value}"
        st_r = requests.post(st_url, data=st_data, headers=headers)

        self._http_download(f"{url}?ticket={st_r.text}", self._src_dir,
                            handler=self._zip_handler)

    def _get_existing_files(self) -> List[Path]:
        """Get existing source RRF files from data directory.
        :return: sorted list of file objects
        """
        return list(sorted(self._src_dir.glob("rxnorm_*.RRF")))

    def _extract_data(self, use_existing: bool = False) -> None:
        """Get source files from RxNorm data directory.
        This class expects a file named `rxnorm_<version>.RRF` and a file named
        `rxnorm_drug_forms_<version>.yaml`. This method will download and
        generate them if they are unavailable.

        :param bool use_existing: if True, don't try to fetch latest source data
        """
        super()._extract_data(use_existing)
        drug_forms_path = self._src_dir / f"rxnorm_drug_forms_{self._version}.yaml"
        if not drug_forms_path.exists():
            self._create_drug_form_yaml()
        else:
            self._drug_forms_file = drug_forms_path

    def _transform_data(self) -> None:
        """Transform the RxNorm source."""
        with open(self._drug_forms_file, "r") as file:
            drug_forms = yaml.safe_load(file)

        # Transformed therapy records
        records: Dict[str, Dict] = dict()
        # Link ingredient (IN) to brand name (BN)
        ingredient_to_brands: Dict[str, str] = dict()
        # Link precise ingredient (PIN) to brand name
        precise_ingredient_to_brand: Dict[str, str] = dict()
        # TODO is this named correctly?
        # Link ingredient to Semantic Banded Drug Form
        ingredient_to_sbdf: Dict[str, str] = dict()
        # Get RXNORM|BN to concept_id
        brand_to_concept_id: Dict[str, str] = dict()

        with open(self._src_file) as f:
            rff_data = csv.reader(f, delimiter="|")
            for row in rff_data:
                if row[11] in RXNORM_XREFS:
                    concept_id = f"{NamespacePrefix.RXNORM.value}:{row[0]}"
                    if row[12] == "BN" and row[11] == "RXNORM":
                        brand_to_concept_id[row[14]] = concept_id
                    if row[12] == "SBDC" and row[11] == "RXNORM":
                        # Semantic Branded Drug Component
                        self._get_brands(row, ingredient_to_brands)
                    else:
                        if concept_id not in records.keys():
                            record: Dict[str, Any] = {"concept_id": concept_id}
                            # TODO i think these can be removed
                            # self._add_str_field(record, row, precise_ingredient,
                            #                     drug_forms, sbdfs)
                            # self._add_xref_assoc(record, row)
                            records[concept_id] = record
                        else:
                            # Concept already created
                            record = records[concept_id]
                        self._add_str_field(record, row, precise_ingredient_to_brand,
                                            drug_forms, ingredient_to_sbdf)
                        self._add_xref_assoc(record, row)

            with self.database.therapies.batch_writer() as batch:
                for record in records.values():
                    if "label" in record:
                        self._get_trade_names(record, precise_ingredient_to_brand,
                                              ingredient_to_brands, ingredient_to_sbdf)
                        self._load_brand_concepts(record, brand_to_concept_id, batch)

                        # record_final = {"concept_id": record["concept_id"]}
                        #
                        # for field in THERAPY_FIELDS:
                        #     record_final[field] = record.get(field)
                        if "PIN" in record:  # TODO any others?
                            del record["PIN"]

                        if record["concept_id"] == "rxcui:100213":
                            breakpoint()  # TODO

                        self._load_therapy(record)

    def _get_brands(self, row: List, ingredient_to_brands: Dict) -> None:
        """Add ingredient and brand to ingredient_brands.

        :param List row: A row in the RxNorm data file.
        :param Dict ingredient_to_brand: Store brands for each ingredient
        """
        # SBDC: Ingredient(s) + Strength + [Brand Name]
        term = row[14]
        ingredients_brand = re.sub(
            r"(\d*)(\d*\.)?\d+ (MG|UNT|ML)?(/(ML|HR|MG))?", "", term
        )
        brand = term.split("[")[-1].split("]")[0]
        ingredients = ingredients_brand.replace(f"[{brand}]", "")
        if "/" in ingredients:
            ingredients = ingredients.split("/")
            for ingredient in ingredients:
                self._add_term_to_field(ingredient_to_brands, brand, ingredient.strip())
        else:
            self._add_term_to_field(ingredient_to_brands, brand, ingredients.strip())

    def _get_trade_names(self, record: Dict, precise_ingredient: Dict,
                         ingredient_brands: Dict, sbdfs: Dict) -> None:
        """Get trade names for a given ingredient.

        :param Dict record: Therapy attributes
        :param Dict precise_ingredient: Brand names for precise ingredient
        :param Dict ingredient_brands: Brand names for ingredient
        :param Dict sbdfs: Brand names for ingredient from SBDF row
        """
        record_label = record["label"].lower()
        labels = [record_label]

        if "PIN" in record and record["PIN"] in precise_ingredient:
            for pin in precise_ingredient[record["PIN"]]:
                labels.append(pin.lower())

        for label in labels:
            trade_names: List[str] = [
                val for key, val in ingredient_brands.items() if label == key.lower()
            ]
            trade_names_uq = {val for sublist in trade_names for val in sublist}
            for tn in trade_names_uq:
                self._add_term_to_field(record, "trade_names", tn)

        if record_label in sbdfs:
            for tn in sbdfs[record_label]:
                self._add_term_to_field(record, "trade_names", tn)

    @staticmethod
    def _load_brand_concepts(record: Dict, brands: Dict, batch: BatchWriter) -> None:
        """Connect brand names to a concept and load into the database.

        :params dict record: A transformed therapy record
        :params dict brands: Connects brand names to concept records
        :param BatchWriter batch: Object to write data to DynamoDB.
        """
        if "trade_names" in record:
            for tn in record["trade_names"]:
                if brands.get(tn):
                    batch.put_item(Item={
                        "label_and_type": f"{brands.get(tn)}##rx_brand",
                        "concept_id": record["concept_id"],
                        "src_name": SourceName.RXNORM.value,
                        "item_type": "rx_brand"
                    })

    def _add_str_field(self, params: Dict, row: List, precise_ingredient: Dict,
                       drug_forms: List, sbdfs: Dict) -> None:
        """Differentiate STR field.

        :param Dict params: A transformed therapy record.
        :param List row: A row in the RxNorm data file.
        :param Dict precise_ingredient: Precise ingredient information
        :param List drug_forms: RxNorm Drug Form values
        :param Dict sbdfs: Brand names for precise ingredient
        """
        term = row[14]
        term_type = row[12]
        source = row[11]

        if (term_type == "IN" or term_type == "PIN") and source == "RXNORM":
            params["label"] = term
            if row[17] == "4096":
                params["approval_ratings"] = [ApprovalRating.RXNORM_PRESCRIBABLE.value]
        elif term_type in ALIASES:
            self._add_term_to_field(params, "aliases", term)
        elif term_type in TRADE_NAMES:
            self._add_term_to_field(params, "trade_names", term)

        if source == "RXNORM":
            if term_type == "SBDF":
                brand = term.split("[")[-1].split("]")[0]
                ingredient_strength = term.replace(f"[{brand}]", "")
                for df in drug_forms:
                    if df in ingredient_strength:
                        ingredient = ingredient_strength.replace(df, "").strip()
                        self._add_term_to_field(sbdfs, ingredient.lower(), brand)
                        break
        elif source == "MSH":
            if term_type == "MH":
                # Get ID for accessing precise ingredient
                params["PIN"] = row[13]
            elif term_type == "PEP":
                self._add_term_to_field(precise_ingredient, row[13], term)

    @staticmethod
    def _add_term_to_field(data_dict: Dict, field: str, term: str) -> None:
        """Add a single value to a listlike field.

        :param Dict data_dict: either in-progress record or RxNorm ref lookup
        :param str field: Record property name
        :param str term: The term to be added
        """
        if field in data_dict and data_dict[field]:
            if term not in data_dict[field]:
                data_dict[field].append(term)
        else:
            data_dict[field] = [term]

    def _add_xref_assoc(self, params: Dict, row: List) -> None:
        """Add xref or associated_with to therapy.

        :param Dict params: A transformed therapy record.
        :param List row: A row in the RxNorm data file.
        """
        ref = row[11]
        lui = row[13]
        if ref and lui != "NOCODE":
            if ref == "MTHSPL":
                xref_assoc = "UNII"
            else:
                xref_assoc = row[11].upper()

            if xref_assoc in XREF_SOURCES:
                source_id = f"{NamespacePrefix[xref_assoc].value}:{lui}"
                if source_id != params["concept_id"]:
                    # Sometimes concept_id is included in the source field
                    self._add_term_to_field(params, "xrefs", source_id)
            elif xref_assoc in ASSOC_WITH_SOURCES:
                source_id = f"{NamespacePrefix[xref_assoc].value}:{lui}"
                self._add_term_to_field(params, "associated_with", source_id)
            else:
                logger.info(f"{xref_assoc} not in NameSpacePrefix.")

    def _load_meta(self) -> None:
        """Add RxNorm metadata."""
        meta = SourceMeta(
            data_license="UMLS Metathesaurus",
            data_license_url="https://www.nlm.nih.gov/research/umls/rxnorm/docs/termsofservice.html",  # noqa: E501
            version=self._version,
            data_url=bioversions.resolve("rxnorm").homepage,
            rdp_url=None,
            data_license_attributes={
                "non_commercial": False,
                "share_alike": False,
                "attribution": True
            }
        )
        params = dict(meta)
        params["src_name"] = SourceName.RXNORM.value
        self.database.metadata.put_item(Item=params)

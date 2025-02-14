"""Provide ETL methods for HemOnc.org data."""

import csv
import logging

from tqdm import tqdm
from wags_tails.hemonc import HemOncPaths

from therapy.etl.base import DiseaseIndicationBase
from therapy.schemas import (
    ApprovalRating,
    NamespacePrefix,
    RecordParams,
    SourceMeta,
)

_logger = logging.getLogger(__name__)


class HemOnc(DiseaseIndicationBase):
    """Class for HemOnc.org ETL methods."""

    def _extract_data(self, use_existing: bool) -> None:
        """Acquire source data.

        This method is responsible for initializing an instance of a data handler and
        setting ``self._data_files`` and ``self._version``.
        """
        data_files, self._version = self._data_source.get_latest(
            from_local=use_existing
        )
        self._data_files: HemOncPaths = data_files  # type: ignore

    def _load_meta(self) -> None:
        """Add HemOnc metadata."""
        meta = {
            "data_license": "CC BY 4.0",
            "data_license_url": "https://creativecommons.org/licenses/by/4.0/legalcode",
            "version": self._version,
            "data_url": "https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/9CY9C6",
            "rdp_url": None,
            "data_license_attributes": {
                "non_commercial": False,
                "share_alike": False,
                "attribution": True,
            },
        }
        self.database.add_source_metadata(self._name, SourceMeta(**meta))

    def _get_concepts(self) -> tuple[dict, dict, dict]:
        """Get therapy, brand name, and disease concepts from concepts file.
        :return: Tuple of dicts mapping ID to object for each type of concept
        """
        therapies: RecordParams = {}  # hemonc id -> record
        brand_names: dict[str, str] = {}  # hemonc id -> brand name
        conditions: dict[str, str] = {}  # hemonc id -> condition name

        with self._data_files.concepts.open() as concepts_file:
            concepts_reader = csv.reader(concepts_file)
            next(concepts_reader)  # skip header
            for row in concepts_reader:
                if row[6]:
                    continue  # skip if deprecated/invalid

                row_type = row[2]
                if row_type == "Component":
                    concept_id = f"{NamespacePrefix.HEMONC.value}:{row[3]}"
                    therapies[row[3]] = {
                        "concept_id": concept_id,
                        "label": row[0],
                        "trade_names": [],
                        "aliases": [],
                        "xrefs": [],
                    }
                elif row_type == "Brand Name":
                    brand_names[row[3]] = row[0]
                elif row_type == "Condition":
                    conditions[row[3]] = row[0]

        return therapies, brand_names, conditions

    @staticmethod
    def _id_to_yr(hemonc_id: str) -> str:
        """Get year from HemOnc ID corresponding to year concept.
        :param str hemonc_id: HemOnc ID to get year for
        :return: str representing year. Raises TypeError if HemOnc ID not valid.
        """
        id_int = int(hemonc_id)
        if id_int == 780:
            return "9999"
        if id_int == 48349:
            return "2020"
        if id_int == 5963:
            return "2021"
        if id_int < 699 or id_int > 780:
            msg = "ID not a valid HemOnc year concept"
            raise TypeError(msg)
        return str(id_int + 1240)

    def _get_rels(self, therapies: dict, brand_names: dict, conditions: dict) -> dict:
        """Gather relations to provide associations between therapies, brand names,
        and conditions.

        :param dict therapies: mapping from IDs to therapy concepts
        :param dict brand_names: mapping from IDs to therapy brand names
        :param dict conditions: mapping from IDs to disease conditions
        :return: therapies dict updated with brand names and conditions
        """
        with self._data_files.rels.open() as rels_file:
            rels_reader = csv.reader(rels_file)
            next(rels_reader)  # skip header

            for row in rels_reader:
                rel_type = row[4]
                hemonc_id = row[0]
                record = therapies.get(hemonc_id)

                if record is None:
                    continue  # skip non-drug items

                if rel_type == "Maps to":
                    if " and " in record.get("label", ""):
                        # xref from the HemOnc combo treatment to individual drugs
                        # see https://github.com/cancervariants/therapy-normalization/issues/417
                        continue
                    src_raw = row[3]
                    if src_raw == "RxNorm":
                        xref = f"{NamespacePrefix.RXNORM.value}:{row[1]}"
                        record["xrefs"].append(xref)
                    elif src_raw == "RxNorm Extension":
                        continue  # skip
                    else:
                        _logger.warning("Unrecognized `Maps To` source: %s", src_raw)

                elif rel_type == "Has brand name":
                    try:
                        record["trade_names"].append(brand_names[row[1]])
                    except KeyError:
                        _logger.warning(
                            "Unrecognized brand name ID (%s) for HemOnc concept %s",
                            row[1],
                            row[0],
                        )
                        continue

                elif rel_type == "Was FDA approved yr":
                    try:
                        year = self._id_to_yr(row[1])
                    except TypeError:
                        _logger.exception(
                            "Failed parse of FDA approval year ID %s for HemOnc ID %s",
                            row[1],
                            row[0],
                        )
                        continue
                    if year == "9999":
                        _logger.warning(
                            "HemOnc ID %s has FDA approval year 9999", row[0]
                        )
                    record["approval_ratings"] = [ApprovalRating.HEMONC_APPROVED.value]
                    if "approval_year" in record:
                        record["approval_year"].append(year)
                    else:
                        record["approval_year"] = [year]

                elif rel_type == "Has FDA indication":
                    try:
                        label = conditions[row[1]]
                    except KeyError:
                        # concept is deprecated or otherwise unavailable
                        _logger.exception(
                            "Unable to process relation with indication %s -- deprecated?",
                            row[0],
                        )
                        continue
                    norm_id = self._normalize_disease(label)
                    hemonc_concept_id = f"{NamespacePrefix.HEMONC.value}:{row[1]}"
                    indication = {
                        "disease_id": hemonc_concept_id,
                        "disease_label": label,
                        "normalized_disease_id": norm_id,
                        "supplemental_info": {"regulatory_body": "FDA"},
                    }
                    if "has_indication" in record:
                        record["has_indication"].append(indication)
                    else:
                        record["has_indication"] = [indication]

        return therapies

    def _get_synonyms(self, therapies: dict) -> dict:
        """Gather synonym entries and associate with therapy concepts.

        :param dict therapies: mapping of IDs to therapy objects
        :return: therapies dict with synonyms added as aliases
        """
        with self._data_files.synonyms.open() as synonyms_file:
            synonyms_reader = csv.reader(synonyms_file)
            next(synonyms_reader)
            for row in synonyms_reader:
                therapy_code = row[1]
                if therapy_code in therapies:
                    therapy = therapies[therapy_code]
                    alias = row[0]
                    if alias != therapy.get("label"):
                        therapies[therapy_code]["aliases"].append(row[0])
            return therapies

    def _transform_data(self) -> None:
        """Prepare dataset for loading into normalizer database."""
        therapies, brand_names, conditions = self._get_concepts()
        therapies = self._get_rels(therapies, brand_names, conditions)
        therapies = self._get_synonyms(therapies)

        for therapy in tqdm(therapies.values(), ncols=80, disable=self._silent):
            self._load_therapy(therapy)

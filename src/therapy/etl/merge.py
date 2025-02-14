"""Create concept groups and merged records."""

import logging
import re
from timeit import default_timer as timer
from typing import Any

from tqdm import tqdm

from therapy.database.database import AbstractDatabase, DatabaseWriteError
from therapy.schemas import RefType, SourceName, SourcePriority

logger = logging.getLogger(__name__)


class Merge:
    """Handles record merging."""

    def __init__(self, database: AbstractDatabase, silent: bool = True) -> None:
        """Initialize Merge instance.

        * self._groups is a dictionary keying concept IDs to the Set of concept IDs in
        that group. This is redundantly captured for all group members (i.e., a group
        of 5 concepts would all have their own key, and the value would be an identical
        Set of the same IDs)
        * self._unii_to_drugsatfda keys UNII codes to valid Drugs@FDA concepts.
        Because UNIIs aren't stored directly in groups, we utilize a separate lookup
        table to prevent repeat queries.
        * self._failed_lookups stores concept IDs for which lookups have been attempted
        and failed. Because we don't associate these IDs with groups, a separate
        mapping is necessary to prevent repeat queries.

        :param database: db instance to use for record retrieval and creation.
        :param silent: if True, suppress all console output
        """
        self.database = database
        self._groups: dict[str, set[str]] = {}
        self._unii_to_drugsatfda: dict[str, set[str]] = {}
        self._failed_lookups: set[str] = set()
        self._silent = silent

    def create_merged_concepts(self, record_ids: set[str]) -> None:
        """Create concept groups, generate merged concept records, and update database.

        :param Set[str] record_ids: concept identifiers from which groups should be
            generated.
        """
        logger.info("Generating record ID sets...")
        start = timer()
        for record_id in tqdm(record_ids, ncols=80, disable=self._silent):
            new_group = self._create_record_id_set(record_id)
            if new_group:
                for concept_id in new_group:
                    self._groups[concept_id] = new_group
        end = timer()
        logger.debug("Built record ID sets in %s seconds", end - start)

        self._groups = {k: v for k, v in self._groups.items() if len(v) > 1}

        logger.info("Creating merged records and updating database...")
        uploaded_ids = set()
        start = timer()
        for record_id, group in tqdm(
            self._groups.items(), ncols=80, disable=self._silent
        ):
            if record_id in uploaded_ids:
                continue
            merged_record = self._generate_merged_record(group)

            # add group merger item to DB
            self.database.add_merged_record(merged_record)

            # add updated references
            for concept_id in group:
                merge_ref = merged_record["concept_id"]
                try:
                    self.database.update_merge_ref(concept_id, merge_ref)
                except DatabaseWriteError as e:
                    if str(e).startswith("No such record exists"):
                        logger.exception(
                            "Updating nonexistent record: %s for merge ref to %s",
                            concept_id,
                            merge_ref,
                        )
                    else:
                        logger.exception(
                            "Unrecognized database write error encountered"
                        )
            uploaded_ids |= group
        self.database.complete_write_transaction()
        logger.info("Merged concept generation successful.")
        end = timer()
        logger.debug("Generated and added concepts in %s seconds", end - start)

    def _get_drugsatfda_from_unii(self, ref: str) -> str | None:
        """Given an `associated_with` item keying a UNII code to a Drugs@FDA record,
        verify that the record can be safely added to a concept group.
        Drugs@FDA tracks a number of "compound therapies", and provides UNIIs to each
        individual component. If we included them in normalized record sets, they would
        end up merging distinct therapies under the umbrella of the compound group.
        We're excluding Drugs@FDA records with multiple UNIIs as a tentative solution.

        :param ref: a Drugs@FDA concept ID that includes an xref to a UNII identifier
        :return: Drugs@FDA concept ID if record meets rules, None otherwise
        """
        fetched = self.database.get_record_by_id(ref, False)
        if fetched:
            uniis = [
                a for a in fetched.get("associated_with", []) if a.startswith("unii")
            ]
        else:
            logger.error("Couldn't retrieve record for %s", ref)
            return None
        if len(uniis) == 1:
            return fetched["concept_id"]
        return None

    def _get_xrefs(self, record: dict[str, Any]) -> set[str]:
        """Extract references to entries in other sources from a record.

        :param Dict record: record to process
        :return: Set of xref values
        """
        xrefs = set(record.get("xrefs", []))
        unii_xrefs = [
            a for a in record.get("associated_with", []) if a.startswith("unii:")
        ]
        for unii in unii_xrefs:
            # get Drugs@FDA records that are associated_with this UNII
            drugsatfda_ids = self._unii_to_drugsatfda.get(unii)
            if drugsatfda_ids is not None:
                xrefs |= drugsatfda_ids
                continue

            unii_assoc = self.database.get_refs_by_type(
                unii.lower(), RefType.ASSOCIATED_WITH
            )
            drugsatfda_refs = set()
            for ref in unii_assoc:
                if ref.startswith("drugsatfda"):
                    drugsatfda_ref = self._get_drugsatfda_from_unii(ref)
                    if drugsatfda_ref:
                        drugsatfda_refs.add(drugsatfda_ref)
            self._unii_to_drugsatfda[unii] = drugsatfda_refs
            xrefs |= drugsatfda_refs
        return xrefs

    def _create_record_id_set(
        self, record_id: str, observed_id_set: set | None = None
    ) -> set[str]:
        """Create concept ID group for an individual record ID.

        :param str record_id: concept ID for record to build group from
        :return: set of related identifiers pertaining to a common concept.
        """
        if observed_id_set is None:
            observed_id_set = set()

        if record_id in self._groups:
            return self._groups[record_id]
        if record_id.startswith("drugsatfda"):
            return {record_id}
        if record_id in self._failed_lookups:
            return observed_id_set - {record_id}

        # get record
        db_record = self.database.get_record_by_id(record_id)
        if not db_record:
            if record_id.startswith("rxcui"):
                brand_lookup = self.database.get_rxnorm_id_by_brand(record_id)
                if brand_lookup:
                    return self._create_record_id_set(brand_lookup, observed_id_set)
            logger.warning(
                "Unable to resolve lookup for %s in ID set: %s",
                record_id,
                observed_id_set,
            )
            self._failed_lookups.add(record_id)
            return observed_id_set - {record_id}

        # construct group
        local_id_set = self._get_xrefs(db_record)
        if not local_id_set:
            return observed_id_set | {db_record["concept_id"]}
        merged_id_set = {record_id} | observed_id_set
        for local_record_id in local_id_set - observed_id_set:
            merged_id_set |= self._create_record_id_set(local_record_id, merged_id_set)
        return merged_id_set

    def _create_record_id_sets(self, record_ids: set[str]) -> None:
        """Update self._groups with normalized concept groups.
        :param Set[str] record_ids: concept identifiers from which groups should be
            generated.
        """
        logger.info("Generating record ID sets...")
        start = timer()
        for record_id in record_ids:
            new_group = self._create_record_id_set(record_id)
            if new_group:
                for concept_id in new_group:
                    self._groups[concept_id] = new_group
        end = timer()
        logger.debug("Built record ID sets in %s seconds", end - start)

    _biologic_suffix_pattern = re.compile(r"^(.*)[ -][a-z]{4}$")

    def _sort_records(self, records: list[dict]) -> list[dict]:
        """Ensure proper sorting of records in group.

        First, order by source priority and tiebreak by smallest concept ID value.
        Then, if the first entry appears to be an RxNorm biosimilar, find the base
        therapeutic concept and move it in the front. This is necessary to ensure that
        the normalized drug's label is something like "trastuzumab" and not
        "trastuzumab-abcd". See
        https://www.fda.gov/files/drugs/published/Nonproprietary-Naming-of-Biological-Products-Guidance-for-Industry.pdf,
        and https://github.com/cancervariants/therapy-normalization/issues/299.

        This method is broken out to faciliate more direct testing.

        :param records: List of records in normalized group
        :return: sorted records list
        """

        def _record_order(record: dict) -> tuple[int, str]:
            """Provide priority values of concepts for sort function.

            :param record: individual therapy record
            :return: tuple (sortable) of source priority, and then concept ID
            :raise ValueError: if unrecognized source
            """
            src = record["src_name"].upper()
            if src == "DRUGS@FDA":
                src = "DRUGSATFDA"
            if src in SourcePriority.__members__:
                source_rank = SourcePriority[src].value
            else:
                msg = f"Prohibited source: {src} in concept_id {record['concept_id']}"
                raise ValueError(msg)
            return source_rank, record["concept_id"]

        records.sort(key=_record_order)

        if len([r for r in records if r["src_name"] == SourceName.RXNORM]) <= 1:
            return records
        first_match = re.findall(
            self._biologic_suffix_pattern, records[0].get("label", "")
        )
        if first_match:
            base = first_match[0].lower()
            for i, record in enumerate(records[1:], start=1):
                if (
                    record["src_name"] == SourceName.RXNORM
                    and record.get("label", "").lower() == base
                ):
                    logger.debug(
                        "Reordering RxNorm entry %s ahead of biosimilars",
                        record["concept_id"],
                    )
                    main_record = records.pop(i)
                    records = [main_record, *records]
                    break
        return records

    def _generate_merged_record(self, record_id_set: set[str]) -> dict:
        """Generate merged record from provided concept ID group.
        Where attributes are 'set-like', they should be combined, and where
        they are 'scalar-like', assign from the highest-priority source where
        that attribute is not None.

        Uses the SourcePriority schema to define source priority.

        :param Set record_id_set: group of concept IDs
        :return: completed merged drug object to be stored in DB
        """
        records = []
        for record_id in record_id_set:
            record = self.database.get_record_by_id(record_id)
            if record:
                records.append(record)
            else:
                logger.error(
                    "Merge record generator could not retrieve record for %s in %s",
                    record_id,
                    record_id_set,
                )
        records = self._sort_records(records)

        # initialize merged record
        merged_attrs = {
            "concept_id": records[0]["concept_id"],
            "xrefs": [r["concept_id"] for r in records[1:]],
            "label": None,
            "aliases": set(),
            "trade_names": set(),
            "associated_with": set(),
            "approval_ratings": set(),
            "approval_year": set(),
            "has_indication": [],
        }

        # merge from constituent records
        set_fields = ["aliases", "trade_names", "associated_with", "approval_year"]
        for record in records:
            for field in set_fields:
                merged_attrs[field] |= set(record.get(field, set()))
            approval_ratings = record.get("approval_ratings")
            if approval_ratings:
                merged_attrs["approval_ratings"] |= set(approval_ratings)
            label = record.get("label")
            if label:
                if merged_attrs["label"] is None:
                    merged_attrs["label"] = label
                elif label != merged_attrs["label"]:
                    merged_attrs["aliases"].add(label)
            for ind in record.get("has_indication", []):
                if ind not in merged_attrs["has_indication"]:
                    merged_attrs["has_indication"].append(ind)

        # clear unused fields
        for field in [*set_fields, "has_indication", "approval_ratings"]:
            field_value = merged_attrs[field]
            if field_value:
                merged_attrs[field] = list(field_value)
            else:
                del merged_attrs[field]
        if not merged_attrs["label"]:
            del merged_attrs["label"]

        merged_attrs["label_and_type"] = f"{merged_attrs['concept_id'].lower()}##merger"
        merged_attrs["item_type"] = "merger"
        return merged_attrs

ALTER TABLE therapy_aliases ADD CONSTRAINT therapy_aliases_concept_id_fkey
    FOREIGN KEY (concept_id) REFERENCES therapy_concepts (concept_id);
ALTER TABLE therapy_associations ADD CONSTRAINT therapy_associations_concept_id_fkey
    FOREIGN KEY (concept_id) REFERENCES therapy_concepts (concept_id);
ALTER TABLE therapy_labels ADD CONSTRAINT therapy_labels_concept_id_fkey
    FOREIGN KEY (concept_id) REFERENCES therapy_concepts (concept_id);
ALTER TABLE therapy_trade_names ADD CONSTRAINT therapy_trade_names_concept_id_fkey
    FOREIGN KEY (concept_id) REFERENCES therapy_concepts (concept_id);
ALTER TABLE therapy_xrefs ADD CONSTRAINT therapy_xrefs_concept_id_fkey
    FOREIGN KEY (concept_id) REFERENCES therapy_concepts (concept_id);
ALTER TABLE therapy_rx_brand_ids ADD CONSTRAINT therapy_rx_brand_ids_concept_id_fkey
    FOREIGN KEY (concept_id) REFERENCES therapy_concepts (concept_id);

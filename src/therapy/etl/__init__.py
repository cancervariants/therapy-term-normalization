"""Module to load and init namespace at package level"""

from .base import EtlError
from .chembl import ChEMBL
from .chemidplus import ChemIDplus
from .drugbank import DrugBank
from .drugsatfda import DrugsAtFDA
from .guidetopharmacology import GuideToPHARMACOLOGY
from .hemonc import HemOnc
from .merge import Merge
from .ncit import NCIt
from .rxnorm import RxNorm
from .wikidata import Wikidata

__all__ = [
    "ChEMBL",
    "ChemIDplus",
    "DrugBank",
    "DrugsAtFDA",
    "EtlError",
    "GuideToPHARMACOLOGY",
    "HemOnc",
    "Merge",
    "NCIt",
    "RxNorm",
    "Wikidata",
]

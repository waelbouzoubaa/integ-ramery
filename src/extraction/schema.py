from pydantic import BaseModel


class PriceItem(BaseModel):
    numero: str
    chapitre: str
    sous_famille: str | None = None
    designation: str
    unite: str | None = None
    quantite: float | None = None
    prix_unitaire: float
    montant_ht: float | None = None


class ExtractionResult(BaseModel):
    items: list[PriceItem]


class DesignationSansPrix(BaseModel):
    numero: str | None = None
    chapitre: str
    sous_famille: str | None = None
    designation: str
    unite: str | None = None


class ExtractionSansPrix(BaseModel):
    items: list[DesignationSansPrix]

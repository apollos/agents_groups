"""Target Profile manager (spec section 5).

Flat configuration, no knowledge graph. Wraps the raw config dict so that the
rest of the pipeline can read placeholder values and entity lists uniformly for
both company and industry targets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TargetProfile:
    target_id: str
    type: str
    canonical_name: str
    aliases: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)
    business_segments: list[str] = field(default_factory=list)
    customers: list[str] = field(default_factory=list)
    suppliers: list[str] = field(default_factory=list)
    competitors: list[str] = field(default_factory=list)
    upstream_terms: list[str] = field(default_factory=list)
    downstream_terms: list[str] = field(default_factory=list)
    core_metrics: list[str] = field(default_factory=list)
    representative_companies: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, data: dict[str, Any]) -> TargetProfile:
        return cls(
            target_id=data["target_id"],
            type=data.get("type", "company"),
            canonical_name=data.get("canonical_name", data["target_id"]),
            aliases=data.get("aliases", []),
            products=data.get("products", []),
            business_segments=data.get("business_segments", []),
            customers=data.get("known_customers", data.get("customers", [])),
            suppliers=data.get("known_suppliers", data.get("suppliers", [])),
            competitors=data.get("competitors", []),
            upstream_terms=data.get("upstream_terms", []),
            downstream_terms=data.get("downstream_terms", []),
            core_metrics=data.get("core_metrics", []),
            representative_companies=data.get("representative_companies", []),
            regions=data.get("regions", []),
            raw=data,
        )

    @property
    def primary_name(self) -> str:
        """Best short name to use as {company} / {industry} in templates."""
        if self.type == "company":
            return self.aliases[0] if self.aliases else self.canonical_name
        return self.canonical_name

    @property
    def industry_name(self) -> str:
        """Industry term for {industry} placeholder."""
        if self.type == "industry":
            return self.canonical_name
        # For a company, fall back to its first business segment / product.
        if self.business_segments:
            return self.business_segments[0]
        if self.products:
            return self.products[0]
        return self.canonical_name

    def all_entity_terms(self) -> list[str]:
        """Every term that should count as a 'target entity match' in triage."""
        terms = [self.canonical_name, *self.aliases, *self.products,
                 *self.customers, *self.suppliers, *self.competitors,
                 *self.representative_companies]
        if self.type == "industry":
            terms.append(self.canonical_name)
        return [t for t in dict.fromkeys(terms) if t]

    def placeholder_values(self) -> dict[str, list[str]]:
        """Maps query-template placeholders to candidate fill values."""
        company = [self.primary_name] if self.type == "company" else self.representative_companies
        industry = [self.industry_name]
        return {
            "company": [c for c in company if c] or [self.canonical_name],
            "industry": [i for i in industry if i] or [self.canonical_name],
            "product": self.products,
            "customer": self.customers,
            "supplier": self.suppliers,
            "competitor": self.competitors,
            "upstream_material": self.upstream_terms,
            "downstream": self.downstream_terms,
            # Project / tender placeholders have no direct profile field; left
            # empty so templates requiring them are skipped unless overridden.
            "project": [],
            "tender": [],
        }

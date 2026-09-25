"""Independent checks on a proposal; never invent missing financial evidence."""

from __future__ import annotations

import math
from typing import Any


def walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def ids(value: Any, *keys: str) -> list[str]:
    found: list[str] = []
    for obj in walk(value):
        for key in keys:
            item = obj.get(key)
            candidates = item if isinstance(item, list) else [item]
            for candidate in candidates:
                if isinstance(candidate, str) and candidate and candidate not in found:
                    found.append(candidate)
    return found


ENTITY_KEYS = {
    "item_ids": ("item_id", "order_item_id", "item_ids"),
    "seller_ids": ("seller_id", "seller_ids", "late_seller_ids"),
    "shipment_ids": ("shipment_id", "shipment_ids", "tracking_id"),
    "payment_references": (
        "payment_reference", "payment_ref", "payment_id", "transaction_id",
        "payment_references", "capture_id",
    ),
}


def verify_proposal(
    result: dict[str, Any], case: dict[str, Any], records: list[dict[str, Any]],
    investigated_order: str | None,
) -> None:
    """Call after JSON Schema validation so all expected field types are known."""
    errors: list[str] = []
    available = {entry["evidence_ref"] for entry in records}
    cited = set(result["evidence_refs"])
    if result["case_id"] != case["case_id"]:
        errors.append("case_id differs from input")
    if not cited or not cited <= available:
        errors.append("evidence_refs must be nonempty and belong to this case's MCP results")
    for obj in walk(result):
        for value in obj.values():
            if isinstance(value, float) and not math.isfinite(value):
                errors.append("non-finite number")
    expected_claims = [c["claim_id"] for c in case.get("customer_request", {}).get("claims", [])]
    claims = result.get("claim_assessments", [])
    actual_claims = [c["claim_id"] for c in claims]
    if sorted(actual_claims) != sorted(expected_claims):
        errors.append("claim_assessments must assess every input claim exactly once")
    for claim in claims:
        if not set(claim["evidence_refs"]) <= cited:
            errors.append("claim evidence must appear in top-level evidence_refs")
        if (claim["verdict"] in {"supported", "partially_supported", "unsupported"}
                and not claim["evidence_refs"]):
            errors.append("a conclusive claim verdict requires evidence")

    entity = result["entity_resolution"]
    resolved = set(entity["resolved_order_ids"])
    assessment = result["assessment"]
    if resolved & set(entity["rejected_candidates"]):
        errors.append("an order cannot be both resolved and rejected")
    request = case.get("customer_request", {})
    candidates = set(case.get("candidate_order_ids", []))
    if request.get("claimed_order_id"):
        candidates.add(request["claimed_order_id"])
    if not set(entity["rejected_candidates"]) <= candidates:
        errors.append("rejected_candidates must come from the input candidates")
    if entity["status"] == "resolved":
        if investigated_order is None or resolved != {investigated_order}:
            errors.append("resolved entity must be the investigated order")
    elif resolved or assessment["case_status"] != "needs_investigation":
        errors.append("unresolved entity needs investigation and no resolved order IDs")
    if set(result["affected_entities"]["order_ids"]) != resolved:
        errors.append("affected order IDs must equal resolved order IDs")
    if entity["status"] != "resolved" and assessment["primary_issue"] != "insufficient_evidence":
        errors.append("unresolved entity requires insufficient_evidence")
    scoped = [r["data"] for r in records if r["arguments"].get("order_id") == investigated_order]
    for field, keys in ENTITY_KEYS.items():
        if not set(result["affected_entities"][field]) <= set(ids(scoped, *keys)):
            errors.append(f"{field} contains IDs absent from investigated order evidence")
    customer = result["customer_context"]
    evidence_data = [r["data"] for r in records]
    if (customer["customer_unique_id"] is not None
            and customer["customer_unique_id"] not in ids(evidence_data, "customer_unique_id")):
        errors.append("customer_unique_id must be supported by evidence")
    observed_orders = set(ids(evidence_data, "order_id", "order_ids", "related_order_ids"))
    if not set(customer["related_order_ids"]) <= observed_orders:
        errors.append("related orders must be supported by MCP evidence")

    payment = result["payment_analysis"]
    financial = result["financial_resolution"]
    refund = financial["recommended_refund_brl"]
    lines_total = sum(line["amount_brl"] for line in financial["refund_lines"])
    if abs(lines_total - refund) > 0.011:
        errors.append("refund line sum differs from recommended refund")
    captured, refunded = payment["captured_total_brl"], payment["refunded_total_brl"]
    if captured is not None and refunded is not None:
        outstanding = max(0.0, round(captured - refunded, 2))
        if refunded > captured + 0.011:
            errors.append("refunded exceeds captured; resolve financial evidence conflict")
        if refund > outstanding + 0.011:
            errors.append("recommended refund exceeds captured minus already refunded")
        if (payment["refundable_total_brl"] is not None
                and payment["refundable_total_brl"] > outstanding + 0.011):
            errors.append("refundable total exceeds remaining captured amount")
    if refund and (captured is None or refunded is None):
        errors.append("positive refund requires known captured and already-refunded totals")
    if (payment["refundable_total_brl"] is not None
            and refund > payment["refundable_total_brl"] + 0.011):
        errors.append("recommended refund exceeds eligible refundable amount")
    if refund and assessment["case_status"] != "action_required":
        errors.append("positive refund requires action_required")
    if assessment["case_status"] == "action_required" and not result["resolution_actions"]:
        errors.append("action_required needs at least one resolution action")
    issue = assessment["primary_issue"]
    payment_for_issue = {
        "duplicate_charge": "duplicate_capture", "payment_mismatch": "capture_mismatch",
        "valid_split_payment": "reconciled", "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }
    if issue in payment_for_issue and payment["verdict"] != payment_for_issue[issue]:
        errors.append("payment verdict contradicts primary issue")
    if (issue == "valid_split_payment" and not assessment["secondary_issues"]
            and (refund or assessment["case_status"] != "no_action")):
        errors.append("valid split payment alone requires no action and zero refund")
    shipment = result["shipment_analysis"]
    for primary, verdict in (
        ("late_delivery_seller", "seller_delay"),
        ("late_delivery_logistics", "logistics_delay"),
    ):
        if issue == primary and shipment["verdict"] != verdict:
            errors.append("shipment verdict contradicts primary issue")
    if not set(shipment["late_seller_ids"]) <= set(result["affected_entities"]["seller_ids"]):
        errors.append("late sellers must be affected sellers")
    parties = result["root_cause_analysis"]["responsible_parties"]
    for party in parties:
        if (party["party_type"] == "seller"
                and party["party_id"] not in result["affected_entities"]["seller_ids"]):
            errors.append("responsible seller is not an evidenced affected seller")
    for conflict in result["data_conflicts"]:
        if (conflict["selected_source"] is not None
                and conflict["selected_source"] not in conflict["sources"]):
            errors.append("selected conflict source must occur in sources")
    ranks = [cause["rank"] for cause in result["root_cause_analysis"]["ranked_causes"]]
    if ranks != list(range(1, len(ranks) + 1)):
        errors.append("root causes must have consecutive ranks starting from 1")
    if errors:
        raise ValueError("; ".join(dict.fromkeys(errors)))

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest

from student_agent import workflow
from student_agent.cli import _prepare_run
from student_agent.contracts import Contracts
from student_agent.model import assessment_api_config
from student_agent.trace import TraceWriter
from student_agent.verification import verify_proposal

ROOT = Path(__file__).resolve().parents[1]


def case_fixture():
    return {
        "case_id": "TEST_CASE_001", "opened_at": "2026-01-01T00:00:00Z",
        "policy_version": "POLICY_TEST",
        "customer_unique_id_hint": "customer-a",
        "candidate_order_ids": ["order-other", "order-good"],
        "customer_request": {
            "claimed_order_id": "order-other", "message": "Request refund for canceled order",
            "claims": [{"claim_id": "claim-a", "topic": "canceled_order_paid"}],
        },
        "investigation_scope": {
            "include_customer_history": True, "include_product_context": True,
            "require_independent_verification": True,
        },
    }


def evidence_fixture():
    return {
        "get_customer_history": {
            "customer_unique_id": "customer-a", "orders": [{"order_id": "order-good"}],
        },
        "get_order": {
            "order_id": "order-good", "customer_unique_id": "customer-a", "status": "canceled",
        },
        "get_order_items": {"items": [{"item_id": "item-a", "seller_id": "seller-a"}]},
        "get_shipment_summary": {"status": "not_shipped", "events": []},
        "get_order_payments": {"payment_id": "payment-a", "pending_refund": False},
        "get_payment_timeline": {"captured_total_brl": 120},
        "get_refund_timeline": {"refunded_total_brl": 20},
        "get_policy": {"canceled_order_refund": "remaining captured amount"},
        "get_product_context": {"products": [{"product_id": "product-a"}]},
    }


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.responses = evidence_fixture()

    async def call(self, tool, *, case_id, **arguments):
        self.calls.append((tool, case_id, arguments))
        if tool == "get_product_context":
            assert arguments == {"order_id": "order-good"}
        return {
            "evidence_ref": "ev_" + hashlib.sha256(tool.encode()).hexdigest()[:32],
            "data": self.responses[tool],
        }


def proposal_fixture(records):
    refs = [r["evidence_ref"] for r in records]
    return {
        "schema_version": "day09-l3b-output-v2", "case_id": "TEST_CASE_001",
        "assessment": {"primary_issue": "canceled_order_paid", "secondary_issues": [],
                       "case_status": "action_required", "confidence": 0.85},
        "affected_entities": {"order_ids": ["order-good"], "item_ids": ["item-a"],
                              "seller_ids": ["seller-a"], "payment_references": ["payment-a"],
                              "shipment_ids": []},
        "claim_assessments": [{"claim_id": "claim-a", "verdict": "supported",
                               "confidence": 0.85, "evidence_refs": refs[:2]}],
        "entity_resolution": {"status": "resolved", "resolved_order_ids": ["order-good"],
                              "rejected_candidates": [], "confidence": 0.9},
        "customer_context": {"customer_unique_id": "customer-a",
                             "related_order_ids": ["order-good"]},
        "shipment_analysis": {"verdict": "insufficient_evidence", "late_seller_ids": [],
                              "timeline_complete": False},
        "payment_analysis": {"verdict": "reconciled", "captured_total_brl": 120,
                             "refunded_total_brl": 20, "refundable_total_brl": 100},
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "CANCELED_AFTER_CAPTURE", "rank": 1}],
            "responsible_parties": [{"party_type": "platform", "party_id": None}],
        },
        "evidence_refs": refs, "data_conflicts": [],
        "financial_resolution": {"currency": "BRL", "recommended_refund_brl": 100,
                                 "refund_lines": [{"reason_code": "CANCELLATION",
                                                   "amount_brl": 100, "entity_id": "order-good"}]},
        "resolution_actions": ["refund_remaining_capture"],
    }


def records_fixture():
    return [{"tool": tool, "arguments": {"order_id": "order-good"},
             "data": data, "evidence_ref": "ev_" + hashlib.sha256(tool.encode()).hexdigest()[:32]}
            for tool, data in evidence_fixture().items()]


def test_workflow_preserves_grounded_refund_and_uses_correct_product_argument(tmp_path, monkeypatch):
    async def model(system, context):
        assert "output_schema" in context
        assert context["entity_resolution_context"]["investigated_order"] == "order-good"
        return proposal_fixture(context["evidence"])

    monkeypatch.setattr(workflow, "request_json", model)
    gateway = FakeGateway()
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts/schemas"))
    result = asyncio.run(workflow.solve_case(case_fixture(), gateway, trace))
    assert result["financial_resolution"]["recommended_refund_brl"] == 100
    assert result["payment_analysis"]["verdict"] == "reconciled"
    assert [call[2] for call in gateway.calls if call[0] == "get_order"] == [
        {"order_id": "order-good"}
    ]
    assert len([call for call in gateway.calls if call[0] == "get_product_context"]) == 1
    assert all(call[1] == "TEST_CASE_001" for call in gateway.calls)
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    assert events[-1]["event_type"] == "verification_completed"


@pytest.mark.parametrize("defect", ["foreign_ref", "overrefund", "wrong_payment", "invented_id"])
def test_verifier_rejects_material_errors(defect):
    records = records_fixture()
    result = proposal_fixture(records)
    if defect == "foreign_ref":
        result["evidence_refs"].append("ev_" + "z" * 32)
    elif defect == "overrefund":
        result["financial_resolution"]["recommended_refund_brl"] = 150
        result["financial_resolution"]["refund_lines"][0]["amount_brl"] = 150
    elif defect == "wrong_payment":
        result["assessment"]["primary_issue"] = "payment_mismatch"
    else:
        result["affected_entities"]["seller_ids"].append("invented-seller")
    with pytest.raises(ValueError):
        verify_proposal(result, case_fixture(), records, "order-good")


def test_customer_hint_is_not_evidence():
    records = records_fixture()
    result = proposal_fixture(records)
    result["customer_context"]["customer_unique_id"] = "invented-customer"
    records[0]["arguments"]["customer_unique_id"] = "invented-customer"
    with pytest.raises(ValueError, match="customer_unique_id"):
        verify_proposal(result, case_fixture(), records, "order-good")


def test_invalid_model_proposal_gets_one_repair_without_new_mcp_calls(tmp_path, monkeypatch):
    calls = []

    async def model(system, context):
        calls.append(copy.deepcopy(context))
        result = proposal_fixture(context["evidence"])
        if len(calls) == 1:
            result["payment_analysis"]["refundable_total_brl"] = 999
        return result

    monkeypatch.setattr(workflow, "request_json", model)
    gateway = FakeGateway()
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts/schemas"))
    asyncio.run(workflow.solve_case(case_fixture(), gateway, trace))
    assert len(calls) == 2
    assert "validation_error" in calls[1]
    assert len(gateway.calls) == 9


def test_model_over_parameter_limit_is_rejected(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "test-account")
    monkeypatch.setenv("CLOUDFLARE_MODEL", "@cf/qwen/qwen3-30b-a3b-fp8")
    with pytest.raises(ValueError, match="under-10B"):
        assessment_api_config()


def test_legacy_outputs_are_preserved_and_not_silently_reused(tmp_path):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    old = outputs / "TEST_CASE_001.json"
    old.write_text('{"baseline": true}')
    with pytest.raises(ValueError, match="previous workflow"):
        _prepare_run(ROOT, tmp_path, {"TEST_CASE_001": case_fixture()}, "model")
    assert old.read_text() == '{"baseline": true}'


def test_input_change_rejects_resume(tmp_path):
    cases = {"TEST_CASE_001": case_fixture()}
    _prepare_run(ROOT, tmp_path, cases, "model")
    _prepare_run(ROOT, tmp_path, cases, "model")
    cases["TEST_CASE_001"]["policy_version"] = "CHANGED"
    with pytest.raises(ValueError, match="changed"):
        _prepare_run(ROOT, tmp_path, cases, "model")

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kernel import PRCSAKernel, run_operator_step, MockOperator


def test_coupling_gate_rejects_overconfident_real_claim():
    """A candidate with evidence_class REAL and a high self-reported
    model_confidence, but no structural support in the projection, must be
    rejected at the COUPLING_GATE stage before ever reaching commit_claim --
    and nothing should land in the claims table.
    """
    dbpath = tempfile.mktemp(suffix=".db")
    try:
        k = PRCSAKernel(dbpath)

        overconfident_candidate = {
            "content": "commit_claim() averages 4.2ms per call in production.",
            "scope": {},
            "evidence_class": "REAL",
            "kind": "CLAIM",
            "model_confidence": 0.9,
            "falsification_test": None,
        }
        adapter = MockOperator(label="overconfident-mock", fixed_response=overconfident_candidate)

        result = run_operator_step(
            k, adapter,
            task="State the average commit_claim() latency in production.",
            seed_ids=[], node_budget=5, max_tokens=4096,
        )

        assert result["stage"] == "COUPLING_GATE"
        assert result["result"] == "REJECTED"
        assert result["coupling"]["verdict"] == "HIGH_GEN_LOW_SUPPORT"

        claim_count = k.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        assert claim_count == 0

        logged_outcome = k.conn.execute(
            "SELECT outcome FROM operator_log ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        assert logged_outcome == "REJECTED"
    finally:
        k.conn.close()
        if os.path.exists(dbpath):
            os.remove(dbpath)


def _fresh_kernel_with_seed():
    dbpath = tempfile.mktemp(suffix=".db")
    k = PRCSAKernel(dbpath)
    seed_cid = k.commit_claim(
        content="Seed claim for relation testing.",
        scope={}, evidence_class="INFERRED", based_on_version=k.current_version(),
        kind="CLAIM", falsification_test="n/a",
    )
    return dbpath, k, seed_cid


def test_about_relation_applied_automatically():
    """ABOUT is the one relation type project() only ever treats as optional
    fill -- a model proposing it should see the edge land in relations
    immediately, no review step.
    """
    dbpath, k, seed_cid = _fresh_kernel_with_seed()
    try:
        candidate = {
            "content": "A related but low-stakes observation.",
            "scope": {}, "evidence_class": "INFERRED", "kind": "CLAIM",
            "falsification_test": "n/a",
            "relation": {"target_claim_id": seed_cid, "relation_type": "ABOUT"},
        }
        result = run_operator_step(
            k, MockOperator(fixed_response=candidate), "task",
            seed_ids=[seed_cid], node_budget=5, max_tokens=4096,
        )

        assert result["relation"]["status"] == "APPLIED"
        rel_row = k.conn.execute(
            "SELECT from_ref, to_ref, relation_type FROM relations WHERE relation_id=?",
            (result["relation"]["relation_id"],),
        ).fetchone()
        assert rel_row == (result["claim_id"], seed_cid, "ABOUT")
    finally:
        k.conn.close()
        if os.path.exists(dbpath):
            os.remove(dbpath)


def test_depends_on_relation_requires_review_before_writing():
    """DEPENDS_ON (and CONTRADICTS) are hinge types project() treats as
    must-keep -- a model's self-report on one must land in
    relation_proposals as PENDING, never directly in relations, until a
    human calls review_relation_proposal().
    """
    dbpath, k, seed_cid = _fresh_kernel_with_seed()
    try:
        candidate = {
            "content": "A claim that depends on the seed in a load-bearing way.",
            "scope": {}, "evidence_class": "INFERRED", "kind": "CLAIM",
            "falsification_test": "n/a",
            "relation": {"target_claim_id": seed_cid, "relation_type": "DEPENDS_ON", "rationale": "testing"},
        }
        result = run_operator_step(
            k, MockOperator(fixed_response=candidate), "task",
            seed_ids=[seed_cid], node_budget=5, max_tokens=4096,
        )

        assert result["relation"]["status"] == "PENDING_REVIEW"
        proposal_id = result["relation"]["proposal_id"]

        # Nothing written to relations yet.
        assert k.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0
        pending = k.list_pending_relation_proposals()
        assert len(pending) == 1 and pending[0]["proposal_id"] == proposal_id

        # Approval is the only thing that writes the edge, and only after
        # the fact, under a human decision -- not the model's own say-so.
        decision = k.review_relation_proposal(proposal_id, "APPROVED", reviewer="test")
        assert decision["status"] == "APPROVED"
        rel_row = k.conn.execute(
            "SELECT from_ref, to_ref, relation_type FROM relations WHERE relation_id=?",
            (decision["applied_relation_id"],),
        ).fetchone()
        assert rel_row == (result["claim_id"], seed_cid, "DEPENDS_ON")

        # A decided proposal can't be decided again.
        try:
            k.review_relation_proposal(proposal_id, "REJECTED")
            assert False, "expected ValidationError on re-deciding a proposal"
        except Exception:
            pass
    finally:
        k.conn.close()
        if os.path.exists(dbpath):
            os.remove(dbpath)


def test_rejected_relation_proposal_never_writes_relation():
    """Rejecting a proposal must update its status without ever touching
    the relations table.
    """
    dbpath, k, seed_cid = _fresh_kernel_with_seed()
    try:
        candidate = {
            "content": "Contradicts the seed.", "scope": {}, "evidence_class": "INFERRED",
            "kind": "CLAIM", "falsification_test": "n/a",
            "relation": {"target_claim_id": seed_cid, "relation_type": "CONTRADICTS"},
        }
        result = run_operator_step(
            k, MockOperator(fixed_response=candidate), "task",
            seed_ids=[seed_cid], node_budget=5, max_tokens=4096,
        )
        proposal_id = result["relation"]["proposal_id"]

        decision = k.review_relation_proposal(proposal_id, "REJECTED", note="not convinced")
        assert decision["status"] == "REJECTED"
        assert decision["applied_relation_id"] is None
        assert k.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0
    finally:
        k.conn.close()
        if os.path.exists(dbpath):
            os.remove(dbpath)


def test_relation_target_must_be_in_projected_workspace():
    """A target_claim_id that exists in the claims table but was never part
    of the projection actually shown to the model must be rejected as
    INVALID -- prevents a hallucinated-but-coincidentally-real id from
    creating a proposal (or an ABOUT edge) for a claim the model never saw.
    """
    dbpath, k, seed_cid = _fresh_kernel_with_seed()
    try:
        candidate = {
            "content": "References a claim never shown to it.",
            "scope": {}, "evidence_class": "INFERRED", "kind": "CLAIM",
            "falsification_test": "n/a",
            "relation": {"target_claim_id": seed_cid, "relation_type": "DEPENDS_ON"},
        }
        # seed_ids=[] -- empty projection, so seed_cid is never in workspace.
        result = run_operator_step(
            k, MockOperator(fixed_response=candidate), "task",
            seed_ids=[], node_budget=5, max_tokens=4096,
        )

        assert result["relation"]["status"] == "INVALID"
        assert k.list_pending_relation_proposals() == []
        assert k.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0
    finally:
        k.conn.close()
        if os.path.exists(dbpath):
            os.remove(dbpath)

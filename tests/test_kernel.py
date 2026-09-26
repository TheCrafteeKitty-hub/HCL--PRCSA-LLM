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

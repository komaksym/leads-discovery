"""Independent black-box contract tests for M4 contact discovery and ranking."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from m3_factories import accepted_facts, build_company, low_score_facts
from m4_contract_fixtures import (
    ClayRoutineScript,
    Responder,
    WireStub,
    call_enrich_live,
    install_mock_http,
    json_body,
    person_result,
    prepare_evaluated_run,
    read_jsonl,
    row_text,
    set_m4_credentials,
)


def _exa_results(results: list[dict[str, Any]]) -> Responder:
    """Return one Exa responder serving a fixed bounded People Search result list."""

    def responder(request: httpx.Request) -> httpx.Response:
        """Serve one successful Exa response."""
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={"results": results, "costDollars": {"total": 0.001}},
        )

    return responder


def _instantly_verified(request: httpx.Request) -> httpx.Response:
    """Return verified status for an existing email only."""
    body = json_body(request) if request.method == "POST" else {}
    email = str(body.get("email") or request.url.path.rsplit("/", 1)[-1])
    return httpx.Response(
        200,
        json={
            "email": email,
            "status": "completed",
            "verification_status": "verified",
            "credits_used": 1,
        },
    )


def _apollo_miss(_request: httpx.Request) -> httpx.Response:
    """Return a completed one-credit Apollo miss without personal data."""
    return httpx.Response(
        200,
        json={"status": "completed", "credits_used": 1, "person": {"email": None}},
    )


def _resume_clay_until_complete(
    *,
    data_root: Path,
    run_dir: Path,
    run_id: str,
    clay: ClayRoutineScript,
    max_invocations: int = 8,
) -> None:
    """Drive every Clay POST through durable state and a later GET before completion."""
    code = call_enrich_live(data_root, run_id)
    assert code != 0
    for _ in range(max_invocations):
        latest = clay.latest_run_id
        assert latest is not None
        checkpoint = run_dir / "contact_checkpoint.json"
        assert checkpoint.exists()
        assert latest in checkpoint.read_text(encoding="utf-8")
        clay.release_started()
        code = call_enrich_live(data_root, run_id)
        if code == 0:
            assert len(clay.posts) == len(clay.gets)
            return
    raise AssertionError("Clay lifecycle did not complete within bounded resume attempts")


def test_only_current_m3_accepted_companies_reach_people_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uncertain/rejected companies cause zero M4 calls while current accepted gets one Exa request."""
    rejected = deepcopy(accepted_facts())
    rejected["pvf_relevant"] = (False, 0.99)
    run_dir = prepare_evaluated_run(
        tmp_path,
        "decision-gate",
        [
            build_company(
                facts=accepted_facts(),
                company_id="cmp_accept",
                name="Accepted Valve",
                domain="accepted.example",
            ),
            build_company(
                facts=low_score_facts(),
                company_id="cmp_uncertain",
                name="Uncertain Valve",
                domain="uncertain.example",
            ),
            build_company(
                facts=rejected,
                company_id="cmp_reject",
                name="Rejected Valve",
                domain="rejected.example",
            ),
        ],
    )
    before = {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}
    stub = WireStub({"exa": _exa_results([])})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_enrich_live(tmp_path, "decision-gate") == 0

    exa = stub.for_provider("exa")
    assert len(exa) == 1
    body = json_body(exa[0])
    assert body.get("category") == "people"
    if "numResults" in body:
        assert isinstance(body["numResults"], int)
        assert body["numResults"] <= 10
    assert stub.for_provider("clay") == []
    assert stub.for_provider("apollo") == []
    assert stub.for_provider("instantly") == []
    for name, payload in before.items():
        assert (run_dir / name).read_bytes() == payload


def test_stale_contacts_cannot_authorize_enrichment_after_m3_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Previously persisted contacts do not bypass a newer non-accepted M3 decision."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "stale-contact",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    person = person_result(
        name="Pat Owner",
        title="President and Owner",
        company="Acme Valve",
        domain="acmevalve.com",
        profile_url="https://www.linkedin.com/in/pat-owner",
    )
    clay = ClayRoutineScript([])
    stub = WireStub({"exa": _exa_results([person]), "clay": clay, "apollo": _apollo_miss})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)
    _resume_clay_until_complete(
        data_root=tmp_path,
        run_dir=run_dir,
        run_id="stale-contact",
        clay=clay,
    )
    assert read_jsonl(run_dir / "contacts.jsonl")

    evaluated = read_jsonl(run_dir / "companies_evaluated.jsonl")
    assert len(evaluated) == 1
    evaluated[0]["final_decision"] = "rejected"
    (run_dir / "companies_evaluated.jsonl").write_text(
        json.dumps(evaluated[0], sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (run_dir / "contact_checkpoint.json").unlink()
    request_count = len(stub.requests)

    assert call_enrich_live(tmp_path, "stale-contact") == 0
    assert len(stub.requests) == request_count


def test_decision_proximity_caps_candidates_contacts_and_paid_enrichment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relevant buyers beat generic seniority; top three survive and rank three never spends."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "ranking",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    candidates = [
        person_result(
            name="Past Owner",
            title="Owner",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url="https://www.linkedin.com/in/past-owner",
            current=False,
        ),
        person_result(
            name="Terry Tech",
            title="Chief Technology Officer",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url="https://www.linkedin.com/in/terry-tech",
        ),
        person_result(
            name="Sam Engineer",
            title="Senior Software Engineer",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url="https://www.linkedin.com/in/sam-engineer",
        ),
        person_result(
            name="Pat Owner",
            title="President and Owner",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url="https://www.linkedin.com/in/pat-owner",
        ),
        person_result(
            name="Vera Ops",
            title="VP Operations",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url="https://www.linkedin.com/in/vera-ops",
        ),
        person_result(
            name="Erin Estimator",
            title="Estimating Manager",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url="https://www.linkedin.com/in/erin-estimator",
        ),
        person_result(
            name="Iris Inside",
            title="Inside Sales Manager",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url="https://www.linkedin.com/in/iris-inside",
        ),
        person_result(
            name="Ben Branch",
            title="Branch Manager",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url="https://www.linkedin.com/in/ben-branch",
        ),
        person_result(
            name="Ari Admin",
            title="Office Administrator",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url="https://www.linkedin.com/in/ari-admin",
        ),
        person_result(
            name="Casey Coordinator",
            title="Marketing Coordinator",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url="https://www.linkedin.com/in/casey-coordinator",
        ),
        person_result(
            name="Overflow Executive",
            title="Chief Executive Officer",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url="https://www.linkedin.com/in/overflow-executive",
        ),
    ]

    def clay_rows(_request: httpx.Request, _run_id: str) -> list[dict[str, Any]]:
        """Return work emails only for the first two decision-ranked contacts."""
        return [
            {
                "name": "Pat Owner",
                "profile_url": "https://www.linkedin.com/in/pat-owner",
                "work_email": "pat.owner@acmevalve.com",
                "email": "pat.owner@acmevalve.com",
            },
            {
                "name": "Vera Ops",
                "profile_url": "https://www.linkedin.com/in/vera-ops",
                "work_email": "vera.ops@acmevalve.com",
                "email": "vera.ops@acmevalve.com",
            },
        ]

    clay = ClayRoutineScript(clay_rows)
    stub = WireStub(
        {"exa": _exa_results(candidates), "clay": clay, "instantly": _instantly_verified}
    )
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)
    _resume_clay_until_complete(
        data_root=tmp_path,
        run_dir=run_dir,
        run_id="ranking",
        clay=clay,
    )

    contacts = read_jsonl(run_dir / "contacts.jsonl")
    assert len(contacts) <= 3
    persisted = "\n".join(row_text(row).casefold() for row in contacts)
    assert "pat owner" in persisted
    assert "vera ops" in persisted
    assert "erin estimator" in persisted
    assert "past owner" not in persisted
    assert "terry tech" not in persisted
    assert "sam engineer" not in persisted
    assert "overflow executive" not in persisted

    paid_payload = "\n".join(
        request.content.decode("utf-8").casefold() for request in clay.posts
    )
    assert "erin-estimator" not in paid_payload
    assert "erin estimator" not in paid_payload
    assert "phone" not in paid_payload
    assert "personal" not in paid_payload
    assert "email" in paid_payload
    assert "work" in paid_payload
    assert stub.for_provider("apollo") == []

    instant_payload = "\n".join(
        (request.content.decode("utf-8") + str(request.url)).casefold()
        for request in stub.for_provider("instantly")
    )
    assert "erin-estimator" not in instant_payload
    assert "erin estimator" not in instant_payload


def test_profile_url_and_name_domain_dedup_are_conservative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Formatting-noise duplicates collapse without merging distinct similar people."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "dedup",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    results = [
        person_result(
            name="Jane Owner",
            title="Owner",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url="https://linkedin.com/in/jane-owner/",
            person_id="jane-a",
        ),
        person_result(
            name="Jane Owner",
            title="Owner",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url="https://www.linkedin.com/in/jane-owner?trk=public#about",
            person_id="jane-b",
        ),
        person_result(
            name="Alex Manager",
            title="Operations Manager",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url=None,
            person_id="alex-a",
        ),
        person_result(
            name="  ALEX   MANAGER  ",
            title="Operations Manager",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url=None,
            person_id="alex-b",
        ),
        person_result(
            name="Alex Manager Jr",
            title="Estimating Manager",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url=None,
            person_id="alex-jr",
        ),
    ]
    clay = ClayRoutineScript([])
    stub = WireStub({"exa": _exa_results(results), "clay": clay, "apollo": _apollo_miss})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)
    _resume_clay_until_complete(
        data_root=tmp_path,
        run_dir=run_dir,
        run_id="dedup",
        clay=clay,
    )

    contacts = read_jsonl(run_dir / "contacts.jsonl")
    assert len(contacts) == 3
    text = [row_text(row).casefold() for row in contacts]
    assert sum("jane owner" in item for item in text) == 1
    assert sum("alex manager jr" in item for item in text) == 1
    assert sum("alex manager" in item and "jr" not in item for item in text) == 1


def test_same_name_at_different_company_domains_is_not_merged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback identity includes company domain, so equal names at two companies stay distinct."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "cross-company",
        [
            build_company(
                facts=accepted_facts(),
                company_id="cmp_alpha",
                name="Alpha Valve",
                domain="alpha.example",
            ),
            build_company(
                facts=accepted_facts(),
                company_id="cmp_beta",
                name="Beta Valve",
                domain="beta.example",
            ),
        ],
    )

    def exa(request: httpx.Request) -> httpx.Response:
        """Return the same name but different company identity per query."""
        query = str(json_body(request).get("query", "")).casefold()
        if "alpha" in query:
            company, domain = "Alpha Valve", "alpha.example"
        elif "beta" in query:
            company, domain = "Beta Valve", "beta.example"
        else:
            raise AssertionError(f"company identity missing from People Search query: {query}")
        result = person_result(
            name="Jordan Lee",
            title="General Manager",
            company=company,
            domain=domain,
            profile_url=None,
        )
        return httpx.Response(
            200,
            json={"results": [result], "costDollars": {"total": 0.001}},
        )

    clay = ClayRoutineScript([])
    stub = WireStub({"exa": exa, "clay": clay, "apollo": _apollo_miss})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)
    _resume_clay_until_complete(
        data_root=tmp_path,
        run_dir=run_dir,
        run_id="cross-company",
        clay=clay,
    )

    contacts = read_jsonl(run_dir / "contacts.jsonl")
    assert len(contacts) == 2
    persisted = "\n".join(row_text(row).casefold() for row in contacts)
    assert persisted.count("jordan lee") >= 2
    assert "cmp_alpha" in persisted
    assert "cmp_beta" in persisted
    assert len(stub.for_provider("exa")) == 2

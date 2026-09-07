from __future__ import annotations

from pathlib import Path
from textwrap import dedent


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one replacement, found {count}: {old[:100]!r}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


def replace_region(path: str, start: str, end: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    start_index = text.index(start)
    end_index = text.index(end, start_index)
    file.write_text(text[:start_index] + new + text[end_index:], encoding="utf-8", newline="\n")


def edit_region(
    path: str,
    start: str,
    end: str,
    changes: list[tuple[str, str, int]],
) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    start_index = text.index(start)
    end_index = text.index(end, start_index)
    region = text[start_index:end_index]
    for old, new, expected in changes:
        count = region.count(old)
        if count != expected:
            raise SystemExit(f"{path}: expected {expected}, found {count}: {old[:100]!r}")
        region = region.replace(old, new, expected)
    file.write_text(text[:start_index] + region + text[end_index:], encoding="utf-8", newline="\n")


coverage = "src/leads_discovery/pipeline/canary_provider_coverage.py"
helper_marker = "def _finite_nonnegative(value: float, label: str) -> float:\n"
coverage_helper = dedent('''\
def _normal_clay_skip_prerequisite(
    company: CompanyRecord,
    contact: ContactRecord,
    normal_contact: ContactRecord | None,
    normal_exa_completed: bool,
    normal_operations: dict[str, dict[str, Any]],
    normal_usage: list[UsageEvent],
) -> bool:
    """Allow private Apollo only when normal M4 skipped it after durable Clay success."""
    if (
        company.final_decision != "accepted"
        or not normal_exa_completed
        or normal_contact is None
        or contact.contact_id != normal_contact.contact_id
    ):
        return False
    normal_entry = _normal_operation_evidence(
        normal_operations,
        normal_usage,
        operation_id="clay:batch",
        provider="clay",
        operation="work_email_routine_start",
    )
    if normal_entry is None:
        return False
    raw_ids = normal_entry.get("contact_ids")
    if not isinstance(raw_ids, list) or contact.contact_id not in raw_ids:
        raise ValueError("normal Clay provider operation does not name the selected contact")
    result_requests = sum(
        event.request_count
        for event in normal_usage
        if event.provider == "clay" and event.operation == "work_email_routine_results"
    )
    if result_requests <= 0:
        raise ValueError(
            "normal Clay completed provider operation lacks authoritative results usage"
        )
    return (
        contact.email_source == "clay"
        and usable_work_email(contact.work_email) is not None
    )


''')
replace_once(coverage, helper_marker, coverage_helper + helper_marker)
replace_once(
    coverage,
    dedent('''\
        input_value = contact.to_dict()
        entry = paid.operation(_APOLLO_OPERATION, input_value=input_value)
        if entry is not None:
            _completed_sync_entry(entry, "Apollo")
            raw_email = entry.get("work_email")
            if raw_email is not None and not isinstance(raw_email, str):
                raise ValueError("private Apollo work email is invalid")
            return usable_work_email(raw_email)
        if not allow_shadow_dispatch:
            return None
'''),
    dedent('''\
        input_value = contact.to_dict()
        entry = paid.operation(_APOLLO_OPERATION, input_value=input_value)
        if not allow_shadow_dispatch:
            if entry is not None:
                raise ValueError(
                    "private Apollo coverage lacks normal Clay skip prerequisite"
                )
            return None
        if entry is not None:
            _completed_sync_entry(entry, "Apollo")
            raw_email = entry.get("work_email")
            if raw_email is not None and not isinstance(raw_email, str):
                raise ValueError("private Apollo work email is invalid")
            return usable_work_email(raw_email)
'''),
)
replace_once(
    coverage,
    "    apollo_email = _apollo_email(\n",
    dedent('''\
        normal_clay_skip_prerequisite = _normal_clay_skip_prerequisite(
            company,
            contact,
            normal_contact,
            normal_exa_completed,
            normal_operations,
            normal_usage,
        )
        apollo_email = _apollo_email(
'''),
)
replace_once(
    coverage,
    "        allow_shadow_dispatch=clay_email is not None,\n",
    "        allow_shadow_dispatch=normal_clay_skip_prerequisite,\n",
)

outcomes = "src/leads_discovery/pipeline/canary_outcomes.py"
replace_once(
    outcomes,
    "from leads_discovery.contacts.providers import usable_work_email\n",
    "from leads_discovery.contacts.providers import usable_work_email\n"
    "from leads_discovery.contacts.selection import contact_decision_order_key\n",
)
outcome_marker = "def _m4(state: _State) -> tuple[IntegrationCoverage, ...]:\n"
outcome_helper = dedent('''\
def _normal_clay_skip_prerequisite(state: _State) -> bool:
    """Return whether normal M4 selected a contact then skipped Apollo after Clay email."""
    checkpoint = state.contact_checkpoint
    accepted = [
        company
        for company in state.companies
        if company.stage_status.get("decision") == "completed"
        and company.final_decision == "accepted"
    ]
    if checkpoint is None or len(accepted) != 1:
        return False
    company = accepted[0]
    operations = _operations(checkpoint)
    exa_entry = operations.get(f"exa:{company.company_id}")
    clay_entry = operations.get("clay:batch")
    if (
        not isinstance(exa_entry, dict)
        or exa_entry.get("state") != "completed"
        or not isinstance(clay_entry, dict)
        or clay_entry.get("state") != "completed"
    ):
        return False
    exa_ids = exa_entry.get("contact_ids")
    clay_ids = clay_entry.get("contact_ids")
    if not isinstance(exa_ids, list) or not isinstance(clay_ids, list):
        return False
    if _requests(_matching(state.contact_usage, "exa", {"people_search"})) <= 0:
        return False
    if (
        _requests(_matching(state.contact_usage, "clay", {"work_email_routine_start"})) <= 0
        or _requests(
            _matching(state.contact_usage, "clay", {"work_email_routine_results"})
        )
        <= 0
    ):
        return False
    contacts_by_id = {contact.contact_id: contact for contact in state.contacts}
    selected = [
        contacts_by_id[contact_id]
        for contact_id in exa_ids
        if isinstance(contact_id, str)
        and contact_id in contacts_by_id
        and contacts_by_id[contact_id].company_id == company.company_id
    ]
    if not selected:
        return False
    selected.sort(key=contact_decision_order_key)
    contact = selected[0]
    return (
        contact.contact_id in clay_ids
        and contact.email_source == "clay"
        and usable_work_email(contact.work_email) is not None
    )


''')
replace_once(outcomes, outcome_marker, outcome_helper + outcome_marker)
replace_region(
    outcomes,
    "    apollo_prerequisite = (\n",
    "    has_email = any(\n",
    dedent('''\
        apollo_prerequisite = _normal_clay_skip_prerequisite(state)
        apollo_deferred_by_pending_poll = (
            apollo_prerequisite
            and state.contact_checkpoint is not None
            and state.contact_checkpoint.status == "paused_pending"
            and _instantly_business(state) == "pending"
        )
        normal_apollo = _normal_integration(
            state, "apollo", "apollo", {"people_enrichment"}, "apollo:",
            False, _apollo_business(state),
        )
        private_apollo = _private_integration(
            state, "apollo", "coverage:apollo", "apollo", {"people_enrichment"}
        )
        if normal_apollo is not None:
            apollo = normal_apollo
        elif private_apollo is not None:
            apollo = (
                private_apollo
                if apollo_prerequisite
                else _coverage(
                    "apollo", "coverage_only", "failure", "invalid_evidence",
                    private_apollo.operation_count, private_apollo.request_count,
                )
            )
        else:
            apollo = _coverage(
                "apollo", "coverage_only",
                (
                    "failure"
                    if apollo_prerequisite and not apollo_deferred_by_pending_poll
                    else "inconclusive"
                ),
                "not_exercised", 0, 0,
            )

'''),
)

replace_once(
    "tests/test_canary_provider_coverage.py",
    dedent('''\
        assert clay.result_ids == ["shadow-clay-run"]
        assert len(apollo.contacts) == 1
        assert apollo.contacts[0].to_dict() == expected.to_dict()
        assert instantly.created == ["alice.owner@acme.com"]
'''),
    dedent('''\
        assert clay.result_ids == ["shadow-clay-run"]
        assert apollo.contacts == []
        assert instantly.created == ["alice.owner@acme.com"]
'''),
)

offline = "tests/test_production_canary_offline_contract.py"
edit_region(
    offline,
    "def test_coverage_paid_failure_has_durable_intent_and_never_redispatches(\n",
    "\n\ndef test_coverage_clay_resumes_same_operation_and_stops_at_three_reads(\n",
    [
        ("    clay = ClayRoutineScript([])\n", '    clay = ClayRoutineScript([{"work_email": _EMAIL}])\n', 1),
        (
            '    stub = WireStub({"exa": _exa_one, "clay": clay, "apollo": apollo})\n'
            '    _install_contract(monkeypatch, tmp_path, run_id, _rejected_company(), stub)\n',
            '    stub = WireStub(\n'
            '        {\n'
            '            "exa": _exa_one,\n'
            '            "clay": clay,\n'
            '            "apollo": apollo,\n'
            '            "instantly": _terminal_instantly("verified", expected_email=_EMAIL),\n'
            '        }\n'
            '    )\n'
            '    _install_contract(monkeypatch, tmp_path, run_id, _accepted_company(), stub)\n',
            1,
        ),
        ('    assert len(stub.for_provider("instantly")) == 0\n', '    assert len(stub.for_provider("instantly")) == 1\n', 2),
    ],
)

review = "tests/test_production_canary_offline_review_gaps.py"
edit_region(
    review,
    "def test_successful_coverage_only_waterfall_uses_selected_contact_without_mutating_normal_state(\n",
    "\n\ndef test_normal_m4_completes_after_same_run_pending_resume_without_second_clay_start(\n",
    [
        (
            "def test_successful_coverage_only_waterfall_uses_selected_contact_without_mutating_normal_state(\n",
            "def test_coverage_only_clay_verifies_selected_contact_without_mutating_normal_state(\n",
            1,
        ),
        (
            '    """Coverage-only Clay resumes in one canary call and cannot change canonical M4 state."""\n',
            '    """Coverage Clay may feed verification but cannot authorize Apollo or mutate M4."""\n',
            1,
        ),
        ("    clay = ClayRoutineScript([])\n", '    clay = ClayRoutineScript([{"work_email": _EMAIL}])\n', 1),
        (
            '    apollo_requests = stub.for_provider("apollo")\n'
            '    assert len(apollo_requests) == 1\n'
            '    apollo_body = json_body(apollo_requests[0])\n'
            '    assert {\n'
            '        "name": apollo_body["name"],\n'
            '        "domain": apollo_body["domain"],\n'
            '        "organization_name": apollo_body["organization_name"],\n'
            '        "linkedin_url": apollo_body["linkedin_url"],\n'
            '    } == {\n'
            '        "name": expected.full_name,\n'
            '        "domain": expected.company_domain,\n'
            '        "organization_name": expected.company_name,\n'
            '        "linkedin_url": expected.linkedin_url,\n'
            '    }\n',
            '    assert stub.for_provider("apollo") == []\n'
            '    instantly_requests = stub.for_provider("instantly")\n'
            '    assert len(instantly_requests) == 1\n'
            '    assert json_body(instantly_requests[0])["email"] == _EMAIL\n',
            1,
        ),
    ],
)

safety = "tests/test_production_canary_offline_safety_contract.py"
replace_once(
    safety,
    'from test_production_canary_offline_contract import (\n'
    '    _exa_one,\n'
    '    _install_contract,\n'
    '    _provider,\n'
    '    _rejected_company,\n'
    '    _report,\n'
    '    _run_canary,\n'
    ')\n',
    'from test_production_canary_offline_contract import (\n'
    '    _EMAIL,\n'
    '    _accepted_company,\n'
    '    _exa_one,\n'
    '    _install_contract,\n'
    '    _provider,\n'
    '    _report,\n'
    '    _run_canary,\n'
    '    _terminal_instantly,\n'
    ')\n',
)
replace_once(safety, "    clay = ClayRoutineScript([])\n", '    clay = ClayRoutineScript([{"work_email": _EMAIL}])\n')
replace_once(
    safety,
    '    stub = WireStub({"exa": _exa_one, "clay": clay, "apollo": apollo})\n'
    '    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _rejected_company(), stub)\n',
    '    stub = WireStub(\n'
    '        {\n'
    '            "exa": _exa_one,\n'
    '            "clay": clay,\n'
    '            "apollo": apollo,\n'
    '            "instantly": _terminal_instantly("verified", expected_email=_EMAIL),\n'
    '        }\n'
    '    )\n'
    '    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _accepted_company(), stub)\n',
)
replace_once(safety, '    assert len(stub.for_provider("instantly")) == 0\n', '    assert len(stub.for_provider("instantly")) == 1\n')
replace_once(
    safety,
    '    assert read_jsonl(run_dir / "contacts.jsonl") == []\n',
    '    contacts = read_jsonl(run_dir / "contacts.jsonl")\n'
    '    assert len(contacts) == 1\n'
    '    assert contacts[0]["work_email"] == _EMAIL\n',
)

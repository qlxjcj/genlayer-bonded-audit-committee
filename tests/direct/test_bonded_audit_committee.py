"""Direct-mode tests for the Bonded Audit Committee.

What makes this contract different from a one-shot "fetch -> assess -> store"
module, and what these tests pin down:

  * a time-locked phase machine (REGISTRATION -> COMMIT -> REVEAL -> RESOLVED)
  * commit-reveal so auditors cannot copy each other
  * bonds that are forfeited by the minority and by ungrounded reveals
  * the LLM only grounds a quote against a source -- it never decides
  * the decision is a deterministic tally, and funds actually move
"""

import json

import pytest

from conftest import (
    AUDITOR_A,
    AUDITOR_B,
    AUDITOR_C,
    BOND,
    BODY_OK,
    COMMIT_WINDOW,
    CRITERIA,
    ESCROW,
    PAYER,
    QUOTE_BUDGET,
    QUOTE_MISSING,
    QUOTE_NEUTRAL,
    QUOTE_NO,
    QUOTE_OK,
    QUOTE_SUB,
    RECIPIENT,
    REF_SOURCE,
    REGISTER_WINDOW,
    REVEAL_WINDOW,
    SALT,
    STRANGER,
    URL_BUDGET,
    URL_EMPTY,
    URL_ERROR,
    URL_NEUTRAL,
    URL_NO,
    URL_OK,
    URL_SUB,
    balance,
    commit,
    contract_balance,
    iso,
    join,
    make_job,
    open_job_with,
    remock,
    resolve,
    reveal,
)


# ==========================================================================
# Phase machine
# ==========================================================================


def test_phase_advances_with_time(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    assert c.get_phase(job_id) == "REGISTRATION"
    vm.warp(iso(REGISTER_WINDOW + 5))
    assert c.get_phase(job_id) == "COMMIT"
    vm.warp(iso(REGISTER_WINDOW + COMMIT_WINDOW + 5))
    assert c.get_phase(job_id) == "REVEAL"
    resolve(vm, c, job_id)
    assert c.get_phase(job_id) == "RESOLVED"


def test_cannot_join_after_registration_closes(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    vm.warp(iso(REGISTER_WINDOW + 5))
    vm.sender = AUDITOR_A
    vm.value = BOND
    with pytest.raises(Exception):
        c.join(job_id)
    vm.value = 0


def test_cannot_commit_before_commit_phase(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    vm.warp(iso(REGISTER_WINDOW // 2 + 5))
    vm.sender = AUDITOR_A
    with pytest.raises(Exception):
        c.commit(job_id, "0" * 64)


def test_cannot_reveal_before_reveal_phase(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    vm.warp(iso(REGISTER_WINDOW + COMMIT_WINDOW // 2 + 5))
    vm.sender = AUDITOR_A
    with pytest.raises(Exception):
        c.reveal(job_id, True, URL_OK, QUOTE_OK, SALT)


def test_cannot_resolve_before_reveal_window_closes(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    vm.warp(iso(REGISTER_WINDOW + COMMIT_WINDOW + REVEAL_WINDOW // 2 + 5))
    vm.sender = STRANGER
    with pytest.raises(Exception):
        c.resolve(job_id)


# ==========================================================================
# Job creation and registration
# ==========================================================================


def test_create_job_escrows_funds(committee):
    vm, c = committee
    before = balance(vm, PAYER)
    job_id = make_job(vm, c)
    assert balance(vm, PAYER) == before - ESCROW
    assert contract_balance(vm) == ESCROW
    job = json.loads(c.get_job(job_id))
    assert job["payer"] == PAYER
    assert job["recipient"] == RECIPIENT
    assert job["phase"] == "REGISTRATION"
    assert job["outcome"] == "PENDING"


def test_rejects_invalid_recipient_address(committee):
    vm, c = committee
    vm.sender = PAYER
    vm.value = ESCROW
    with pytest.raises(Exception):
        c.create_job("PlayerA", CRITERIA, REF_SOURCE, 100, 100, 100, BOND)
    vm.value = 0


def test_rejects_recipient_equal_to_payer(committee):
    vm, c = committee
    vm.sender = PAYER
    vm.value = ESCROW
    with pytest.raises(Exception):
        c.create_job(PAYER, CRITERIA, REF_SOURCE, 100, 100, 100, BOND)
    vm.value = 0


def test_rejects_zero_escrow(committee):
    vm, c = committee
    vm.sender = PAYER
    vm.value = 0
    with pytest.raises(Exception):
        c.create_job(RECIPIENT, CRITERIA, REF_SOURCE, 100, 100, 100, BOND)


def test_rejects_sub_minimum_bond(committee):
    vm, c = committee
    vm.sender = PAYER
    vm.value = ESCROW
    with pytest.raises(Exception):
        c.create_job(RECIPIENT, CRITERIA, REF_SOURCE, 100, 100, 100, 1)
    vm.value = 0


def test_rejects_non_positive_window(committee):
    vm, c = committee
    vm.sender = PAYER
    vm.value = ESCROW
    with pytest.raises(Exception):
        c.create_job(RECIPIENT, CRITERIA, REF_SOURCE, 0, 100, 100, BOND)
    vm.value = 0


def test_join_requires_exact_bond(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    vm.warp(iso(50))
    vm.sender = AUDITOR_A
    vm.value = BOND - 1
    with pytest.raises(Exception):
        c.join(job_id)
    vm.value = 0


def test_join_is_a_real_bond_transfer(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    before = balance(vm, AUDITOR_A)
    join(vm, c, job_id, AUDITOR_A)
    assert balance(vm, AUDITOR_A) == before - BOND
    assert c.get_bond(job_id, AUDITOR_A) == BOND
    assert contract_balance(vm) == ESCROW + BOND


def test_rejects_duplicate_registration(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    vm.warp(iso(51))
    vm.sender = AUDITOR_A
    vm.value = BOND
    with pytest.raises(Exception):
        c.join(job_id)
    vm.value = 0


def test_only_registered_auditors_may_commit(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    vm.warp(iso(REGISTER_WINDOW + 5))
    vm.sender = STRANGER
    with pytest.raises(Exception):
        c.commit(job_id, "0" * 64)


# ==========================================================================
# Commit-reveal binding
# ==========================================================================


def test_rejects_malformed_commitment(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    vm.warp(iso(REGISTER_WINDOW + 5))
    vm.sender = AUDITOR_A
    with pytest.raises(Exception):
        c.commit(job_id, "not-a-hash")
    with pytest.raises(Exception):
        c.commit(job_id, "zz" * 32)


def test_rejects_double_commit(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    vm.sender = AUDITOR_A
    with pytest.raises(Exception):
        c.commit(job_id, "1" * 64)


def test_rejects_reveal_without_commit(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    vm.warp(iso(REGISTER_WINDOW + COMMIT_WINDOW + 5))
    vm.sender = AUDITOR_A
    with pytest.raises(Exception):
        c.reveal(job_id, True, URL_OK, QUOTE_OK, SALT)


def test_rejects_reveal_that_does_not_match_commitment(committee):
    """Changing the verdict after committing is impossible."""
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    vm.warp(iso(REGISTER_WINDOW + COMMIT_WINDOW + 5))
    vm.sender = AUDITOR_A
    with pytest.raises(Exception):
        c.reveal(job_id, False, URL_OK, QUOTE_OK, SALT)


def test_rejects_reveal_with_wrong_salt(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    vm.warp(iso(REGISTER_WINDOW + COMMIT_WINDOW + 5))
    vm.sender = AUDITOR_A
    with pytest.raises(Exception):
        c.reveal(job_id, True, URL_OK, QUOTE_OK, "other-salt")


def test_rejects_double_reveal(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    vm.sender = AUDITOR_A
    with pytest.raises(Exception):
        c.reveal(job_id, True, URL_OK, QUOTE_OK, SALT)


# ==========================================================================
# Evidence grounding: the LLM checks a quote, it does not decide
# ==========================================================================


def test_quote_present_and_supporting_is_grounded(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    grounded = reveal(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    assert grounded == "true"
    r = json.loads(c.get_reveal(job_id, AUDITOR_A))
    assert r["grounded"] == "true"
    assert r["present"] == "true"
    assert r["supports"] == "true"


def test_quote_present_and_contradicting_is_grounded_for_the_no_verdict(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    commit(vm, c, job_id, AUDITOR_A, False, URL_NO, QUOTE_NO)
    grounded = reveal(vm, c, job_id, AUDITOR_A, False, URL_NO, QUOTE_NO)
    assert grounded == "true"
    r = json.loads(c.get_reveal(job_id, AUDITOR_A))
    assert r["supports"] == "false"


def test_quote_missing_from_source_is_not_grounded(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_MISSING)
    grounded = reveal(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_MISSING)
    assert grounded == "false"
    r = json.loads(c.get_reveal(job_id, AUDITOR_A))
    assert r["present"] == "false"
    assert r["grounded"] == "false"


def test_empty_source_body_fails_closed(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    commit(vm, c, job_id, AUDITOR_A, True, URL_EMPTY, QUOTE_OK)
    assert reveal(vm, c, job_id, AUDITOR_A, True, URL_EMPTY, QUOTE_OK) == "false"


def test_provider_error_fails_closed(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    commit(vm, c, job_id, AUDITOR_A, True, URL_ERROR, QUOTE_OK)
    assert reveal(vm, c, job_id, AUDITOR_A, True, URL_ERROR, QUOTE_OK) == "false"


def test_quote_that_supports_nothing_is_not_grounded(committee):
    """Present in the source, but probative of neither verdict."""
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    commit(vm, c, job_id, AUDITOR_A, True, URL_NEUTRAL, QUOTE_NEUTRAL)
    assert reveal(vm, c, job_id, AUDITOR_A, True, URL_NEUTRAL, QUOTE_NEUTRAL) == "false"


def test_model_claiming_absence_overrules_the_substring_check(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    commit(vm, c, job_id, AUDITOR_A, True, URL_BUDGET, QUOTE_BUDGET)
    assert reveal(vm, c, job_id, AUDITOR_A, True, URL_BUDGET, QUOTE_BUDGET) == "false"


def test_rejects_invalid_grounding_enum(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    commit(vm, c, job_id, AUDITOR_A, True, URL_SUB, QUOTE_SUB)
    vm.warp(iso(REGISTER_WINDOW + COMMIT_WINDOW + 5))
    vm.sender = AUDITOR_A
    with pytest.raises(Exception):
        c.reveal(job_id, True, URL_SUB, QUOTE_SUB, SALT)


def test_rejects_oversized_quote(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    long_quote = "x" * 401
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, long_quote)
    vm.warp(iso(REGISTER_WINDOW + COMMIT_WINDOW + 5))
    vm.sender = AUDITOR_A
    with pytest.raises(Exception):
        c.reveal(job_id, True, URL_OK, long_quote, SALT)


# ==========================================================================
# Tally and settlement: the decision is deterministic, and funds move
# ==========================================================================


def test_majority_grounded_yes_pays_the_recipient(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    for a in (AUDITOR_A, AUDITOR_B, AUDITOR_C):
        join(vm, c, job_id, a)
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    commit(vm, c, job_id, AUDITOR_B, True, URL_OK, QUOTE_OK)
    commit(vm, c, job_id, AUDITOR_C, False, URL_NO, QUOTE_NO)
    reveal(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_B, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_C, False, URL_NO, QUOTE_NO)

    recipient_before = balance(vm, RECIPIENT)
    payer_before = balance(vm, PAYER)
    resolve(vm, c, job_id)

    job = json.loads(c.get_job(job_id))
    assert job["outcome"] == "DELIVERED"
    assert balance(vm, RECIPIENT) == recipient_before + ESCROW
    assert balance(vm, PAYER) == payer_before, "payer must not be refunded on delivery"


def test_majority_grounded_no_refunds_the_payer(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    for a in (AUDITOR_A, AUDITOR_B, AUDITOR_C):
        join(vm, c, job_id, a)
    commit(vm, c, job_id, AUDITOR_A, False, URL_NO, QUOTE_NO)
    commit(vm, c, job_id, AUDITOR_B, False, URL_NO, QUOTE_NO)
    commit(vm, c, job_id, AUDITOR_C, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_A, False, URL_NO, QUOTE_NO)
    reveal(vm, c, job_id, AUDITOR_B, False, URL_NO, QUOTE_NO)
    reveal(vm, c, job_id, AUDITOR_C, True, URL_OK, QUOTE_OK)

    payer_before = balance(vm, PAYER)
    recipient_before = balance(vm, RECIPIENT)
    resolve(vm, c, job_id)

    job = json.loads(c.get_job(job_id))
    assert job["outcome"] == "NOT_DELIVERED"
    assert balance(vm, PAYER) == payer_before + ESCROW
    assert balance(vm, RECIPIENT) == recipient_before


def test_minority_bond_is_slashed_and_split(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    for a in (AUDITOR_A, AUDITOR_B, AUDITOR_C):
        join(vm, c, job_id, a)
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    commit(vm, c, job_id, AUDITOR_B, True, URL_OK, QUOTE_OK)
    commit(vm, c, job_id, AUDITOR_C, False, URL_NO, QUOTE_NO)
    reveal(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_B, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_C, False, URL_NO, QUOTE_NO)

    resolve(vm, c, job_id)

    # Auditor C is in the minority and loses the bond.
    assert c.get_bond(job_id, AUDITOR_C) == 0
    assert c.get_claimable(job_id, AUDITOR_C) == 0

    # The two majority auditors each get their bond back plus half of C's.
    expected = BOND + BOND // 2
    assert c.get_claimable(job_id, AUDITOR_A) == expected
    assert c.get_claimable(job_id, AUDITOR_B) == expected


def test_ungrounded_reveal_forfeits_its_bond(committee):
    """A reveal whose evidence does not ground is not a vote at all."""
    vm, c = committee
    job_id = make_job(vm, c)
    for a in (AUDITOR_A, AUDITOR_B, AUDITOR_C):
        join(vm, c, job_id, a)
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    commit(vm, c, job_id, AUDITOR_B, True, URL_OK, QUOTE_OK)
    commit(vm, c, job_id, AUDITOR_C, True, URL_NEUTRAL, QUOTE_MISSING)
    reveal(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_B, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_C, True, URL_NEUTRAL, QUOTE_MISSING)

    resolve(vm, c, job_id)

    job = json.loads(c.get_job(job_id))
    assert job["outcome"] == "DELIVERED"
    assert job["grounded_count"] == "2"
    assert c.get_bond(job_id, AUDITOR_C) == 0
    assert c.get_claimable(job_id, AUDITOR_C) == 0
    assert c.get_claimable(job_id, AUDITOR_A) == BOND + BOND // 2


def test_no_grounded_evidence_is_inconclusive_and_refunds_everyone(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    join(vm, c, job_id, AUDITOR_B)
    commit(vm, c, job_id, AUDITOR_A, True, URL_NEUTRAL, QUOTE_MISSING)
    commit(vm, c, job_id, AUDITOR_B, True, URL_EMPTY, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_A, True, URL_NEUTRAL, QUOTE_MISSING)
    reveal(vm, c, job_id, AUDITOR_B, True, URL_EMPTY, QUOTE_OK)

    payer_before = balance(vm, PAYER)
    resolve(vm, c, job_id)

    job = json.loads(c.get_job(job_id))
    assert job["outcome"] == "INCONCLUSIVE"
    assert balance(vm, PAYER) == payer_before + ESCROW
    # nobody is slashed when there is nothing to go on
    assert c.get_claimable(job_id, AUDITOR_A) == BOND
    assert c.get_claimable(job_id, AUDITOR_B) == BOND


def test_tie_is_inconclusive_and_slashes_nobody(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    join(vm, c, job_id, AUDITOR_B)
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    commit(vm, c, job_id, AUDITOR_B, False, URL_NO, QUOTE_NO)
    reveal(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_B, False, URL_NO, QUOTE_NO)

    payer_before = balance(vm, PAYER)
    resolve(vm, c, job_id)

    assert json.loads(c.get_job(job_id))["outcome"] == "INCONCLUSIVE"
    assert balance(vm, PAYER) == payer_before + ESCROW
    assert c.get_claimable(job_id, AUDITOR_A) == BOND
    assert c.get_claimable(job_id, AUDITOR_B) == BOND


def test_auditor_who_never_revealed_gets_the_bond_back_when_inconclusive(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    join(vm, c, job_id, AUDITOR_B)
    commit(vm, c, job_id, AUDITOR_A, True, URL_NEUTRAL, QUOTE_MISSING)
    reveal(vm, c, job_id, AUDITOR_A, True, URL_NEUTRAL, QUOTE_MISSING)
    # AUDITOR_B commits but never reveals

    resolve(vm, c, job_id)
    assert json.loads(c.get_job(job_id))["outcome"] == "INCONCLUSIVE"
    assert c.get_claimable(job_id, AUDITOR_A) == BOND
    assert c.get_claimable(job_id, AUDITOR_B) == BOND


def test_tally_is_recorded_on_the_job(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    for a in (AUDITOR_A, AUDITOR_B, AUDITOR_C):
        join(vm, c, job_id, a)
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    commit(vm, c, job_id, AUDITOR_B, True, URL_OK, QUOTE_OK)
    commit(vm, c, job_id, AUDITOR_C, False, URL_NO, QUOTE_NO)
    reveal(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_B, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_C, False, URL_NO, QUOTE_NO)
    resolve(vm, c, job_id)

    job = json.loads(c.get_job(job_id))
    assert job["grounded_count"] == "3"
    assert job["delivered_votes"] == "2"
    assert job["not_delivered_votes"] == "1"
    assert job["outcome"] == "DELIVERED"
    assert job["phase"] == "RESOLVED"


# ==========================================================================
# Claims
# ==========================================================================


def test_claim_pays_a_real_transfer_and_clears_the_claim(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    for a in (AUDITOR_A, AUDITOR_B, AUDITOR_C):
        join(vm, c, job_id, a)
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    commit(vm, c, job_id, AUDITOR_B, True, URL_OK, QUOTE_OK)
    commit(vm, c, job_id, AUDITOR_C, False, URL_NO, QUOTE_NO)
    reveal(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_B, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_C, False, URL_NO, QUOTE_NO)
    resolve(vm, c, job_id)

    expected = BOND + BOND // 2
    before = balance(vm, AUDITOR_A)
    vm.sender = AUDITOR_A
    c.claim(job_id)
    assert balance(vm, AUDITOR_A) == before + expected
    assert c.get_claimable(job_id, AUDITOR_A) == 0
    # B has not claimed yet, so the contract still holds B's share.
    assert contract_balance(vm) == expected

    before_b = balance(vm, AUDITOR_B)
    vm.sender = AUDITOR_B
    c.claim(job_id)
    assert balance(vm, AUDITOR_B) == before_b + expected
    assert contract_balance(vm) == 0


def test_cannot_claim_twice(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    join(vm, c, job_id, AUDITOR_B)
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    commit(vm, c, job_id, AUDITOR_B, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_B, True, URL_OK, QUOTE_OK)
    resolve(vm, c, job_id)

    vm.sender = AUDITOR_A
    c.claim(job_id)
    with pytest.raises(Exception):
        c.claim(job_id)


def test_cannot_claim_before_resolution(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    vm.warp(iso(REGISTER_WINDOW + COMMIT_WINDOW + 5))
    vm.sender = AUDITOR_A
    with pytest.raises(Exception):
        c.claim(job_id)


def test_stranger_has_nothing_to_claim(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    resolve(vm, c, job_id)
    vm.sender = STRANGER
    with pytest.raises(Exception):
        c.claim(job_id)


# ==========================================================================
# Refund / recovery path
# ==========================================================================


def test_cancel_before_registration_refunds_the_payer(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    payer_before = balance(vm, PAYER)
    vm.warp(iso(10))
    vm.sender = PAYER
    c.cancel_job(job_id)
    assert balance(vm, PAYER) == payer_before + ESCROW
    assert contract_balance(vm) == 0
    assert json.loads(c.get_job(job_id))["outcome"] == "INCONCLUSIVE"


def test_cannot_cancel_after_registration_closes(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    vm.warp(iso(REGISTER_WINDOW + 5))
    vm.sender = PAYER
    with pytest.raises(Exception):
        c.cancel_job(job_id)


def test_cannot_cancel_once_auditors_joined(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    vm.warp(iso(10))
    vm.sender = PAYER
    with pytest.raises(Exception):
        c.cancel_job(job_id)


def test_only_payer_can_cancel(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    vm.warp(iso(10))
    vm.sender = STRANGER
    with pytest.raises(Exception):
        c.cancel_job(job_id)
    assert contract_balance(vm) == ESCROW


def test_cannot_resolve_twice(committee):
    vm, c = committee
    job_id = make_job(vm, c)
    join(vm, c, job_id, AUDITOR_A)
    commit(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    reveal(vm, c, job_id, AUDITOR_A, True, URL_OK, QUOTE_OK)
    resolve(vm, c, job_id)
    with pytest.raises(Exception):
        resolve(vm, c, job_id)

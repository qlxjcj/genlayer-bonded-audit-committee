"""Fixtures for Bonded Audit Committee direct-mode tests.

Direct mode does not track native value flow, so two accounting hooks are
installed (mirroring what the chain does) so the tests can genuinely assert
fund movement:

1. payable value: `create_job` / `join` move `vm.value` sender -> contract
2. `emit_transfer` is intercepted as a PostMessage gl_call and moved
   contract -> recipient

Without them every payout assertion would be a tautology.

Time is driven explicitly with `vm.warp(iso)` so the phase machine
(REGISTRATION -> COMMIT -> REVEAL -> RESOLVED) is tested deterministically.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

CONTRACT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "bonded_audit_committee.py",
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)

REGISTER_WINDOW = 100
COMMIT_WINDOW = 100
REVEAL_WINDOW = 100

ESCROW = 10 ** 18
BOND = 10 ** 16

PAYER = "0x" + "aa" * 20
RECIPIENT = "0x" + "bb" * 20
AUDITOR_A = "0x" + "11" * 20
AUDITOR_B = "0x" + "22" * 20
AUDITOR_C = "0x" + "33" * 20
STRANGER = "0x" + "44" * 20

CRITERIA = "Ship the API integration with all tests passing."
REF_SOURCE = "https://ref.example.com/spec"

# --- evidence sources ---
BODY_OK = "Report 42. The milestone was delivered on time."
BODY_NO = "Report 7. The milestone was not delivered."
BODY_NEUTRAL = "Report 9. Weather was sunny all week."
BODY_BUDGET = "Report 5. Budget was 50000."
BODY_SUB = "Report 3. The submarine was launched successfully."
BODY_EMPTY = ""
BODY_ERROR = "Internal Server Error"

URL_OK = "https://evidence-ok.example.com/report"
URL_NO = "https://evidence-no.example.com/report"
URL_NEUTRAL = "https://evidence-neutral.example.com/report"
URL_BUDGET = "https://evidence-budget.example.com/report"
URL_EMPTY = "https://evidence-empty.example.com/report"
URL_ERROR = "https://evidence-error.example.com/report"
URL_SUB = "https://evidence-sub.example.com/report"

# Each quote must literally appear in its own source body (deterministic check).
QUOTE_OK = "The milestone was delivered on time"
QUOTE_NO = "The milestone was not delivered"
QUOTE_NEUTRAL = "Weather was sunny all week"
QUOTE_BUDGET = "Budget was 50000"
QUOTE_SUB = "the submarine was launched successfully"
QUOTE_MISSING = "the zeppelin reached the north pole"

SALT = "salt-1"

GROUNDED_YES = json.dumps({
    "present": True, "supports": "true", "matched_excerpt": "delivered on time"})
GROUNDED_NO = json.dumps({
    "present": True, "supports": "false", "matched_excerpt": "not delivered"})
GROUNDED_NEITHER = json.dumps({
    "present": True, "supports": "neither", "matched_excerpt": "sunny all week"})
GROUND_NOT_PRESENT = json.dumps({
    "present": False, "supports": "neither", "matched_excerpt": ""})
GROUND_BAD_ENUM = json.dumps({
    "present": True, "supports": "maybe", "matched_excerpt": ""})


def iso(offset: int) -> str:
    return (BASE + timedelta(seconds=offset)).isoformat()


def balance(vm, addr):
    return vm._balances.get(vm._to_bytes(addr), 0)


def contract_balance(vm):
    return vm._balances.get(vm._contract_address, 0)


def remock(vm):
    vm.clear_mocks()
    vm.mock_web(".*evidence-ok.*", {"method": "GET", "status": 200, "body": BODY_OK})
    vm.mock_web(".*evidence-no.*", {"method": "GET", "status": 200, "body": BODY_NO})
    vm.mock_web(".*evidence-neutral.*", {"method": "GET", "status": 200, "body": BODY_NEUTRAL})
    vm.mock_web(".*evidence-empty.*", {"method": "GET", "status": 200, "body": BODY_EMPTY})
    vm.mock_web(".*evidence-error.*", {"method": "GET", "status": 500, "body": BODY_ERROR})
    vm.mock_web(".*evidence-sub.*", {"method": "GET", "status": 200, "body": BODY_SUB})
    vm.mock_web(".*evidence-budget.*", {"method": "GET", "status": 200, "body": BODY_BUDGET})
    # Mocks are first-match-wins; these are keyed on the quoted evidence, which
    # the contract puts verbatim into the grounding prompt.
    vm.mock_llm(r".*milestone was delivered on time.*", GROUNDED_YES)
    vm.mock_llm(r".*milestone was not delivered.*", GROUNDED_NO)
    vm.mock_llm(r".*Weather was sunny all week.*", GROUNDED_NEITHER)
    vm.mock_llm(r".*Budget was 50000.*", GROUND_NOT_PRESENT)
    vm.mock_llm(r".*submarine was launched.*", GROUND_BAD_ENUM)
    vm.mock_llm(r".*zeppelin reached the north pole.*", GROUNDED_YES)


@pytest.fixture
def committee(direct_vm, direct_deploy):
    vm = direct_vm

    def _value_transfer_hook(vm, request):
        if "PostMessage" not in request:
            return None
        msg = request["PostMessage"]
        amount = int(msg.get("value", 0))
        if amount > 0:
            contract = vm._contract_address
            recipient = vm._to_bytes(msg["address"])
            vm._balances[contract] = vm._balances.get(contract, 0) - amount
            vm._balances[recipient] = vm._balances.get(recipient, 0) + amount
        return {"ok": None}

    vm._gl_call_hook = _value_transfer_hook

    remock(vm)
    c = direct_deploy(CONTRACT)

    # Payable accounting for both payable entry points.
    def _wrap(fn):
        def inner(*args):
            if vm.value > 0:
                sender = vm._to_bytes(vm.sender)
                contract = vm._contract_address
                vm._balances[sender] = vm._balances.get(sender, 0) - vm.value
                vm._balances[contract] = vm._balances.get(contract, 0) + vm.value
            return fn(*args)
        return inner

    c.create_job = _wrap(c.create_job)
    c.join = _wrap(c.join)

    for addr in (PAYER, RECIPIENT, AUDITOR_A, AUDITOR_B, AUDITOR_C, STRANGER):
        vm.deal(addr, 100 * ESCROW)

    vm.warp(iso(0))
    vm.sender = PAYER
    return vm, c


def make_job(vm, c, escrow=ESCROW, bond=BOND, recipient=RECIPIENT,
             register=REGISTER_WINDOW, commit=COMMIT_WINDOW, reveal=REVEAL_WINDOW):
    vm.warp(iso(0))
    vm.sender = PAYER
    vm.value = escrow
    job_id = c.create_job(recipient, CRITERIA, REF_SOURCE, register, commit, reveal, bond)
    vm.value = 0
    return job_id


def join(vm, c, job_id, auditor, bond=BOND):
    vm.warp(iso(REGISTER_WINDOW // 2))
    vm.sender = auditor
    vm.value = bond
    c.join(job_id)
    vm.value = 0


def commit(vm, c, job_id, auditor, verdict, evidence_ref, quote, salt=SALT):
    vm.warp(iso(REGISTER_WINDOW + COMMIT_WINDOW // 2))
    vm.sender = auditor
    h = c.get_commitment_hash(verdict, evidence_ref, quote, salt)
    c.commit(job_id, h)
    return h


def reveal(vm, c, job_id, auditor, verdict, evidence_ref, quote, salt=SALT):
    vm.warp(iso(REGISTER_WINDOW + COMMIT_WINDOW + REVEAL_WINDOW // 2))
    vm.sender = auditor
    return c.reveal(job_id, verdict, evidence_ref, quote, salt)


def resolve(vm, c, job_id):
    vm.warp(iso(REGISTER_WINDOW + COMMIT_WINDOW + REVEAL_WINDOW + 10))
    vm.sender = STRANGER
    c.resolve(job_id)


def open_job_with(vm, c, auditors, job_id=None):
    """auditors: list of (address, verdict, evidence_ref, quote)."""
    if job_id is None:
        job_id = make_job(vm, c)
    for addr, _, _, _ in auditors:
        join(vm, c, job_id, addr)
    for addr, verdict, ref, quote in auditors:
        commit(vm, c, job_id, addr, verdict, ref, quote)
    for addr, verdict, ref, quote in auditors:
        reveal(vm, c, job_id, addr, verdict, ref, quote)
    return job_id

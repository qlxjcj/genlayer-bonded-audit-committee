# Bonded Audit Committee

A GenLayer intelligent contract where a **committee of bonded auditors** decides
whether a paid-for deliverable was actually delivered. The escrow moves to the
recipient or back to the payer based on their verdicts.

Bradbury testnet: `0x6CA0B62c9513c84711EC0A1C752c69bF068ec7c7`

---

## Why this is not another "fetch -> assess -> store" module

GenLayer already has plenty of contracts of the shape

```
caller supplies URLs -> gl.nondet.web.render -> one prompt_comparative
                     -> JSON record in a TreeMap -> counter + get_stats
```

This contract is built the other way round. **The model never decides.** It only
answers one narrow, checkable question: does this quoted line really appear in
this source, and does it back the auditor's verdict? Everything else — who
decides, when, with what incentive, and where the money goes — is deterministic
contract logic.

| | one-shot assessor | this contract |
|---|---|---|
| who produces the verdict | the model | bonded auditors, independently |
| collusion resistance | none | commit-then-reveal |
| incentives | none | bonds, forfeited by the minority and by anyone whose evidence does not ground |
| lifecycle | a single call | `REGISTRATION -> COMMIT -> REVEAL -> RESOLVED`, time-locked |
| funds | none | escrow to the recipient or refunded to the payer |
| the model's job | "decide X" | "is this quote in this source, and does it support the claim?" |
| the decision | one model output | a deterministic tally over grounded reveals |

## Lifecycle

```
 create_job  (payable: escrow)
     |
     v
 REGISTRATION   join()        auditors post an exact bond
     |          [register_deadline]
     v
 COMMIT         commit()      auditors post sha256(verdict|evidence|quote|salt)
     |          [commit_deadline]
     v
 REVEAL         reveal()      auditors disclose; the contract grounds the quote
     |          [reveal_deadline]
     v
 RESOLVED       resolve()     deterministic tally, escrow moves
                claim()       auditors withdraw bond + reward
```

Phases advance from `gl.message_raw['datetime']`, so each call is gated by an
explicit deadline rather than by trusting the caller.

## The model's only job: grounding

`reveal()` hashes are checked first — a reveal that does not match its commitment
is rejected before any network or model work happens. Then, inside the
equivalence block:

1. **deterministic pre-check** — the quoted text must literally occur in the
   fetched source (whitespace-normalised). If it does not, the reveal is
   ungrounded without ever asking the model.
2. **model check** — the model returns `present`, `supports`
   (`true`/`false`/`neither`) and a `matched_excerpt`.
3. **grounded** ⟺ `present` **and** `supports` agrees with the auditor's verdict.

The equivalence principle is deliberately tight:

> Equivalent if `retrieved`, `present` and `supports` all match exactly.
> `matched_excerpt` wording may differ.

Grounding happens **at reveal time**, one model call per auditor, so `resolve()`
is a pure state transition with no nondeterministic work left in it.

## Bond economics

| situation | effect |
|---|---|
| reveal whose evidence does not ground | bond forfeited |
| auditor in the minority | bond forfeited |
| auditor in the majority | bond returned + equal share of everything forfeited |
| no grounded reveal at all | inconclusive: escrow refunded to the payer, **nobody slashed** |
| tied vote | inconclusive: escrow refunded, **nobody slashed** |

"Nothing to go on" never becomes a decision, and never costs an honest auditor
their bond.

## Settlement

- `DELIVERED` -> escrow transfers to the registered `recipient`
- `NOT_DELIVERED` / `INCONCLUSIVE` -> escrow transfers back to the `payer`
- `claim()` pays each auditor their bond plus their share, then zeroes the claim

`cancel_job()` is the recovery path: while the committee is still open and empty,
the payer can withdraw the whole escrow.

## Testing

```
python -m pytest tests/direct -q
```

47 direct-mode tests. Direct mode does not track native value flow on its own, so
`tests/direct/conftest.py` installs two accounting hooks (a payable hook moving
`vm.value` sender -> contract, and a `PostMessage` gl_call hook moving contract ->
recipient on `emit_transfer`). Without them the payout assertions would be
tautologies. With them the tests assert real balance deltas.

Time is driven explicitly with `vm.warp(iso)` so the phase machine is tested
deterministically.

Coverage includes:

- every phase gate (join/commit/reveal/resolve called in the wrong phase)
- real bond transfer on join; real escrow payout to the recipient on `DELIVERED`
- real refund to the payer on `NOT_DELIVERED`, on `INCONCLUSIVE`, and on `cancel_job`
- minority bonds forfeited and split between the majority
- an ungrounded reveal forfeits its bond
- commit/reveal binding: wrong verdict, wrong salt, no commit, double commit,
  double reveal all rejected
- grounding: quote missing from source, empty body, HTTP 500, quote that supports
  nothing, model claiming absence, invalid grounding enum
- oversized quotes, malformed commitment hashes, non-auditor calls
- claim pays out and clears; double claim rejected; claim before resolution rejected

## Live on chain

Verified against the deployed contract on Bradbury:

| check | result |
|---|---|
| participant that is not a blockchain address | rejected |
| recipient equal to the payer | rejected |
| empty criteria | rejected |
| non-http reference source | rejected |
| bond below the minimum | rejected |
| non-positive phase window | rejected |
| zero escrow | rejected |
| `get_commitment_hash` for identical inputs | identical hashes |
| `get_commitment_hash` with the verdict flipped | different hash |

`genlayer write` hardcodes `value: 0n`, so a funded job cannot be opened from the
CLI; the funded lifecycle is exercised by the test suite with real balance
assertions and can be run from Studio or any `genlayer-js` client that sets
`value` on `create_job` / `join`.

## Contract surface

| Method | Kind | Purpose |
|---|---|---|
| `create_job(recipient, criteria, reference_source, register_window, commit_window, reveal_window, bond)` | write, payable | escrows the payment and opens the committee |
| `join(job_id)` | write, payable | register as an auditor, posting exactly `bond` |
| `commit(job_id, commitment_hash)` | write | post `sha256(verdict\|evidence_ref\|quote\|salt)` |
| `reveal(job_id, verdict, evidence_ref, quote, salt)` | write | disclose; the contract grounds the quote |
| `resolve(job_id)` | write | deterministic tally; escrow moves; bonds redistributed |
| `claim(job_id)` | write | withdraw bond + reward |
| `cancel_job(job_id)` | write | payer's refund path while the committee is empty |
| `get_job(job_id)` | view | full record incl. `outcome`, vote totals, deadlines |
| `get_phase(job_id)` | view | current phase, advanced to the caller's timestamp |
| `get_bond` / `get_claimable` / `get_commitment` / `get_reveal` | view | per-auditor state, keyed by `(job, auditor)` |
| `get_auditors(job_id)` | view | the registered committee |
| `get_job_count()` | view | counter |
| `get_commitment_hash(verdict, evidence_ref, quote, salt)` | view | build a commitment off chain before committing |

# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
import hashlib
import json
from genlayer import *

MIN_BOND = 1000000000000000
MAX_AUDITORS = 25
MAX_QUOTE_LEN = 400

REGISTRATION = "REGISTRATION"
COMMIT = "COMMIT"
REVEAL = "REVEAL"
RESOLVED = "RESOLVED"

PENDING = "PENDING"
DELIVERED = "DELIVERED"
NOT_DELIVERED = "NOT_DELIVERED"
INCONCLUSIVE = "INCONCLUSIVE"


class BondedAuditCommittee(gl.Contract):
    """A committee of bonded auditors decides whether a paid-for deliverable was
    really delivered: commit-then-reveal, bonds forfeited by the minority, and a
    model that only grounds a quote against a source. See README."""

    jobs: TreeMap[str, str]
    bonds: TreeMap[str, u256]
    commitments: TreeMap[str, str]
    reveals: TreeMap[str, str]
    claimable: TreeMap[str, u256]
    job_count: u256

    def __init__(self):
        self.job_count = 0

    # ---------------- helpers ----------------

    def _addr_hex(self, a) -> str:
        if isinstance(a, str):
            return a
        h = getattr(a, "as_hex", None)
        if isinstance(h, str):
            return h
        if isinstance(a, (bytes, bytearray)):
            return "0x" + bytes(a).hex()
        return str(a)

    def _is_valid_address(self, addr) -> bool:
        if not isinstance(addr, str) or not addr.startswith("0x") or len(addr) != 42:
            return False
        try:
            int(addr[2:], 16)
        except ValueError:
            return False
        return True

    def _iso_to_epoch(self, s: str) -> int:
        # Hand-rolled so every validator agrees, with no optional stdlib.
        s = s.strip()
        if len(s) < 19:
            return 0
        offset = 0
        tail = s[19:]
        if tail.endswith("Z"):
            tail = tail[:-1]
        if tail and (tail[0] == "+" or tail[0] == "-"):
            try:
                offset = (1 if tail[0] == "+" else -1) * (int(tail[1:3]) * 3600 + int(tail[4:6]) * 60)
            except (ValueError, IndexError):
                offset = 0
        try:
            y, mo, d = int(s[0:4]), int(s[5:7]), int(s[8:10])
            hh, mm, ss = int(s[11:13]), int(s[14:16]), int(s[17:19])
        except (ValueError, IndexError):
            return 0
        if not (1 <= mo <= 12 and 1 <= d <= 31 and hh < 24 and mm < 60 and ss < 61):
            return 0
        yy = y - 1 if mo <= 2 else y
        era = yy // 400
        yoe = yy - era * 400
        doy = (153 * (mo + (-3 if mo > 2 else 9)) + 2) // 5 + d - 1
        doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
        return (era * 146097 + doe - 719468) * 86400 + hh * 3600 + mm * 60 + ss - offset

    def _now(self) -> int:
        # gl.message exposes no clock; the ISO stamp lives on message_raw.
        raw = getattr(gl, "message_raw", None)
        d = None
        if raw is not None:
            try:
                d = raw["datetime"]
            except Exception:
                d = None
        if d is None:
            d = getattr(gl.message, "datetime", None)
        if d is None:
            return 0
        if isinstance(d, str):
            return self._iso_to_epoch(d)
        return self._iso_to_epoch(str(d))

    def _decode_body(self, content) -> str:
        body = getattr(content, "body", None)
        if body is None:
            return str(content)
        return body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)

    def _norm(self, text: str) -> str:
        return " ".join(str(text).lower().split())

    def _key(self, job_id: str, auditor: str) -> str:
        return str(job_id) + "|" + self._addr_hex(auditor).lower()

    def _bond_amount(self, key: str) -> int:
        v = self.bonds.get(key, 0)
        return int(v) if v is not None else 0

    def _claim_amount(self, key: str) -> int:
        v = self.claimable.get(key, 0)
        return int(v) if v is not None else 0

    def _commitment_hash(self, verdict: bool, evidence_ref: str, quote: str, salt: str) -> str:
        material = ("true" if verdict else "false") + "|" + evidence_ref + "|" + quote + "|" + salt
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _load_job(self, job_id: str) -> dict:
        raw = self.jobs.get(str(job_id), "")
        if not raw:
            raise gl.vm.UserError("Job not found")
        return json.loads(raw)

    def _save_job(self, job: dict):
        self.jobs[job["job_id"]] = json.dumps(job)

    def _auditors(self, job: dict) -> list:
        try:
            return json.loads(job["auditors"])
        except (json.JSONDecodeError, TypeError):
            return []

    def _is_auditor(self, job: dict, addr: str) -> bool:
        target = self._addr_hex(addr).lower()
        for a in self._auditors(job):
            if self._addr_hex(a).lower() == target:
                return True
        return False

    def _transfer(self, recipient: Address, amount: int):
        if amount > 0:
            gl.get_contract_at(recipient).emit_transfer(value=u256(amount))

    # ---------------- phase machine ----------------

    def _advance(self, job: dict):
        now = self._now()
        if job["phase"] == REGISTRATION and now >= int(job["register_deadline"]):
            job["phase"] = COMMIT
        if job["phase"] == COMMIT and now >= int(job["commit_deadline"]):
            job["phase"] = REVEAL

    def _require_phase(self, job: dict, phase: str, what: str):
        self._advance(job)
        if job["phase"] != phase:
            raise gl.vm.UserError(what + " only in " + phase + ", now " + job["phase"])

    # ---------------- evidence grounding ----------------

    def _ground(self, question: str, verdict: bool, quote: str, evidence_ref: str) -> dict:
        # Fetch and model call are both nondeterministic, so both live inside
        # the equivalence block.
        def check() -> dict:
            body = ""
            try:
                body = self._decode_body(gl.nondet.web.render(evidence_ref))
            except Exception:
                body = ""
            if not body or self._norm(quote) not in self._norm(body):
                return {"retrieved": False, "present": False, "supports": "neither", "matched_excerpt": ""}

            fmt = '{"present": <true|false>, "supports": "true"|"false"|"neither", "matched_excerpt": "<text>"}'
            task = (
                "Check one quoted piece of evidence against a source. Do NOT decide the case.\n"
                "QUESTION: " + question + "\n"
                "VERDICT CLAIMED: " + ("completed" if verdict else "not completed") + "\n"
                "QUOTE: " + quote + "\nSOURCE:\n" + body[:1500] + "\n\n"
                "JSON only: " + fmt + "\n"
                "present=true only if the quote really appears in the source. "
                "supports=true if it backs the verdict, false if it contradicts, neither otherwise. "
                "matched_excerpt is the matching part of the source."
            )
            result = gl.nondet.exec_prompt(task)
            if isinstance(result, str):
                result = json.loads(result.replace("```json", "").replace("```", ""))
            if not isinstance(result, dict):
                raise gl.vm.UserError("[LLM_ERROR] non-dict result")
            result["retrieved"] = True
            return result

        principle = (
            "Equivalent if retrieved, present and supports all match exactly. "
            "matched_excerpt wording may differ."
        )
        return gl.eq_principle.prompt_comparative(check, principle)

    # ---------------- lifecycle ----------------

    @gl.public.write.payable
    def create_job(self, recipient: str, criteria: str, reference_source: str,
                   register_window: int, commit_window: int, reveal_window: int,
                   bond: int) -> str:
        payer = self._addr_hex(gl.message.sender_address)
        escrow = int(gl.message.value)
        if not self._is_valid_address(recipient):
            raise gl.vm.UserError("Recipient must be 0x + 40 hex")
        if self._addr_hex(recipient).lower() == payer.lower():
            raise gl.vm.UserError("Recipient must differ from payer")
        if not criteria or not criteria.strip():
            raise gl.vm.UserError("Criteria required")
        if not reference_source.startswith("http://") and not reference_source.startswith("https://"):
            raise gl.vm.UserError("Reference source must be http(s)")
        if escrow <= 0:
            raise gl.vm.UserError("Escrow must be > 0")
        if int(bond) < MIN_BOND:
            raise gl.vm.UserError("Bond below minimum")
        if int(register_window) <= 0 or int(commit_window) <= 0 or int(reveal_window) <= 0:
            raise gl.vm.UserError("Windows must be > 0")

        now = self._now()
        self.job_count += 1
        job_id = str(self.job_count)
        self.jobs[job_id] = json.dumps({
            "job_id": job_id,
            "payer": payer,
            "recipient": self._addr_hex(recipient),
            "criteria": criteria.strip(),
            "reference_source": reference_source.strip(),
            "escrow": str(escrow),
            "bond": str(int(bond)),
            "phase": REGISTRATION,
            "register_deadline": str(now + int(register_window)),
            "commit_deadline": str(now + int(register_window) + int(commit_window)),
            "reveal_deadline": str(now + int(register_window) + int(commit_window) + int(reveal_window)),
            "outcome": PENDING,
            "auditors": "[]",
            "grounded_count": "0",
            "delivered_votes": "0",
            "not_delivered_votes": "0",
            "created_at": str(now),
        })
        return job_id

    @gl.public.write.payable
    def join(self, job_id: str) -> str:
        job = self._load_job(job_id)
        self._require_phase(job, REGISTRATION, "Joining")
        auditor = self._addr_hex(gl.message.sender_address)
        if self._is_auditor(job, auditor):
            raise gl.vm.UserError("Already registered")
        if int(gl.message.value) != int(job["bond"]):
            raise gl.vm.UserError("Bond must equal " + job["bond"])
        auditors = self._auditors(job)
        if len(auditors) >= MAX_AUDITORS:
            raise gl.vm.UserError("Committee full")
        auditors.append(auditor)
        job["auditors"] = json.dumps(auditors)
        self._save_job(job)
        self.bonds[self._key(job_id, auditor)] = u256(int(job["bond"]))
        return auditor

    @gl.public.write
    def cancel_job(self, job_id: str):
        # Refund path: the payer can withdraw while the committee is still open.
        job = self._load_job(job_id)
        self._advance(job)
        if job["phase"] != REGISTRATION:
            raise gl.vm.UserError("Only REGISTRATION can be cancelled")
        if self._addr_hex(gl.message.sender_address).lower() != job["payer"].lower():
            raise gl.vm.UserError("Only payer can cancel")
        if len(self._auditors(job)) > 0:
            raise gl.vm.UserError("Auditors already joined; let it resolve")
        job["phase"] = RESOLVED
        job["outcome"] = INCONCLUSIVE
        self._save_job(job)
        self._transfer(Address(job["payer"]), int(job["escrow"]))

    @gl.public.write
    def commit(self, job_id: str, commitment_hash: str):
        job = self._load_job(job_id)
        self._require_phase(job, COMMIT, "Commitment")
        auditor = self._addr_hex(gl.message.sender_address)
        if not self._is_auditor(job, auditor):
            raise gl.vm.UserError("Only registered auditors commit")
        if not isinstance(commitment_hash, str) or len(commitment_hash) != 64:
            raise gl.vm.UserError("commitment_hash must be 64 hex chars")
        try:
            int(commitment_hash, 16)
        except ValueError:
            raise gl.vm.UserError("commitment_hash must be hex")
        key = self._key(job_id, auditor)
        if self._bond_amount(key) == 0:
            raise gl.vm.UserError("No bond posted")
        if key in self.commitments:
            raise gl.vm.UserError("Already committed")
        self.commitments[key] = commitment_hash

    @gl.public.write
    def reveal(self, job_id: str, verdict: bool, evidence_ref: str, quote: str, salt: str) -> str:
        job = self._load_job(job_id)
        self._require_phase(job, REVEAL, "Reveal")
        auditor = self._addr_hex(gl.message.sender_address)
        if not self._is_auditor(job, auditor):
            raise gl.vm.UserError("Only registered auditors reveal")
        key = self._key(job_id, auditor)
        if key in self.reveals:
            raise gl.vm.UserError("Already revealed")
        expected = self.commitments.get(key, "")
        if not expected:
            raise gl.vm.UserError("Nothing committed")
        if not isinstance(verdict, bool):
            raise gl.vm.UserError("verdict must be bool")
        if not evidence_ref.startswith("http://") and not evidence_ref.startswith("https://"):
            raise gl.vm.UserError("evidence_ref must be http(s)")
        if not quote or not quote.strip():
            raise gl.vm.UserError("quote required")
        quote = quote.strip()
        if len(quote) > MAX_QUOTE_LEN:
            raise gl.vm.UserError("quote too long")
        if not salt or not salt.strip():
            raise gl.vm.UserError("salt required")
        salt = salt.strip()
        evidence_ref = evidence_ref.strip()
        if self._commitment_hash(verdict, evidence_ref, quote, salt) != expected:
            raise gl.vm.UserError("Reveal does not match commitment")

        g = self._ground(job["criteria"], verdict, quote, evidence_ref)
        if not (isinstance(g, dict) and isinstance(g.get("retrieved"), bool)
                and isinstance(g.get("present"), bool)
                and g.get("supports") in ("true", "false", "neither")
                and isinstance(g.get("matched_excerpt"), str)):
            raise gl.vm.UserError("Invalid grounding result")
        present = g.get("present") is True
        supports = str(g.get("supports", "neither"))
        grounded = present and supports == ("true" if verdict else "false")

        self.reveals[key] = json.dumps({
            "job_id": str(job_id),
            "auditor": auditor,
            "verdict": "true" if verdict else "false",
            "evidence_ref": evidence_ref,
            "quote": quote,
            "salt": salt,
            "grounded": "true" if grounded else "false",
            "present": "true" if present else "false",
            "supports": supports,
            "matched_excerpt": str(g.get("matched_excerpt", "")),
        })
        return "true" if grounded else "false"

    @gl.public.write
    def resolve(self, job_id: str):
        # Deterministic tally: grounding already happened at reveal, so this is
        # a pure state transition with no nondeterministic work left.
        job = self._load_job(job_id)
        self._advance(job)
        if job["phase"] == RESOLVED:
            raise gl.vm.UserError("Already resolved")
        if self._now() < int(job["reveal_deadline"]):
            raise gl.vm.UserError("Reveal window still open")
        job["phase"] = RESOLVED

        auditors = self._auditors(job)
        escrow = int(job["escrow"])
        yes, no, ungrounded = [], [], []
        for auditor in auditors:
            raw = self.reveals.get(self._key(job_id, auditor), "")
            if not raw:
                ungrounded.append(auditor)
                continue
            r = json.loads(raw)
            if r.get("grounded") != "true":
                ungrounded.append(auditor)
            elif r.get("verdict") == "true":
                yes.append(auditor)
            else:
                no.append(auditor)

        total = len(yes) + len(no)
        if total == 0:
            job.update(outcome=INCONCLUSIVE, grounded_count="0",
                       delivered_votes="0", not_delivered_votes="0")
            self._save_job(job)
            self._transfer(Address(job["payer"]), escrow)
            for auditor in auditors:
                key = self._key(job_id, auditor)
                self.claimable[key] = u256(self._claim_amount(key) + self._bond_amount(key))
                self.bonds[key] = u256(0)
            return

        if len(yes) > len(no):
            outcome = DELIVERED
        elif len(no) > len(yes):
            outcome = NOT_DELIVERED
        else:
            outcome = INCONCLUSIVE

        job.update(outcome=outcome, grounded_count=str(total),
                   delivered_votes=str(len(yes)), not_delivered_votes=str(len(no)))
        self._save_job(job)

        self._transfer(Address(job["recipient"] if outcome == DELIVERED else job["payer"]), escrow)

        forfeited = 0
        for auditor in ungrounded:
            key = self._key(job_id, auditor)
            forfeited += self._bond_amount(key)
            self.bonds[key] = u256(0)

        majority = yes if outcome == DELIVERED else (no if outcome == NOT_DELIVERED else [])
        if not majority:
            for auditor in yes + no:
                key = self._key(job_id, auditor)
                self.claimable[key] = u256(self._claim_amount(key) + self._bond_amount(key))
                self.bonds[key] = u256(0)
            return

        for auditor in yes + no:
            if auditor in majority:
                continue
            key = self._key(job_id, auditor)
            forfeited += self._bond_amount(key)
            self.bonds[key] = u256(0)

        share = forfeited // len(majority)
        for auditor in majority:
            key = self._key(job_id, auditor)
            self.claimable[key] = u256(self._claim_amount(key) + self._bond_amount(key) + share)
            self.bonds[key] = u256(0)

    @gl.public.write
    def claim(self, job_id: str):
        job = self._load_job(job_id)
        if job["phase"] != RESOLVED:
            raise gl.vm.UserError("Claim only after resolution")
        key = self._key(job_id, gl.message.sender_address)
        amount = self._claim_amount(key)
        if amount <= 0:
            raise gl.vm.UserError("Nothing to claim")
        self.claimable[key] = u256(0)
        self._transfer(Address(self._addr_hex(gl.message.sender_address)), amount)

    # ---------------- views ----------------

    @gl.public.view
    def get_job(self, job_id: str) -> str:
        return self.jobs.get(str(job_id), "{}")

    @gl.public.view
    def get_phase(self, job_id: str) -> str:
        raw = self.jobs.get(str(job_id), "")
        if not raw:
            return ""
        job = json.loads(raw)
        self._advance(job)
        return job["phase"]

    @gl.public.view
    def get_bond(self, job_id: str, auditor: str) -> int:
        return self._bond_amount(self._key(job_id, auditor))

    @gl.public.view
    def get_commitment(self, job_id: str, auditor: str) -> str:
        return self.commitments.get(self._key(job_id, auditor), "")

    @gl.public.view
    def get_reveal(self, job_id: str, auditor: str) -> str:
        return self.reveals.get(self._key(job_id, auditor), "{}")

    @gl.public.view
    def get_claimable(self, job_id: str, auditor: str) -> int:
        return self._claim_amount(self._key(job_id, auditor))

    @gl.public.view
    def get_auditors(self, job_id: str) -> str:
        raw = self.jobs.get(str(job_id), "")
        return json.loads(raw)["auditors"] if raw else "[]"

    @gl.public.view
    def get_job_count(self) -> int:
        return self.job_count

    @gl.public.view
    def get_commitment_hash(self, verdict: bool, evidence_ref: str, quote: str, salt: str) -> str:
        return self._commitment_hash(verdict, evidence_ref, quote, salt)

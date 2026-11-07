"""Rebuild the profiling and held-out slices from what Phase 1 committed
(RC1-631, ADR 0010, R3).

The committed query log and split manifest are the source. `rebuild` reads
the log (checking its SHA-256), resolves each line's tenant against the
dataset, re-runs the seeded split and checks the result against the
manifest: same candidate, same assignment hash, same held-out accounts. Any
difference stops the run.

Access is asymmetric. `profiling()` is open. `held_out()` needs a verified
frozen candidate or refusal (pocbench.candidate), so the development entry
point has no way to obtain held-out instances. `guard_profiling` re-checks
every instance a development run is about to execute against two
independent sources, the rebuilt assignment and the manifest's account
list, and refuses on any held-out item: the planted-leak test plants one.
"""

import gzip
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import psycopg

from pocbench import candidate, config, profile, shapes, split, workload
from pocbench.loadgen import Instance

MANIFEST = "results/phase1/split-manifest.json"


class LeakError(RuntimeError):
    """A development run was about to execute a held-out instance."""


class SliceMismatchError(RuntimeError):
    """The rebuilt split differs from the committed manifest."""


@dataclass(frozen=True)
class SliceInstances:
    name: str
    seqs: tuple[int, ...]
    accounts: tuple[int | None, ...]
    instances: tuple[Instance, ...]


@dataclass(frozen=True)
class Rebuilt:
    manifest: dict
    slice_of: dict[int, str]
    _items: dict[str, SliceInstances]

    def profiling(self) -> SliceInstances:
        return self._items[split.PROFILING]

    def held_out(self, frozen: candidate.Frozen | None) -> SliceInstances:
        if not isinstance(frozen, candidate.Frozen) or not frozen.verified:
            raise candidate.NotFrozenError(
                "the held-out slice is only released against a verified frozen candidate "
                "or refusal record")
        return self._items[split.HELD_OUT]


def read_log(root: Path, manifest: dict) -> list[tuple[int, str]]:
    path = root / manifest["log"]["path"]
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if sha != manifest["log"]["sha256"]:
        raise SliceMismatchError(f"{path.name} sha256 {sha[:12]} != manifest "
                                 f"{manifest['log']['sha256'][:12]}")
    rows = [json.loads(line) for line in gzip.decompress(raw).decode().splitlines()]
    return [(r["seq"], r["sql"]) for r in rows]


def assemble(entries: list[tuple[int, str]], account_of: dict[int, int | None],
             account_sizes: dict[int, int], manifest: dict, space: dict) -> Rebuilt:
    """Pure part of `rebuild`: split, check against the manifest, group."""
    sid_of_template = {v["template"]: sid for sid, v in manifest["shapes"].items()}
    norm = {seq: shapes.normalize(sql) for seq, sql in entries}
    unknown = {n.template for n in norm.values()} - set(sid_of_template)
    if unknown:
        raise SliceMismatchError(f"{len(unknown)} log templates are not in the manifest")
    s = space["split"]
    sp = split.make([split.Item(seq, sid_of_template[norm[seq].template], account_of[seq])
                     for seq, _ in entries], account_sizes, held_frac=s["held_out_fraction"],
                    seed=s["seed"], candidates=s["candidates"])
    problems = []
    if sp.candidate != manifest["rule"]["chosen_candidate"]:
        problems.append(f"candidate {sp.candidate} != {manifest['rule']['chosen_candidate']}")
    if split.assignment_sha256(sp.slice_of) != manifest["assignment_sha256"]:
        problems.append("assignment sha256 differs")
    if sorted(sp.held_out_accounts) != manifest["slices"][split.HELD_OUT]["accounts"]:
        problems.append("held-out accounts differ")
    if problems:
        raise SliceMismatchError("rebuilt split does not match the manifest: "
                                 + "; ".join(problems))
    grouped: dict[str, list] = {split.PROFILING: [], split.HELD_OUT: []}
    for seq, _ in entries:
        n = norm[seq]
        grouped[sp.slice_of[seq]].append(
            (seq, account_of[seq], Instance(sid_of_template[n.template], n.executable, n.params)))
    items = {name: SliceInstances(name, tuple(g[0] for g in rows), tuple(g[1] for g in rows),
                                  tuple(g[2] for g in rows))
             for name, rows in grouped.items()}
    return Rebuilt(manifest, dict(sp.slice_of), items)


def rebuild(conn: psycopg.Connection, root: Path = config.REPO_ROOT) -> Rebuilt:
    manifest = json.loads((root / MANIFEST).read_text())
    space = config.config_space(root / "config" / "config-space.toml").data
    entries = read_log(root, manifest)
    norm = {seq: shapes.normalize(sql) for seq, sql in entries}
    account_of = profile.resolve_accounts(conn, norm)
    facts = workload.read_facts(conn)
    sizes = dict(zip(facts.accounts, facts.account_weights, strict=True))
    return assemble(entries, account_of, sizes, manifest, space)


def guard_profiling(si: SliceInstances, rebuilt: Rebuilt) -> None:
    """Refuse a development run that would execute any held-out instance."""
    if si.name != split.PROFILING:
        raise LeakError(f"development runs take the profiling slice, not {si.name!r}")
    held_accounts = set(rebuilt.manifest["slices"][split.HELD_OUT]["accounts"])
    by_assignment = [s for s in si.seqs if rebuilt.slice_of.get(s) != split.PROFILING]
    by_account = [s for s, a in zip(si.seqs, si.accounts, strict=True) if a in held_accounts]
    if by_assignment or by_account:
        leaked = sorted(set(by_assignment) | set(by_account))
        raise LeakError(f"{len(leaked)} held-out instances in a development run "
                        f"(first seqs {leaked[:5]}); refusing to run")

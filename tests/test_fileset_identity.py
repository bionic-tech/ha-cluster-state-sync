"""Identity preservation across a fileset swap (Task 13).

The go-bag replaces `.storage` wholesale, and `.storage/core.config_entries`
holds two things that must be treated oppositely:

* the `mobile_app` registrations and refresh-token-adjacent entries, which the
  promoted node has to **inherit** or the companion app stops working;
* this integration's own config entry, which it must **not** inherit.

That entry carries `node_id`, and the lease that prevents two leaders
(ADR-003, AR-0017) compares identity — `elseif current == ARGV[1] then
PEXPIRE` — so a standby promoted carrying the leader's `node_id` renews the
*leader's* lease, and the returning leader is told it still holds it. Both
nodes then believe they lead, both publish, and each prunes the other's blobs
computing `keep` from its own generations. The rehearsal observed exactly that.

Suffixing `node_id` with the install UUID (AR-0025) does not help: the UUID
lives in `.storage/core.uuid`, which is replicated too.

The same entry also carries `peer_host` (inheriting it makes a promoted node
its own peer), `ha_container_ip` and `ha_config_path` — the two nodes'
`/config` mounts are already known to differ (finding F4).

The fix preserves this node's own entry **wholesale** rather than merging a
list of per-node fields, deliberately: a per-node setting added in a year
cannot then silently reintroduce this bug. The cost is that a genuinely
cluster-wide setting changed on the leader does not propagate until the
operator reconfigures — config drift, in the safe direction.

One field goes the other way: `entry_id` is taken from the go-bag. It is not a
setting anyone chooses — it is the join key of the registries inherited in the
same swap — and keeping the local one leaves the leader's entities orphaned as
`unavailable`, squatting the ids `failover_readiness.yaml` addresses. See
`test_the_grafted_entry_keeps_our_settings_but_takes_the_go_bags_entry_id`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from custom_components.cluster_state_sync.scripts.fileset_identity import (
    DOMAIN,
    EXIT_FAILED,
    EXIT_NO_LOCAL_IDENTITY,
    EXIT_OK,
    IdentityError,
    graft_entries,
    main,
    own_entries,
)


def _entry(domain: str, node: str, **data: object) -> dict:
    return {
        "entry_id": f"{domain}-{node}",
        "domain": domain,
        "title": f"{domain} on {node}",
        "unique_id": f"{domain}@{node}",
        "data": {"node": node, **data},
        "options": {},
    }


def _doc(entries: list[dict]) -> dict:
    return {
        "version": 1,
        "minor_version": 1,
        "key": "core.config_entries",
        "data": {"entries": entries},
    }


def _write_storage(storage: Path, entries: list[dict]) -> Path:
    storage.mkdir(parents=True, exist_ok=True)
    target = storage / "core.config_entries"
    target.write_text(json.dumps(_doc(entries), indent=2), encoding="utf-8")
    return target


def _leader_entries() -> list[dict]:
    return [
        _entry("mobile_app", "node-A", webhook_id="wh-A"),
        _entry(DOMAIN, "node-A", node_id="node-A", peer_host="node-A-peer.lan"),
        _entry("zha", "node-A"),
    ]


def _local_entries() -> list[dict]:
    return [
        _entry("mobile_app", "node-B", webhook_id="wh-B"),
        _entry(DOMAIN, "node-B", node_id="node-B", peer_host="node-B-peer.lan"),
    ]


def _entries_in(storage: Path) -> list[dict]:
    raw = (storage / "core.config_entries").read_text(encoding="utf-8")
    return json.loads(raw)["data"]["entries"]


# --------------------------------------------------------------------------
# The discriminating case, as pure functions
# --------------------------------------------------------------------------


def test_capture_then_restore_keeps_our_node_id_and_the_peers_registrations() -> None:
    """The whole task in one assertion pair.

    `node_id` must come out **B** — ours — while the `mobile_app` entry must
    come out **A's**. Any fix that preserves the wrong half fails one of them.
    """
    captured = own_entries(_doc(_local_entries()))

    grafted = graft_entries(_doc(_leader_entries()), captured)

    entries = grafted["data"]["entries"]
    ours = [e for e in entries if e["domain"] == DOMAIN]
    assert [e["data"]["node_id"] for e in ours] == ["node-B"]
    assert [e["data"]["peer_host"] for e in ours] == ["node-B-peer.lan"]
    assert [e["unique_id"] for e in entries if e["domain"] == "mobile_app"] == ["mobile_app@node-A"]


def test_the_grafted_entry_keeps_our_settings_but_takes_the_go_bags_entry_id() -> None:
    """The other half of the discriminating pair, and the suite passed under
    either shape without it.

    `entry_id` is the one field that must come from the go-bag. It is not a
    per-node *setting* -- `config_entries.py` mints it as `ulid_now()`, so two
    independently configured nodes never share one -- it is the join key of the
    registries this swap inherits wholesale. This integration's entities key
    off it twice (`_attr_unique_id = f"{entry.entry_id}_{key}"` and
    `DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})`), so keeping the local
    one means `async_get_or_create` matches nothing in the inherited
    `core.entity_registry` and creates fresh rows. Nothing cleans the old ones
    up -- the entity registry prunes only on `ConfigEntries.async_remove`, and
    a `config_entry_id` absent at load is never removed -- so
    `_write_unavailable_states` pins the leader's four sensors to `unavailable`
    forever, squatting the clean entity ids. The swap also installs the
    leader's `automations.yaml`, and `failover_readiness.yaml` addresses those
    four by `!input`: the promoted node would run the readiness automation
    against dead ids, and an `unavailable` binary_sensor never matches
    `state: "on"`. The failover alarm, inert on the node that just failed over.
    """
    grafted = graft_entries(_doc(_leader_entries()), own_entries(_doc(_local_entries())))

    ours = [e for e in grafted["data"]["entries"] if e["domain"] == DOMAIN]
    assert [e["entry_id"] for e in ours] == [f"{DOMAIN}-node-A"], (
        "the entity and device registries inherited from the go-bag join on the "
        "go-bag's entry_id; keeping ours orphans every row that references it"
    )
    # ...and nothing else came from the go-bag.
    assert ours[0]["data"]["node_id"] == "node-B"
    assert ours[0]["unique_id"] == f"{DOMAIN}@node-B"
    assert ours[0]["title"] == f"{DOMAIN} on node-B"


def test_our_entry_id_is_kept_when_the_go_bag_has_none_to_pair_with() -> None:
    """An unconfigured leader publishes a go-bag with no `cluster_state_sync`
    entry, so there is no id to inherit. Falling back to our own is the only
    answer that leaves a loadable file."""
    grafted = graft_entries(
        _doc([_entry("mobile_app", "node-A")]), own_entries(_doc(_local_entries()))
    )

    ours = [e for e in grafted["data"]["entries"] if e["domain"] == DOMAIN]
    assert [e["entry_id"] for e in ours] == [f"{DOMAIN}-node-B"]


def test_an_unpaired_second_entry_keeps_its_own_id() -> None:
    """Pairing is positional and unambiguous at one entry per node, which is
    the only shape seen in the field. A second local entry with nothing to pair
    against must not be dropped, and must not be handed a duplicate id."""
    second = _entry(DOMAIN, "node-B2", node_id="node-B2")
    ours = [*own_entries(_doc(_local_entries())), second]

    grafted = graft_entries(_doc(_leader_entries()), ours)

    got = [e["entry_id"] for e in grafted["data"]["entries"] if e["domain"] == DOMAIN]
    assert got == [f"{DOMAIN}-node-A", f"{DOMAIN}-node-B2"]
    assert len(set(got)) == len(got), "two config entries cannot share an entry_id"


def test_graft_does_not_mutate_what_it_was_given() -> None:
    """`graft_entries` re-homes entries by copying them. Mutating the captured
    list in place would make a second `restore` -- promotion scripts get
    re-run -- graft an entry that already carries the go-bag's id, which reads
    as working right up until the go-bag changes."""
    captured = own_entries(_doc(_local_entries()))
    before = [dict(e) for e in captured]

    graft_entries(_doc(_leader_entries()), captured)

    assert captured == before


def test_graft_leaves_every_other_domain_exactly_as_the_go_bag_had_it() -> None:
    """Byte-identical, not merely present: the go-bag's entries are the whole
    point of replicating `.storage`, and quietly rewriting one would be a
    silent data change in the file Home Assistant boots from."""
    leader = _doc(_leader_entries())
    foreign_before = [e for e in leader["data"]["entries"] if e["domain"] != DOMAIN]

    grafted = graft_entries(leader, own_entries(_doc(_local_entries())))

    foreign_after = [e for e in grafted["data"]["entries"] if e["domain"] != DOMAIN]
    assert foreign_after == foreign_before


def test_graft_keeps_the_stores_envelope() -> None:
    """`core.config_entries` is a Home Assistant `Store` file, not a bare
    list. Dropping `version`/`key` would make it unloadable — a promotion that
    ends in a Home Assistant which cannot read its own config entries."""
    grafted = graft_entries(_doc(_leader_entries()), own_entries(_doc(_local_entries())))

    assert grafted["version"] == 1
    assert grafted["key"] == "core.config_entries"


def test_own_entries_finds_nothing_when_this_node_is_unconfigured() -> None:
    assert own_entries(_doc([_entry("mobile_app", "node-B")])) == []


# --------------------------------------------------------------------------
# capture — reads the LIVE file, before the swap replaces it
# --------------------------------------------------------------------------


def test_capture_writes_only_our_own_entries(tmp_path: Path) -> None:
    storage = tmp_path / ".storage"
    _write_storage(storage, _local_entries())
    out = tmp_path / "identity.json"

    rc = main(["capture", "--storage", str(storage), "--identity", str(out)])

    assert rc == EXIT_OK
    captured = json.loads(out.read_text(encoding="utf-8"))["entries"]
    assert [e["domain"] for e in captured] == [DOMAIN]
    assert captured[0]["data"]["node_id"] == "node-B"


def test_the_captured_file_is_not_world_readable(tmp_path: Path) -> None:
    """It carries `cluster_secret` and `redis_password` in the clear — the
    same two credentials the bundle writes 0600 on the host. It lands in a
    world-*writable* directory (`/tmp`), which makes the mode the only thing
    protecting it, and it exists for the length of a promotion on a host that
    runs other things."""
    storage = tmp_path / ".storage"
    _write_storage(storage, [_entry(DOMAIN, "node-B", cluster_secret="s3cr3t")])
    out = tmp_path / "identity.json"
    out.write_text("pre-existing, world readable", encoding="utf-8")
    out.chmod(0o644)

    main(["capture", "--storage", str(storage), "--identity", str(out)])

    assert stat.S_IMODE(out.stat().st_mode) & 0o077 == 0, oct(out.stat().st_mode)


def test_capture_of_a_node_with_no_storage_at_all_is_not_an_error(tmp_path: Path) -> None:
    """A node that has never run Home Assistant has no `core.config_entries`.
    That is the unconfigured case, handled at restore time by *removing* the
    leader's entries — not a reason to abort a promotion here."""
    out = tmp_path / "identity.json"

    rc = main(["capture", "--storage", str(tmp_path / "nothing"), "--identity", str(out)])

    assert rc == EXIT_OK
    assert json.loads(out.read_text(encoding="utf-8"))["entries"] == []


def test_capture_refuses_an_unreadable_config_entries_file(tmp_path: Path) -> None:
    """Truncated or half-written JSON means we cannot know what this node's
    identity is. Failing here stops the swap before it installs the leader's
    — which is the safe way to be wrong, because being logged out is
    degrading and two writers is corrupting."""
    storage = tmp_path / ".storage"
    storage.mkdir()
    (storage / "core.config_entries").write_text('{"data": {"entr', encoding="utf-8")

    rc = main(["capture", "--storage", str(storage), "--identity", str(tmp_path / "id.json")])

    assert rc == EXIT_FAILED


def test_capture_refuses_a_file_of_the_wrong_shape(tmp_path: Path) -> None:
    """Valid JSON is not the same as a config-entries store. A future Home
    Assistant that reshaped this file must stop the swap, not be silently
    read as "this node has no identity"."""
    storage = tmp_path / ".storage"
    storage.mkdir()
    (storage / "core.config_entries").write_text('{"data": {"entries": {}}}', encoding="utf-8")

    rc = main(["capture", "--storage", str(storage), "--identity", str(tmp_path / "id.json")])

    assert rc == EXIT_FAILED


# --------------------------------------------------------------------------
# restore — writes into the NEW file, after the swap installed it
# --------------------------------------------------------------------------


def test_restore_puts_our_entry_back_into_the_go_bags_file(tmp_path: Path) -> None:
    storage = tmp_path / ".storage"
    _write_storage(storage, _local_entries())
    identity = tmp_path / "identity.json"
    assert main(["capture", "--storage", str(storage), "--identity", str(identity)]) == EXIT_OK
    # The swap happens here: the leader's `.storage` replaces ours wholesale.
    _write_storage(storage, _leader_entries())

    rc = main(["restore", "--storage", str(storage), "--identity", str(identity)])

    assert rc == EXIT_OK
    entries = _entries_in(storage)
    assert [e["data"]["node_id"] for e in entries if e["domain"] == DOMAIN] == ["node-B"]
    assert [e["entry_id"] for e in entries if e["domain"] == DOMAIN] == [f"{DOMAIN}-node-A"]
    assert [e["unique_id"] for e in entries if e["domain"] == "mobile_app"] == ["mobile_app@node-A"]


def test_restore_removes_the_leaders_entry_when_this_node_has_none(tmp_path: Path) -> None:
    """Decided edge case: an unconfigured node must not *acquire* an identity.

    Nearly unreachable — no config means no pull timer means no go-bag — but
    AR-0040 lived its entire life in exactly that kind of gap. The distinct
    exit code is what makes the swap mark the promotion degraded rather than
    report a clean one.
    """
    storage = tmp_path / ".storage"
    _write_storage(storage, [_entry("mobile_app", "node-B")])
    identity = tmp_path / "identity.json"
    main(["capture", "--storage", str(storage), "--identity", str(identity)])
    _write_storage(storage, _leader_entries())

    rc = main(["restore", "--storage", str(storage), "--identity", str(identity)])

    assert rc == EXIT_NO_LOCAL_IDENTITY
    assert [e["domain"] for e in _entries_in(storage)] == ["mobile_app", "zha"]


def test_restore_fails_rather_than_guess_when_the_capture_is_missing(tmp_path: Path) -> None:
    """No capture file means capture never ran, or ran and vanished. Writing
    the file unchanged would leave the leader's `node_id` installed and report
    success — the exact defect this exists to close."""
    storage = tmp_path / ".storage"
    _write_storage(storage, _leader_entries())

    rc = main(["restore", "--storage", str(storage), "--identity", str(tmp_path / "gone")])

    assert rc == EXIT_FAILED


def test_restore_fails_on_a_corrupt_go_bag_file(tmp_path: Path) -> None:
    storage = tmp_path / ".storage"
    _write_storage(storage, _local_entries())
    identity = tmp_path / "identity.json"
    main(["capture", "--storage", str(storage), "--identity", str(identity)])
    (storage / "core.config_entries").write_text("{ truncated", encoding="utf-8")

    rc = main(["restore", "--storage", str(storage), "--identity", str(identity)])

    assert rc == EXIT_FAILED


def test_restore_leaves_the_file_untouched_when_it_fails(tmp_path: Path) -> None:
    """The swap rolls `.storage` back on a restore failure, so a half-written
    `core.config_entries` would be rolled away anyway — but a failure that
    corrupts the file first turns a recoverable promotion into an unbootable
    one if the rollback is ever the thing that breaks."""
    storage = tmp_path / ".storage"
    _write_storage(storage, _leader_entries())
    before = (storage / "core.config_entries").read_text(encoding="utf-8")

    main(["restore", "--storage", str(storage), "--identity", str(tmp_path / "gone")])

    assert (storage / "core.config_entries").read_text(encoding="utf-8") == before


def test_restore_keeps_the_files_permissions(tmp_path: Path) -> None:
    """Written through a temporary file and `os.replace`, so a crash mid-write
    cannot leave a truncated `core.config_entries`. That swaps the inode, so
    the mode has to be carried over explicitly or an owner-only store file
    silently becomes world-readable."""
    storage = tmp_path / ".storage"
    target = _write_storage(storage, _local_entries())
    identity = tmp_path / "identity.json"
    main(["capture", "--storage", str(storage), "--identity", str(identity)])
    _write_storage(storage, _leader_entries())
    target.chmod(0o600)

    main(["restore", "--storage", str(storage), "--identity", str(identity)])

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_restore_is_idempotent(tmp_path: Path) -> None:
    """Promotion scripts get re-run. A second pass must not stack two copies
    of our entry into the file — Home Assistant would set the integration up
    twice and both copies would take the lease."""
    storage = tmp_path / ".storage"
    _write_storage(storage, _local_entries())
    identity = tmp_path / "identity.json"
    main(["capture", "--storage", str(storage), "--identity", str(identity)])
    _write_storage(storage, _leader_entries())

    main(["restore", "--storage", str(storage), "--identity", str(identity)])
    main(["restore", "--storage", str(storage), "--identity", str(identity)])

    assert len([e for e in _entries_in(storage) if e["domain"] == DOMAIN]) == 1


# --------------------------------------------------------------------------
# The shipped copy is the tested copy
# --------------------------------------------------------------------------


def test_the_module_imports_nothing_outside_the_standard_library() -> None:
    """It runs on the host, from a promotion script, with nothing installed —
    the same constraint `ha_device_preflight.py` and `fileset_pull.py` work
    under. A third-party import here fails at the one moment it matters."""
    import ast

    from custom_components.cluster_state_sync.scripts import fileset_identity

    source = Path(fileset_identity.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    assert imported <= set(__import__("sys").stdlib_module_names), sorted(
        imported - set(__import__("sys").stdlib_module_names)
    )


@pytest.mark.parametrize("subcommand", ["capture", "restore"])
def test_both_subcommands_require_their_arguments(subcommand: str) -> None:
    """argparse exits 2 on a missing required flag. Asserted so a rename of
    `--identity` cannot silently make the swap's invocation a no-op."""
    with pytest.raises(SystemExit) as exc:
        main([subcommand])

    assert exc.value.code == 2


def test_an_unknown_subcommand_is_refused() -> None:
    with pytest.raises(SystemExit):
        main(["merge"])


def test_capture_does_not_leave_a_partial_file_behind_on_failure(tmp_path: Path) -> None:
    """Otherwise a later restore reads a truncated capture, or worse, an empty
    one — and an empty capture means "this node is unconfigured", which
    *removes* entries."""
    storage = tmp_path / ".storage"
    storage.mkdir()
    (storage / "core.config_entries").write_text("{ truncated", encoding="utf-8")
    out = tmp_path / "identity.json"

    main(["capture", "--storage", str(storage), "--identity", str(out)])

    assert not out.exists() or not os.path.getsize(out)


def test_a_write_failure_is_one_line_and_an_exit_code_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A full disk, or a `.storage` this promotion cannot write.

    The behaviour was already correct by accident — an unhandled exception
    exits 1, so the swap rolls back either way — but what the operator finds
    in the promotion log at 3am should be the single line this module promises
    everywhere else, not a stack trace from a script they have never read.
    """
    if os.geteuid() == 0:
        pytest.skip("a read-only directory does not stop root")

    storage = tmp_path / ".storage"
    _write_storage(storage, _leader_entries())
    identity = tmp_path / "identity.json"
    _write_storage(storage, _local_entries())
    main(["capture", "--storage", str(storage), "--identity", str(identity)])
    _write_storage(storage, _leader_entries())
    storage.chmod(0o500)
    try:
        rc = main(["restore", "--storage", str(storage), "--identity", str(identity)])
    finally:
        storage.chmod(0o700)

    assert rc == EXIT_FAILED
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert len(err.strip().splitlines()) == 1, err
    assert "restore" in err


def test_a_go_bag_carrying_more_of_our_entries_than_we_have_is_refused() -> None:
    """The asymmetric remainder that is not harmless.

    Go-bag `[A, B]` against a local `[X]`: positional pairing re-homes X onto
    A and **B simply vanishes**, taking with it nothing except every
    `core.entity_registry` row that joins on B's `entry_id` — orphaned, pinned
    `unavailable` for good, squatting the ids `failover_readiness.yaml`
    addresses. That is the Critical this pairing exists to prevent, arriving
    through the back door.

    Nothing here can make it coherent: this program edits one file and does not
    touch the registries. So it refuses, and the swap rolls back — the same
    trade the rest of this module makes, because a node on its own old config
    is degraded and a node with orphaned registry rows is quietly broken.
    """
    go_bag = _doc(
        [
            _entry("mobile_app", "node-A"),
            _entry(DOMAIN, "node-A", node_id="node-A"),
            _entry(DOMAIN, "node-A2", node_id="node-A2"),
        ]
    )

    with pytest.raises(IdentityError, match="2 cluster_state_sync config entries"):
        graft_entries(go_bag, own_entries(_doc(_local_entries())))


def test_the_refusal_reaches_the_caller_as_an_exit_code(tmp_path: Path) -> None:
    """`graft_entries` raising has to come out of `main` as EXIT_FAILED, which
    is what makes the swap roll back rather than install the go-bag."""
    storage = tmp_path / ".storage"
    _write_storage(storage, _local_entries())
    identity = tmp_path / "identity.json"
    main(["capture", "--storage", str(storage), "--identity", str(identity)])
    _write_storage(
        storage,
        [
            _entry("mobile_app", "node-A"),
            _entry(DOMAIN, "node-A", node_id="node-A"),
            _entry(DOMAIN, "node-A2", node_id="node-A2"),
        ],
    )
    before = (storage / "core.config_entries").read_text(encoding="utf-8")

    rc = main(["restore", "--storage", str(storage), "--identity", str(identity)])

    assert rc == EXIT_FAILED
    assert (storage / "core.config_entries").read_text(encoding="utf-8") == before


def test_an_unconfigured_node_is_not_the_refusal_case(tmp_path: Path) -> None:
    """Empty `ours` against a go-bag holding two entries is still the decided
    unconfigured path — remove them and mark — not a refusal. The two are told
    apart by whether this node has an identity to protect at all."""
    storage = tmp_path / ".storage"
    _write_storage(storage, [_entry("mobile_app", "node-B")])
    identity = tmp_path / "identity.json"
    main(["capture", "--storage", str(storage), "--identity", str(identity)])
    _write_storage(
        storage,
        [
            _entry("mobile_app", "node-A"),
            _entry(DOMAIN, "node-A", node_id="node-A"),
            _entry(DOMAIN, "node-A2", node_id="node-A2"),
        ],
    )

    rc = main(["restore", "--storage", str(storage), "--identity", str(identity)])

    assert rc == EXIT_NO_LOCAL_IDENTITY
    assert [e["domain"] for e in _entries_in(storage)] == ["mobile_app"]

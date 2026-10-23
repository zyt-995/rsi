"""Synchronize the released CoreDNS test server fixture in the private grader."""
import hashlib

ISTIO_TASK = "istio__istio_lc_0078_3c4a72e5"
ISTIO_BASE = "da8b81877e396e871b1c372493d8c62e8a2ae372"
ISTIO_SOURCE = "pilot/pkg/serviceregistry/kube/controller/ambient/testdata"
ISTIO_DESTINATION = "pilot/pkg/serviceregistry/ambient/testdata"
ISTIO_TREE_SHA256 = "27013819daa8aa22ca4ab08041b6454484fd59a6fea12921f597e2045ecac907"


def restore_istio_testdata(item, container, output, docker, atomic):
    if item["raw"]["base_commit"] != ISTIO_BASE:
        raise RuntimeError("Istio fixture base changed")
    tree = docker("exec", "--user", "0", container, "git", "-C", "/testbed",
                  "ls-tree", "-rz", ISTIO_BASE + ":" + ISTIO_SOURCE).stdout
    if hashlib.sha256(tree).hexdigest() != ISTIO_TREE_SHA256:
        raise RuntimeError("Istio released testdata differs from audited original blobs")
    # The released patches move Go test files but omit these 44 unchanged
    # fixtures. Only the disposable private grader receives the relocated copy.
    command = ("set -euo pipefail\ncd /testbed\n"
               "rm -rf -- /testbed/" + ISTIO_DESTINATION + "\n"
               "mkdir -p /testbed/" + ISTIO_DESTINATION + "\n"
               "git archive --format=tar " + ISTIO_BASE + ":" + ISTIO_SOURCE
               + " | tar --no-same-owner -xf - -C /testbed/" + ISTIO_DESTINATION + "\n")
    docker("exec", "--user", "0", container, "bash", "-lc", command)
    atomic(output / "private-istio-testdata.json", {
        "base_commit": ISTIO_BASE, "source": ISTIO_SOURCE, "destination": ISTIO_DESTINATION,
        "git_tree_manifest_sha256": ISTIO_TREE_SHA256, "files": 44,
        "source_bytes_unchanged": True, "private_grader_only": True,
        "official_assertions_patches_commands_unchanged": True,
        "solver_or_proposer_modified": False,
        "original_verifier_deadline_extended": False})

TASK = "coredns__coredns_lc_0053_2263340f"
SOURCE_SHA256 = "1b07ce75a2c8f81639382ffc9b65a40c265eaf55ca729de2867898a37f3f0a74"


def synchronized_fixture(source):
    if hashlib.sha256(source.encode()).hexdigest() != SOURCE_SHA256:
        raise RuntimeError("CoreDNS private test fixture does not match audited base")
    source = source.replace('"sync"', '"sync"\n "time"\n "github.com/miekg/dns"', 1)
    marker = "udp, tcp = CoreDNSServerPorts(i, 0)"
    assert source.count(marker) == 1
    # Receiving a response establishes that ActivateAndServe has started before
    # the caller can Stop its temporary port probe. The root query matches none
    # of this task's zones, so it does not invoke the tested plugin chains.
    return source.replace(marker, marker + '''
 if udp != "" {
  m := new(dns.Msg); m.SetQuestion(".", dns.TypeNS)
  _, _, readyErr := (&dns.Client{Timeout: time.Second}).Exchange(m, udp)
  if readyErr != nil { i.Stop(); return nil, "", "", readyErr }
 }''', 1)


def apply(item, container, output, docker, copy_into, atomic):
    if item["instance_id"] == ISTIO_TASK:
        restore_istio_testdata(item, container, output, docker, atomic)
        return
    if item["instance_id"] != TASK:
        return
    source = docker("exec", "--user", "0", container, "git", "-C", "/testbed",
                    "show", item["raw"]["base_commit"] + ":test/server.go").stdout.decode()
    fixed = synchronized_fixture(source)
    path = output / "private-test-server-fixture.go"
    path.write_text(fixed)
    copy_into(container, path, "/testbed/test/server.go")
    atomic(output / "private-test-server-fixture.json", {
        "original_sha256": SOURCE_SHA256,
        "effective_sha256": hashlib.sha256(fixed.encode()).hexdigest(),
        "reason": "Prevent stopped port-probe server from receiving later test queries",
        "official_assertions_patches_commands_unchanged": True,
        "solver_or_proposer_modified": False,
        "original_verifier_deadline_extended": False})
